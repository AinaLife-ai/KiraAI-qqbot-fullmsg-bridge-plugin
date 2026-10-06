"""QQ官方bot兼容与增强补丁 — KiraAI plugin（2.x / 3.0 双世代）。

定位
----
* **2.x**：核心缺东西 ⇒ 补丁型增强（补解析器、接管事件、补发送链）。
* **3.0**：核心已把「全量群消息 / 真昵称 / @ 解析 / 引用收发 / 去重 / 富内容归一化」
  都做完了 ⇒ **纯增强插件**，只做核心没做的部分，绝不重复接管、绝不造事件。
* **同一份代码**靠 `core_profiles.detect()` 自动判世代，用户零配置。

在 2.x 上补什么
------------
  ① `_parser unknown event group_message_create` —— 给 qq-botpy 补群全量消息解析器；
  ② @ 消息与单聊统一接管（昵称取真实 QQ 昵称，而非 32 位 OpenID）；
  ③ `is_mentioned` 按 NapCat 语义判定；④ 同一 msg_id 去重；
  ⑤ 自动记住昵称；⑥ 主动消息兜底（被动窗口失效时）。

两版都做的新增能力（3.0 也没有）
--------------------------
  A. **群名**：`GET /v2/groups/{openid}/info` 后台拉一次 + 本地缓存
     （白名单接口，失败自动降级为 openid，用户无需任何操作）；
  B. **markdown / 键盘**：`<markdown>` / `<keyboard>` 标签 → 发送层分流
     （纯文本消息没有 @ 能力，markdown = 能排版 + 能真 @）；
  C. **互动回调**：INTERACTION_CREATE → 3 秒内回执 → 转成一条消息给模型；
  D. **群管理工具**：撤回 / 禁言 / 禁言查询 / 机器人群内状态（Route 直发，跨世代通用）；
  E. **成员事件**（需 `extra_intents` 开关）：成员进出 / 加群申请；
  F. **3.0 引用唤醒补洞**：核心判据只认内存里"自己发过的消息"，重启即失效。

性能、阻塞与可逆性约定
----------------------
* 消息路径上**没有同步 I/O、没有锁、没有无界循环**；
* 群名拉取一律 `create_task` 后台执行；昵称/群名落盘走 `asyncio.to_thread`；
* 每个异常都被兜住并降级成一条日志，绝不把异常抛回 botpy 的事件循环；
* 所有 KiraAI 私有属性都用 `getattr` 防御式读取；
* **补丁全部可逆**：把 `enabled` 关掉后，下一次巡检会把所有补丁还原成框架原生实现。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import time
from collections import OrderedDict

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

if "qqbot_bridge" in sys.modules:
    try:
        importlib.reload(sys.modules["qqbot_bridge"])
    except Exception:
        pass

from core.plugin import BasePlugin, logger

try:  # 钩子装饰器与优先级：老/裁剪过的 core 可能没有 → 降级为"不注入工具与标签"
    from core.plugin import Priority, on

    _HOOKS_AVAILABLE = True
except Exception:  # pragma: no cover
    Priority = None  # type: ignore
    on = None  # type: ignore
    _HOOKS_AVAILABLE = False

try:  # 标签基类：同上
    from core.tag import BaseTag
except Exception:  # pragma: no cover
    class BaseTag:  # type: ignore
        name = None
        description = None
        parent = "msg"

        def __init__(self, ctx=None, **kwargs):
            self.ctx = ctx

        def __init_subclass__(cls, **kw):
            super().__init_subclass__(**kw)

if not _HOOKS_AVAILABLE:
    def _noop_hook(*_a, **_kw):
        def _inner(func):
            return func

        return _inner

    class _OnStub:
        llm_request = staticmethod(_noop_hook)

    on = _OnStub()  # type: ignore

    class _PriorityStub:
        MEDIUM = 0
        SYS_HIGH = 100

    Priority = _PriorityStub  # type: ignore
from core.chat import Group, User
from core.chat.message_elements import At, File, Image, Reply, Text
from core.chat.message_utils import KiraIMMessage, KiraMessageEvent, KiraIMSentResult

from core_profiles import (
    GEN_UNKNOWN,
    GEN_V2,
    GEN_V3,
    CoreProfile,
    detect as detect_profile,
    is_allowed as profile_is_allowed,
    message_types_of,
)
from group_names import GroupInfoCache
from rich_content import (
    KEYBOARD_TAG_DESCRIPTION,
    KeyboardMarker,
    MarkdownText,
    MARKDOWN_TAG_DESCRIPTION,
    split_markdown_and_keyboard,
    validate_keyboard,
)
from interactions import InteractionBridge
from api_send import (
    PENDING_KB,
    PENDING_MD,
    QUOTE_REF,
    ApiSendPatcher,
)
from admin_tools import build_tools as build_admin_tools
from v3_support import V3Enhancer

from qqbot_bridge import (
    EVENT_C2C_MESSAGE,
    EVENT_GROUP_AT_MESSAGE,
    EVENT_GROUP_MESSAGE,
    KIND_AT,
    KIND_DM,
    KIND_FULL,
    IdentityStore,
    MessageDedup,
    SelfIdentity,
    attach_client_handler,
    build_event,
    check_adapter_capabilities,
    collect_self_ids,
    dedup_key,
    detach_client_handler,
    drop_live_parser,
    at_user_markup,
    collect_self_identity,
    describe_member_event,
    extract_msg_idx,
    extract_sent_ref_idx,
    normalize_outgoing_markup,
    strip_at_markup,
    inject_live_parser,
    install_class_parser,
    install_member_parser,
    build_member_event,
    MEMBER_EVENTS,
    normalize_body,
    normalize_mentions,
    restore_class_parser,
)

_SELF_PLUGIN_ID = "qqbot-fullmsg-bridge"

#: 引用索引兜底表（适配器不支持挂属性时按名字共享）
_REF_STORES_BY_NAME: dict = {}


def ref_store_for(adapter):
    """取「适配器级」共享的引用索引。

    挂在**适配器对象**上而不是插件实例上 —— 热重载/多次加载时，
    负责收消息的实例和负责发消息的实例可能不是同一个，
    各存一份就会出现「明明记过、却查不到」（用户实测踩到）。
    """
    try:
        store = getattr(adapter, "_qqbot_bridge_refs", None)
        if store is None:
            store = OrderedDict()
            adapter._qqbot_bridge_refs = store
        return store
    except Exception:
        pass
    name = str(getattr(getattr(adapter, "info", None), "name", "?"))
    return _REF_STORES_BY_NAME.setdefault(name, OrderedDict())



def _plugin_version() -> str:
    try:
        with open(os.path.join(_PLUGIN_DIR, "manifest.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("version") or "?")
    except Exception:
        return "?"

#: 15s 巡检：插件与适配器的启动顺序不确定，客户端重建要重新挂载，配置变更要能还原。
#: 每次巡检只是幂等的 dict/getattr 操作，成本可忽略。
_WATCH_INTERVAL = 15.0

_ALL_EVENTS = (EVENT_GROUP_MESSAGE, EVENT_GROUP_AT_MESSAGE, EVENT_C2C_MESSAGE)
_HANDLERS = ("on_group_message_create", "on_group_at_message_create", "on_c2c_message_create")


def _group_names_path():
    """群名缓存落盘位置（与 identities.json 同目录）。"""
    try:
        from core.utils.path_utils import get_config_path

        return str(get_config_path() / "plugins" / _SELF_PLUGIN_ID / "groups.json")
    except Exception:
        try:
            return os.path.join(_PLUGIN_DIR, "data", "groups.json")
        except Exception:
            return None


def _identity_path():
    """Where to persist the auto-learned nickname directory."""
    try:
        from core.utils.path_utils import get_config_path

        return str(get_config_path() / "plugins" / _SELF_PLUGIN_ID / "identities.json")
    except Exception:
        try:
            return os.path.join(_PLUGIN_DIR, "data", "identities.json")
        except Exception:
            return None


class QQOfficialGroupBridge(BasePlugin):
    """把 QQ 官方机器人的群/单聊事件对齐成 KiraAI 标准语义。"""

    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)

        basic = cfg.get("section_basic", {}) or {}
        self.enabled = bool(basic.get("enabled", True))
        self.mention_mode = str(basic.get("mention_mode", "auto") or "auto").lower()
        try:
            self.dedup_ttl = max(10.0, float(basic.get("dedup_ttl", 180)))
        except (TypeError, ValueError):
            self.dedup_ttl = 180.0
        self.unify_at = bool(basic.get("unify_at_messages", True))
        self.unify_dm = bool(basic.get("unify_direct_messages", True))
        try:
            self.at_grace = max(0.0, float(basic.get("at_grace_seconds", 0)))
        except (TypeError, ValueError):
            self.at_grace = 0.0
        self.remember_nicknames = bool(basic.get("remember_nicknames", True))
        self.resolve_at = bool(basic.get("resolve_at_markup", True))
        self.learn_self_openid = bool(basic.get("learn_self_openid", True))
        self.reply_to_self_wakes = bool(basic.get("reply_to_self_wakes", True))
        self.quote_reply = bool(basic.get("quote_reply", True))
        self.send_at_mention = bool(basic.get("send_at_mention", True))
        self.enhance_rich = bool(basic.get("enhance_rich_content", True))
        self.at_markup_style = str(basic.get("at_markup_style", "legacy") or "legacy").lower()
        self.at_markdown = bool(basic.get("at_markdown", True))
        self.pinned_self_openid = str(basic.get("self_openid", "") or "").strip()
        # ---- 新增：群名 / markdown / 键盘 / 互动 / 群管理工具（默认全开） ----
        self.group_name_enabled = bool(basic.get("group_name_enabled", True))
        self.markdown_enabled = bool(basic.get("markdown_enabled", True))
        self.keyboard_enabled = bool(basic.get("keyboard_enabled", True))
        self.interaction_enabled = bool(basic.get("interaction_enabled", True))
        # ---- v1.3.3：按"是否需要群管理权限"分成两组 ----
        # 原则（用户约定）：不需要权限的默认开；需要权限的默认关。
        # ⚠ 存量用户不受影响：核心只在「配置里没有这个键」时才填默认值
        #   （plugin_registry._ensure_plugin_config），已保存的值一律保留。
        member = cfg.get("section_member", {}) or {}
        admin = cfg.get("section_admin", {}) or {}
        self.group_info_enabled = bool(member.get("group_info_enabled", True))
        self.member_query_enabled = bool(member.get("member_query_enabled", True))
        self.member_notice_enabled = bool(member.get("member_notice_enabled", True))
        # ⚠ 加群申请事件**需要机器人是群管理员**（官方原文：
        #   "只有当机器人是群管理员时才可以收到此事件"）⇒ 归入管理组、默认关。
        self.join_request_notice_enabled = bool(
            admin.get("admin_join_request_notice", False))
        self.receive_files = bool(member.get("receive_files", True))

        # 群管理总闸（默认关）+ 各细项
        self.admin_tools_enabled = bool(admin.get("admin_tools_enabled", False))
        self.admin_mute = bool(admin.get("admin_mute", False))
        self.admin_mute_state = bool(admin.get("admin_mute_state", False))
        self.admin_join_approval = bool(admin.get("admin_join_approval", False))
        self.admin_recall_others = bool(admin.get("admin_recall_others", False))
        self.admin_member_roster = bool(admin.get("admin_member_roster", False))
        self.admin_kick = bool(admin.get("admin_kick", False))
        self.admin_blacklist = bool(admin.get("admin_blacklist", False))
        #: 多订阅两个 intent 位（成员事件 1<<24 / 互动回调 1<<26）。
        #: **默认关**：多订阅若被平台拒绝，botpy 会 _can_reconnect=False 反复失败，
        #: 那会连"能收消息"这个基本盘一起搞挂。单独开关 + 自愈还原。
        self.extra_intents = bool(basic.get("extra_intents", True))

        proactive = cfg.get("section_proactive", {}) or {}
        self.proactive_enabled = bool(proactive.get("proactive_enabled", True))
        try:
            self.proactive_min_interval = max(0.0, float(proactive.get("proactive_min_interval", 0)))
        except (TypeError, ValueError):
            self.proactive_min_interval = 0.0

        self.dedup = MessageDedup(ttl=self.dedup_ttl)
        self.identities = IdentityStore(path=_identity_path() if self.remember_nicknames else None)
        #: 把通讯录挂到 ctx 上，供 L1 的「按名字找人」工具读取
        #   （工具是独立类，只拿得到 ctx；挂载失败不影响主流程）
        try:
            if self.ctx is not None:
                setattr(self.ctx, "_bridge_identities", self.identities)
        except Exception:
            pass

        self._task = None
        self._stop = asyncio.Event()
        self._patch_state = {}
        self._sample_logged = False
        self._patched = set()
        self._broken_adapters = set()
        self._patched_sends = {}
        self._restore_reported = False
        self._handled = 0
        #: 观测计数：同一条消息同时以「全量+@」两种事件到达的次数（正常恒为 0）
        self._cross_pairs = 0
        #: (sid, 展示态 message_id) -> REFIDX，用于「引用回复」
        self._api_patched: dict = {}
        self._text_originals: dict = {}
        #: 每个适配器实例 → 机器人自己的身份（OpenID / 昵称）
        self._self_ident = {}
        self._self_logged = set()
        self._at_sample_logged = False
        self._rich_logged = set()
        self._ref_diag_done = False
        self._ref_miss = 0
        self._quote_miss_logged = False
        self._llm_markup_logged = False
        self._at_md_logged = False
        self._at_md_fallback_logged = False
        self._ref_logged = 0
        self._last_proactive = {}
        #: 仅用于日志观测（今日主动消息条数），不做任何限制——配额由官方判
        self._proactive_day = ""
        self._proactive_count = 0
        self._last_report = ""

        # ---- 新增子系统 ----
        #: 世代档案：适配器名 -> CoreProfile
        self.profiles: dict = {}
        #: 群名缓存（白名单接口，失败自动降级）
        self.group_names = GroupInfoCache(
            path=_group_names_path() if self.group_name_enabled else None
        )
        #: api 层发送补丁（markdown / keyboard / 引用）
        self.api_send = ApiSendPatcher(self, logger)
        #: 3.0 增量增强（群名 + 引用唤醒补丁）
        self.v3 = V3Enhancer(self, logger)
        #: 互动回调
        self.interactions = InteractionBridge(self, logger)
        #: 已安装 api 补丁的适配器名
        self._api_send_installed: set = set()
        #: 机器人身份（按群隔离，供 3.0 引用判据用）：target -> {"openid":…, "name":…}
        self._self_ident_by_target: dict = {}
        #: 互动事件计数（仅观测）
        self._interactions_seen = 0
        #: 成员/加群申请事件计数
        self._member_events = 0
        #: intent 自愈：记录最近一次注入时间，用于失败回退
        self._intents_patched_at = 0.0
        self._intents_reverted = False

        # ★ 立刻装 `botpy.Client.start` 补丁（**早于任何 await**）。
        #   核心的启动顺序是「适配器先连、插件后加载」（lifecycle.py:153-233），
        #   等到 initialize() 里的巡检才装就已经晚了 —— 首连拿不到额外订阅位。
        #   这里在构造函数里装，覆盖"插件加载之后才连接"的全部场景。
        if self.extra_intents:
            try:
                self._install_intent_patches()
            except Exception as exc:  # pragma: no cover
                logger.debug("[QQBOT-BRIDGE] 预装 intent 补丁失败（忽略）: %s", exc)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self):
        # 无论启用与否都跑巡检：禁用时负责把补丁**还原**干净（可逆，不需要重启）。
        self._task = asyncio.create_task(self._watch_loop(), name="qqbot-fullmsg-bridge")
        await self._tick(report=True)
        if self.enabled:
            logger.info(
                "[QQBOT-BRIDGE] v%s 已启动：全量群消息=开；统一@消息=%s；统一单聊=%s；"
                "@事件等待窗口=%.1fs（0=默认不等待）；引用回复=%s；@标记形态=%s；主动消息通道=%s",
                _plugin_version(),
                "开" if self.unify_at else "关",
                "开" if self.unify_dm else "关",
                self.at_grace,
                "开" if self.quote_reply else "关",
                self.at_markup_style,
                "开" if self.proactive_enabled else "关",
            )
            logger.info(
                "[QQBOT-BRIDGE] 无需权限的能力：群信息=%s；按名字找人=%s；读文件=%s；"
                "成员进出通知=%s（成员进出事件不需要管理员）；额外订阅位=%s",
                "开" if self.group_info_enabled else "关",
                "开" if self.member_query_enabled else "关",
                "开" if self.receive_files else "关",
                "开" if self.member_notice_enabled else "关",
                "开" if self.extra_intents else "关",
            )
            logger.info(
                "[QQBOT-BRIDGE] 需管理员的能力（总闸=%s）：禁言=%s；禁言查询=%s；"
                "加群申请提醒=%s；加群审批=%s；成员名册=%s；踢人=%s；黑名单=%s",
                "开" if self.admin_tools_enabled else "关",
                "开" if self.admin_mute else "关",
                "开" if self.admin_mute_state else "关",
                "开" if self.join_request_notice_enabled else "关",
                "开" if self.admin_join_approval else "关",
                "开" if self.admin_member_roster else "关",
                "开" if self.admin_kick else "关",
                "开" if self.admin_blacklist else "关",
            )
            logger.info(
                "[QQBOT-BRIDGE] 增强能力：群名=%s；markdown=%s；键盘=%s；互动回调=%s；"
                "群管理总闸=%s；成员事件=%s；额外订阅位(需重启+可能被平台拒)=%s",
                "开" if self.group_name_enabled else "关",
                "开" if self.markdown_enabled else "关",
                "开" if self.keyboard_enabled else "关",
                "开" if self.interaction_enabled else "关",
                "开" if self.admin_tools_enabled else "关",
                "开" if self.member_notice_enabled else "关",
                "开" if self.extra_intents else "关",
            )
            if not self.extra_intents:
                logger.info(
                    "[QQBOT-BRIDGE] 提示：成员进出 / 加群申请 事件需要打开配置项 "
                    "extra_intents（默认关，为避免个别环境下多订阅被平台拒导致连接反复失败）"
                )
        else:
            logger.info("[QQBOT-BRIDGE] 桥接已禁用（section_basic.enabled=false），已还原既有补丁")

    async def terminate(self):
        self._stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self._flush_identities(force=True)
        await self._flush_group_names(force=True)
        logger.info(
            "[QQBOT-BRIDGE] 已停止（本次已处理 %d 条消息，其中 %d 条出现「全量+@」双副本，通常为 0；"
            "补丁保留给热重载，要彻底还原请把 enabled 设为 false 或重启 KiraAI）",
            self._handled, self._cross_pairs,
        )

    async def _watch_loop(self):
        while not self._stop.is_set():
            try:
                await asyncio.sleep(_WATCH_INTERVAL)
            except asyncio.CancelledError:
                raise
            try:
                await self._tick()
            except Exception as exc:
                logger.debug("[QQBOT-BRIDGE] 巡检异常（忽略）: %s", exc)
            try:
                self._health_check_intents()
            except Exception as exc:
                logger.debug("[QQBOT-BRIDGE] intent 健康检查异常（忽略）: %s", exc)
            await self._flush_identities()
            await self._flush_group_names()

    async def _flush_group_names(self, force: bool = False):
        """群名缓存落盘 —— 同样走线程，不占事件循环。"""
        if not self.group_name_enabled:
            return
        if not force and not self.group_names.dirty:
            return
        try:
            await asyncio.to_thread(self.group_names.save)
        except Exception as exc:
            logger.debug("[QQBOT-BRIDGE] 群名缓存落盘失败（忽略）: %s", exc)

    async def _flush_identities(self, force: bool = False):
        """落盘昵称通讯录 —— 走线程，绝不占用事件循环。"""
        if not self.remember_nicknames:
            return
        if not force and not self.identities.dirty:
            return
        try:
            await asyncio.to_thread(self.identities.save)
        except Exception as exc:
            logger.debug("[QQBOT-BRIDGE] 昵称通讯录落盘失败（忽略）: %s", exc)

    # ------------------------------------------------------------------ #
    # 巡检 / 补丁安装与还原
    # ------------------------------------------------------------------ #
    async def _tick(self, report: bool = False):
        if not self.enabled:
            self._restore_all()
            return
        adapters = self._find_adapters()
        if not adapters:
            if report:
                logger.info("[QQBOT-BRIDGE] 暂未发现 QQ Official 适配器实例（插件可能先于适配器加载）")
            return
        # ⚠ 顺序很重要：**先探测世代**，再决定要不要补 botpy 解析器。
        #   3.0 的核心已自带全量群消息（`_install_message_parsers`），桥接若还在
        #   `ConnectionState` 类上注册解析器，属于多余的全局副作用。
        for name, adapter in adapters:
            profile = detect_profile(adapter)
            if profile.is_known:
                self.profiles[name] = profile
        patches = {}
        if any(getattr(p, "is_v2", False) for p in self.profiles.values()
               if isinstance(p, CoreProfile)):
            patches = self._ensure_class_patch()
        for name, adapter in adapters:
            self._attach(adapter, name, patches)
        names = ",".join(n for n, _ in adapters)
        if report or self._last_report != names:
            self._last_report = names
            logger.info("[QQBOT-BRIDGE] 已挂载到适配器: %s", ", ".join(n for n, _ in adapters))

    @staticmethod
    def _connection_state_cls():
        try:
            import botpy.connection as _conn  # noqa: WPS433

            return _conn.ConnectionState
        except Exception:
            return None

    def _ensure_class_patch(self):
        """Register (or restore) the parsers we own on botpy's ConnectionState."""
        cls = self._connection_state_cls()
        if cls is None:
            if self._patch_state.get("_import") != "unavailable":
                self._patch_state["_import"] = "unavailable"
                logger.warning("[QQBOT-BRIDGE] 未安装 qq-botpy，桥接无法工作")
            return {}

        # (event, force_override, wanted)
        plan = [
            (EVENT_GROUP_MESSAGE, False, True),
            (EVENT_GROUP_AT_MESSAGE, True, bool(self.unify_at)),
            (EVENT_C2C_MESSAGE, True, bool(self.unify_dm)),
        ]
        patches = {}
        for event_name, force, wanted in plan:
            if wanted:
                state = install_class_parser(cls, event_name, force=force)
            else:
                state = "restored" if restore_class_parser(cls, event_name) else "untouched"
            patches[event_name] = state
            prev = self._patch_state.get(event_name)
            self._patch_state[event_name] = state
            if state == "patched" and prev != "patched":
                logger.info(
                    "[QQBOT-BRIDGE] 已为 botpy.ConnectionState 注册 parse_%s（%s）",
                    event_name,
                    "这是 `_parser unknown event group_message_create` 的直接修复"
                    if event_name == EVENT_GROUP_MESSAGE
                    else "改用原始 payload 以保留昵称/引用",
                )
            elif state == "foreign" and prev != "foreign":
                logger.info("[QQBOT-BRIDGE] botpy 已自带 %s 解析器且不该覆盖，桥接对该事件让位", event_name)
            elif state == "restored" and prev not in (None, "restored"):
                logger.info("[QQBOT-BRIDGE] 已还原 parse_%s 为框架原生实现", event_name)
        return patches

    def _restore_all(self):
        """Put every patch back (used when the plugin is switched off)."""
        changed = []
        cls = self._connection_state_cls()
        if cls is not None:
            for event_name in _ALL_EVENTS:
                if restore_class_parser(cls, event_name):
                    changed.append("parse_" + event_name)
        for name, adapter in self._find_adapters():
            try:
                client = adapter.get_client()
            except Exception:
                client = None
            if client is not None:
                state = getattr(getattr(client, "_connection", None), "state", None)
                for attr in _HANDLERS:
                    if detach_client_handler(client, attr):
                        changed.append(f"{name}.{attr}")
                if state is not None:
                    for event_name in _ALL_EVENTS:
                        drop_live_parser(state, event_name)
            original = self._patched_sends.pop(name, None)
            if original is not None:
                adapter._send_message = original
                changed.append(f"{name}._send_message")
            text_orig = self._text_originals.pop(name, None)
            if text_orig is not None:
                adapter._text_content = text_orig
                changed.append(f"{name}._text_content")
            # 发送增强（api 层）
            for api, method_name, orig in self._api_patched.pop(name, []):
                try:
                    setattr(api, method_name, orig)
                    changed.append(f"{name}.api.{method_name}")
                except Exception:
                    pass
            if self.api_send.restore(name):
                self._api_send_installed.discard(name)
                changed.append(f"{name}.api_send")
            # 发送入口包装
            self._unpatch_send_entry(adapter, name)
            # 互动回调（标记与 qqbot_bridge.attach_client_handler 统一，便于还原）
            current = getattr(client, "on_interaction_create", None)
            if callable(current) and detach_client_handler(client, "on_interaction_create"):
                changed.append(f"{name}.on_interaction_create")
            for _event in MEMBER_EVENTS:
                if detach_client_handler(client, "on_" + _event):
                    changed.append(f"{name}.on_{_event}")
            # 3.0 增量
            if self.v3.restore(name):
                changed.append(f"{name}.v3")
        # intent 扩展
        self._revert_extra_intents()
        if changed and not self._restore_reported:
            self._restore_reported = True
            logger.info("[QQBOT-BRIDGE] 已还原 %d 处补丁：%s", len(changed), ", ".join(changed[:6]))

    # ------------------------------------------------------------------ #
    # 插件钩子：工具 / 标签注入（L1 工具层 + markdown & keyboard 标签）
    # ------------------------------------------------------------------ #
    def _profile_for(self, event) -> CoreProfile:
        name = str(getattr(getattr(event, "adapter", None), "name", "") or "")
        profile = self.profiles.get(name)
        if isinstance(profile, CoreProfile):
            return profile
        try:
            adapter = self.ctx.adapter_mgr.get_adapter(name)
        except Exception:
            adapter = None
        profile = detect_profile(adapter)
        if profile.is_known:
            self.profiles[name] = profile
        return profile

    def _is_qq_official_event(self, event) -> bool:
        platform = str(getattr(getattr(event, "adapter", None), "platform", "") or "")
        if platform == "QQ Official":
            return True
        name = str(getattr(getattr(event, "adapter", None), "name", "") or "")
        return name in self.profiles and isinstance(self.profiles.get(name), CoreProfile)

    def inject_tools_and_tags(self, event, request, tag_set) -> None:
        """ON_LLM_REQUEST 钩子体（两版实参一致：event, request, tag_set）。"""
        if not self.enabled or request is None or tag_set is None:
            return
        if not self._is_qq_official_event(event):
            return

        # ---- L1 工具（跨世代通用：Route 直发，不依赖任何补丁）----
        #   分层：不需要权限的始终按开关注入；需要管理员权限的还受总闸约束。
        try:
            tool_set = getattr(request, "tool_set", None)
            if tool_set is not None:
                for cls in build_admin_tools({
                    # 无需权限
                    "recall_enabled": True,
                    "group_info_enabled": self.group_info_enabled,
                    "member_query_enabled": self.member_query_enabled,
                    "receive_files": self.receive_files,
                    "bot_state_enabled": True,
                    # 需要管理员（再受总闸约束）
                    "admin_tools_enabled": self.admin_tools_enabled,
                    "mute_enabled": self.admin_mute,
                    "mute_state_enabled": self.admin_mute_state,
                    "join_approval_enabled": self.admin_join_approval,
                    "roster_enabled": self.admin_member_roster,
                    "kick_enabled": self.admin_kick,
                    "blacklist_enabled": self.admin_blacklist,
                }):
                    tool_set.add(cls(ctx=self.ctx))
        except Exception as exc:
            logger.debug("[QQBOT-BRIDGE] 注入 L1 工具失败: %s", exc)

        # ---- markdown / keyboard 标签（description 会进 format 提示词）----
        for cls, desc, wanted in (
            (MarkdownTag, MARKDOWN_TAG_DESCRIPTION, self.markdown_enabled),
            (KeyboardTag, KEYBOARD_TAG_DESCRIPTION, self.keyboard_enabled),
        ):
            if not wanted:
                continue
            try:
                tag_set.register(cls(self.ctx, desc))
            except Exception as exc:
                logger.debug("[QQBOT-BRIDGE] 注册标签 %s 失败: %s", cls.__name__, exc)

        # ---- 3.0 增量（群名 / 引用唤醒补洞），幂等 ----
        profile = self._profile_for(event)
        if profile.is_v3:
            self._attach_v3(event, profile)

    def _attach_v3(self, event, profile: CoreProfile) -> None:
        name = str(getattr(getattr(event, "adapter", None), "name", "") or "")
        if not name:
            return
        holders = self.profiles.setdefault("__inst__", {})
        try:
            adapter = self.ctx.adapter_mgr.get_adapter(name)
        except Exception:
            return
        if adapter is None:
            return
        if holders.get(name) is adapter and self.v3.installed(name):
            return
        holders[name] = adapter
        if self.v3.install(adapter, name, profile):
            logger.info(
                "[QQBOT-BRIDGE] %s：已装好 3.0 增量（群名=%s；引用唤醒补洞=%s）",
                name,
                "开" if self.group_name_enabled else "关",
                "开" if self.reply_to_self_wakes else "关",
            )

    # ------------------------------------------------------------------ #
    # 机器人身份 / 引用索引（供 3.0 判据与引用注入用）
    # ------------------------------------------------------------------ #
    def self_identity_for(self, target_id: str) -> dict:
        return self._self_ident_by_target.get(str(target_id), {}) or {}

    def remember_self_identity(self, target_id: str, openid=None, name=None) -> None:
        if not target_id:
            return
        item = self._self_ident_by_target.setdefault(str(target_id), {})
        if openid and not item.get("openid"):
            item["openid"] = str(openid)
        if name and not item.get("name"):
            item["name"] = str(name)

    def remember_sent_ref(self, adapter, target_id: str, sent_id: str,
                          ref_idx: str, is_group: bool) -> None:
        """记录机器人自己发出消息的 REFIDX（以后才能引用它）。"""
        try:
            display = adapter._display_message_id(str(sent_id))
        except Exception:
            return
        sid = f"{getattr(adapter.info, 'name', '?')}:{'gm' if is_group else 'dm'}:{target_id}"
        self._remember_ref(adapter, sid, display, ref_idx)

    # ------------------------------------------------------------------ #
    # 合成事件（互动回调 / 成员进出）
    # ------------------------------------------------------------------ #
    def publish_synthetic_event(self, *, target_id: str, sender_id: str, is_group: bool,
                                text: str, is_notice: bool = True,
                                target_holder: dict = None) -> bool:
        """把非消息类事件（按钮点击 / 成员进出）转成一条标准 Kira 事件。

        `is_notice=True` 与核心 `PluginContext.publish_notice` 同语义；内置 kira-ai
        插件对 notice 有专门的消息格式化分支。
        """
        try:
            from core.chat import Group, User, MessageChain
            from core.chat.message_elements import Text
            from core.chat.message_utils import KiraIMMessage, KiraMessageEvent
        except Exception as exc:
            logger.debug("[QQBOT-BRIDGE] 合成事件导入失败: %s", exc)
            return False

        holders = self.profiles.get("__inst__", {})
        for adapter_name, adapter in self._find_adapters():
            adapter = (target_holder or {}).get("adapter") or holders.get(adapter_name) or adapter
            profile = self.profiles.get(adapter_name)
            if not isinstance(profile, CoreProfile):
                profile = detect_profile(adapter)
            types = message_types_of(adapter, profile) or ["text"]
            ts = int(time.time())
            try:
                event = KiraMessageEvent(
                    adapter=adapter.info,
                    message_types=list(types),
                    message=KiraIMMessage(
                        timestamp=ts,
                        group=Group(
                            group_id=str(target_id),
                            group_name=self.group_names.lookup(adapter_name, str(target_id))
                            or str(target_id),
                        ) if is_group else None,
                        sender=User(user_id=str(sender_id), nickname=None),
                        is_mentioned=True,
                        is_notice=is_notice,
                        message_id="",
                        self_id=getattr(adapter, "app_id", None),
                        chain=MessageChain([Text(text)]),
                    ),
                    timestamp=ts,
                )
            except Exception as exc:
                logger.debug("[QQBOT-BRIDGE] 构造合成事件失败: %s", exc)
                return False
            try:
                adapter.publish(event)
                return True
            except Exception as exc:
                logger.debug("[QQBOT-BRIDGE] 发布合成事件失败: %s", exc)
                return False
        return False

    def _find_adapters(self):
        found = []
        try:
            adapters = self.ctx.adapter_mgr.get_adapters()
        except Exception:
            return found
        for name, adapter in list(adapters.items()):
            if self._is_qq_official(adapter):
                found.append((str(name), adapter))
        return found

    @staticmethod
    def _is_qq_official(adapter) -> bool:
        cls = type(adapter)
        if "qq_official" in str(getattr(cls, "__module__", "")):
            return True
        if cls.__name__ == "QQOfficialAdapter":
            return True
        # 结构性兜底（框架改了包名也能认出来）
        return all(
            hasattr(adapter, attr)
            for attr in ("app_id", "app_secret", "_group_reply_ids", "get_client", "_handle_group_message")
        )

    def _attach(self, adapter, name: str, patches: dict):
        """把补丁挂到一个适配器上（幂等，按世代选落点）。

        世代分工（详见 core_profiles）：
        * **2.x**：核心缺东西 —— 补解析器 + 接管三条事件 + 补发送链；
        * **3.0**：核心已做完 —— **不接管、不造事件**，只装 api 层发送增强、
          3.0 增量（群名 / 引用补洞）、互动回调和 L1 工具；
        * **unknown**：只装 api 层 + L1 工具（两者都不依赖世代落点）。
        """
        try:
            client = adapter.get_client()
        except Exception as exc:
            logger.debug("[QQBOT-BRIDGE] %s: get_client() 失败: %s", name, exc)
            return
        if client is None:
            return

        profile = detect_profile(adapter)
        if profile.is_known:
            self.profiles[name] = profile
        conn = getattr(client, "_connection", None)
        state = getattr(conn, "state", None)

        # ---- L3（发送增强）：无条件安装，两版结构一致 ----
        self._install_api_send(adapter, name, client)
        if self.markdown_enabled or self.keyboard_enabled or self.quote_reply:
            self._patch_send_entry(adapter, name)

        # ---- 互动回调（INTERACTION_CREATE）：两版都需要 ----
        if self.interaction_enabled:
            res = self.interactions.install(client)
            if res == "attached":
                logger.info("[QQBOT-BRIDGE] %s: 已挂载互动回调处理器", name)

        # ---- intent 扩展（成员事件 / 互动回调位）----
        self._apply_extra_intents(adapter, name, client)

        # ---- 成员事件解析器（需要 intent 1<<24；缺解析器时补上）----
        if self.extra_intents and self.member_notice_enabled and state is not None:
            for event_name in MEMBER_EVENTS:
                if inject_live_parser(state, event_name, force=False):
                    logger.info("[QQBOT-BRIDGE] %s: 已为成员事件 %s 注入解析器", name, event_name)
            for event_name in MEMBER_EVENTS:
                attach_client_handler(
                    client, "on_" + event_name,
                    self._make_member_handler(adapter, name, event_name),
                    allow_shadow=False, owner=self,
                )

        if profile.generation == GEN_UNKNOWN:
            if name not in self._broken_adapters:
                self._broken_adapters.add(name)
                logger.warning(
                    "[QQBOT-BRIDGE] %s: 认不出核心世代（%s）—— 只启用"
                    "「群管理工具 + markdown/键盘 + 引用」这些不依赖核心内部结构的增强；"
                    "事件层与群名显示保持不变。若功能异常请到插件仓库反馈",
                    name, profile.detail,
                )
            return

        if profile.is_v3:
            # 3.0：核心已自带全量群消息/昵称/@/引用/去重 —— 桥接**绝不接管、绝不造事件**
            self.v3.install(adapter, name, profile)
            if name not in self._patched:
                self._patched.add(name)
                logger.info(
                    "[QQBOT-BRIDGE] %s: 桥接就绪（KiraAI %s：核心已自带全量群消息与真昵称，"
                    "桥接只做增量 —— 群名 / markdown / 键盘 / 互动 / 群管理工具）",
                    name, profile.generation,
                )
            return

        # ---- 以下仅 2.x ----
        missing = check_adapter_capabilities(adapter)
        if missing:
            if name not in self._broken_adapters:
                self._broken_adapters.add(name)
                logger.error(
                    "[QQBOT-BRIDGE] %s: 2.x 适配器缺少必要接口 %s —— 事件层停用"
                    "（发送增强与工具层仍可用）。通常是 KiraAI core 版本变动导致，"
                    "请到插件仓库反馈",
                    name, missing,
                )
            return

        # (event, handler attr, kind, is_group, wanted, allow_shadow)
        #
        # ⚠ allow_shadow 的取舍（这里踩过两个方向的坑，说明白）：
        #   * **2.x 必须为 True**：核心自带 on_group_at_message_create /
        #     on_c2c_message_create，但实现是 `nickname = OpenID` —— 顶掉它
        #     正是本插件的核心价值（真昵称修复）。设 False 会静默丢掉这个功能。
        #   * **3.0 必须为 False**：核心实现已完整（真昵称 + @ 解析 + 引用 + 去重），
        #     桥接若顶掉它，就会用 2.x 的字段名造事件 ⇒ 所有 @ 消息静默丢失。
        #   由于 3.0 在上面的分支里已提前 return，不会走到这里；这里再显式限定
        #   为「仅 v2」，双保险。
        allow_shadow_events = profile.is_v2
        plan = [
            (EVENT_GROUP_MESSAGE, "on_group_message_create", KIND_FULL, True,
             True, allow_shadow_events),
            (EVENT_GROUP_AT_MESSAGE, "on_group_at_message_create", KIND_AT, True,
             bool(self.unify_at), allow_shadow_events),
            (EVENT_C2C_MESSAGE, "on_c2c_message_create", KIND_DM, False,
             bool(self.unify_dm), allow_shadow_events),
        ]

        newly_attached = []
        for event_name, attr, kind, is_group, wanted, allow_shadow in plan:
            if not wanted:
                # 关掉某个 unify 开关 -> 把该事件还给框架原生实现（否则原生处理器
                # 会收到原始 dict 而静默丢消息）。
                if detach_client_handler(client, attr):
                    logger.info("[QQBOT-BRIDGE] %s: %s 已还原为框架原生实现", name, attr)
                if state is not None:
                    drop_live_parser(state, event_name)
                continue
            # 顺序很重要：先挂处理器，再强制替换解析器。
            res = attach_client_handler(
                client, attr, self._make_handler(adapter, name, kind, is_group),
                allow_shadow=allow_shadow, owner=self,
            )
            if res == "attached":
                newly_attached.append(attr)
            elif res == "native":
                logger.info("[QQBOT-BRIDGE] %s: %s 已由框架原生实现，桥接让位", name, attr)

        if state is not None:
            # 与上面同一取舍：2.x 要接管（force），其它世代让位
            live_plan = [
                (EVENT_GROUP_MESSAGE, allow_shadow_events, True),
                (EVENT_GROUP_AT_MESSAGE, allow_shadow_events, bool(self.unify_at)),
                (EVENT_C2C_MESSAGE, allow_shadow_events, bool(self.unify_dm)),
            ]
            for event_name, forced, wanted in live_plan:
                if not wanted:
                    continue
                if inject_live_parser(state, event_name, force=forced):
                    logger.info("[QQBOT-BRIDGE] %s: 运行中解析表已注入 %s", name, event_name)

        if newly_attached:
            logger.info("[QQBOT-BRIDGE] %s: 已挂载 %s", name, ", ".join(newly_attached))
        if name not in self._patched and (
            newly_attached or patches.get(EVENT_GROUP_MESSAGE) in ("patched", "already")
        ):
            self._patched.add(name)
            logger.info(
                "[QQBOT-BRIDGE] %s: 桥接就绪（全量群消息%s%s；昵称取真实 QQ 昵称）",
                name,
                " + @消息" if self.unify_at else "",
                " + 单聊" if self.unify_dm else "",
            )
        # 2.x：发送链补丁挂在 adapter._send_message 上（3.0 没有这个方法）
        if self.proactive_enabled or self.quote_reply or self.send_at_mention:
            self._patch_send_path(adapter, name, client)

    # ------------------------------------------------------------------ #
    # 事件 -> KiraAI
    # ------------------------------------------------------------------ #
    def _make_handler(self, adapter, name: str, kind: str, is_group: bool):
        async def _handler(payload):
            try:
                await self._on_event(adapter, name, payload, kind, is_group)
            except Exception as exc:  # 绝不把异常抛回 botpy 的事件循环
                logger.warning("[QQBOT-BRIDGE] 处理事件失败（%s）: %s: %s", kind, type(exc).__name__, exc)

        return _handler

    async def _on_event(self, adapter, name: str, payload, kind: str, is_group: bool):
        if not self.enabled:
            return
        body = normalize_body(payload) or {}
        raw_content = body.get("content")
        rich_notes: list = []
        ident = self._self_identity(adapter, name)
        key = dedup_key(body, is_group)

        # 官方文档：全量模式下「群里的每一条消息（不限于@机器人）」都走
        # GROUP_MESSAGE_CREATE；@ 消息在开启全量后不再单独走 AT 事件
        # （AstrBot#8131 的现象也印证：只挂 AT 处理器的适配器在全量模式下连 @ 都收不到）。
        # 所以正常情况下这里**不会**出现跨事件重复。万一真出现（文档没承诺过），
        # 策略是「绝不丢唤醒」：@ 副本照常放行并告警——想消除重复把 at_grace_seconds 设 1.5。
        if kind == KIND_AT and is_group and key and self.dedup.kind_of(key) == KIND_FULL:
            self._cross_pairs += 1
            if self._cross_pairs == 1:
                logger.warning(
                    "[QQBOT-BRIDGE] 检测到同一条消息同时以「全量」和「@」两种事件到达"
                    "（官方文档未说明会这样）。已按「绝不丢唤醒」放行两条；"
                    "若上下文里看到重复，把 at_grace_seconds 设为 1.5 即可消除"
                )

        group_name = None
        if is_group:
            try:
                group_name = self._fill_group_name_v2(adapter, name, body)
            except Exception:
                group_name = None
        try:
            event, reason = build_event(
                adapter,
                body,
                Group=Group,
                User=User,
                KiraIMMessage=KiraIMMessage,
                KiraMessageEvent=KiraMessageEvent,
                kind=kind,
                is_group=is_group,
                force_mention=(kind == KIND_AT or not is_group),
                mention_mode=self.mention_mode,
                self_ids=collect_self_ids(adapter.get_client()),
                dedup=self.dedup,
                identities=self.identities if self.remember_nicknames else None,
                self_identity=ident,
                resolve_at=self.resolve_at,
                learn_self=self.learn_self_openid,
                reply_to_self_wakes=self.reply_to_self_wakes,
                enhance_rich=self.enhance_rich,
                notes=rich_notes,
                At=At,
                Text=Text,
                group_name=group_name,
            )
        except Exception as exc:
            logger.warning("[QQBOT-BRIDGE] 构造事件失败（%s）: %s: %s", kind, type(exc).__name__, exc)
            return

        if event is None:
            if reason != "duplicate":
                logger.debug("[QQBOT-BRIDGE] 事件被丢弃（%s）: %s", kind, reason)
            return

        try:
            if is_group and ident.openid:
                self.remember_self_identity(
                    str(body.get("group_openid") or ""), ident.openid, ident.name
                )
        except Exception:
            pass
        self._log_self_learned(name, ident)
        self._log_at_sample_once(raw_content, body, ident, event)
        for note in rich_notes:
            if note not in self._rich_logged:
                self._rich_logged.add(note)
                logger.info("[QQBOT-BRIDGE] 富内容归一化首次生效: %s", note)
        if self.quote_reply:
            ref = extract_msg_idx(body)
            if ref:
                sid = str(getattr(event.session, "sid", "") or "")
                display = str(event.message.message_id or "")
                self._remember_ref(adapter, sid, display, ref)
                # 模型有时会回原始 id（框架某些路径渲染的是它）——两个键都记
                raw_id = str(body.get("id") or "")
                if raw_id and raw_id != display:
                    self._remember_ref(adapter, sid, raw_id, ref)
            self._log_ref_diag(body, ref)

        # 严格模式（可选，默认关）：全量副本先压住 at_grace 秒，@ 副本随后到达就让位。
        # 代价是每条群消息都晚 at_grace 秒，所以默认 0 不启用。
        if kind == KIND_FULL and is_group and self.unify_at and self.at_grace > 0 and key:
            await asyncio.sleep(self.at_grace)
            if self._stop.is_set():
                return
            if self.dedup.kind_of(key) == KIND_AT:
                self._cross_pairs += 1
                logger.debug("[QQBOT-BRIDGE] 全量副本被随后到达的 @ 事件取代，已丢弃")
                return

        self._handled += 1
        self._log_sample_once(body, reason, kind)
        try:
            adapter.publish(event)
        except Exception as exc:
            logger.warning("[QQBOT-BRIDGE] 发布事件失败: %s", exc)

    def _self_identity(self, adapter, name: str):
        """取（并顺带刷新）机器人自己的身份。"""
        ident = self._self_ident.get(name)
        if ident is None:
            ident = SelfIdentity()
            self._self_ident[name] = ident
        if not ident.openid and self.pinned_self_openid:
            ident.openid = self.pinned_self_openid
            ident.source = "config"
        if not ident.name:
            try:
                _, robot_name = collect_self_identity(adapter.get_client())
            except Exception:
                robot_name = None
            if robot_name:
                ident.name = str(robot_name)
        return ident

    def _log_self_learned(self, name: str, ident) -> None:
        if not ident.openid or name in self._self_logged:
            return
        self._self_logged.add(name)
        logger.info(
            "[QQBOT-BRIDGE] %s: 已认出机器人自己的 OpenID（来源 %s），昵称「%s」——"
            "此后内容里的 <@自己> 会渲染成 [At %s（你）(pid)] 并强制视为被 @",
            name, ident.source, ident.name or "?", ident.name or "你",
        )

    def _log_at_sample_once(self, raw_content, body, ident, event) -> None:
        """第一次遇到 @ 富文本时把原文/mentions/判定结果如实打出来（便于核对）。"""
        if self._at_sample_logged:
            return
        if not (isinstance(raw_content, str) and "<@" in raw_content):
            return
        self._at_sample_logged = True
        try:
            mentions = json.dumps(normalize_mentions(body.get("mentions")),
                                  ensure_ascii=False)[:500]
        except Exception:
            mentions = "<?>"
        resolved = ""
        try:
            for ele in event.message.chain:
                text = getattr(ele, "text", None)
                if isinstance(text, str):
                    resolved += text
                else:
                    resolved += (getattr(ele, "repr", None) or str(ele))
        except Exception:
            resolved = "<?>"
        logger.info(
            "[QQBOT-BRIDGE] 首次遇到 @ 富文本：原文=%r → 解析后=%r；mentions=%s；"
            "机器人 OpenID=%s（来源 %s）",
            raw_content, resolved[:200], mentions, ident.openid, ident.source,
        )

    def _log_sample_once(self, body, source: str, kind: str):
        if self._sample_logged or kind != KIND_FULL:
            return
        self._sample_logged = True
        try:
            sample = json.dumps(body.get("mentions"), ensure_ascii=False)[:400]
        except Exception:
            sample = "<?>"
        author = body.get("author") if isinstance(body.get("author"), dict) else {}
        logger.info(
            "[QQBOT-BRIDGE] 首条全量群消息：@判定来源=%s；昵称=%r；mentions 原文=%s",
            source, author.get("username"), sample,
        )

    # ------------------------------------------------------------------ #
    # 可选：主动消息通道（官方 bot 无被动窗口时兜底）
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # 发送链路：① 引用回复（message_reference）② 主动消息兜底
    # ------------------------------------------------------------------ #
    _REF_MAX = 1024

    def _remember_ref(self, adapter, sid: str, display_id: str, ref_idx: str) -> None:
        if not display_id or not ref_idx:
            return
        store = ref_store_for(adapter)
        key = (str(sid or ""), str(display_id))
        store[key] = str(ref_idx)
        store.move_to_end(key)
        while len(store) > self._REF_MAX:
            store.popitem(last=False)
        # 头几条打出来：把「记录时的键」和「查找时的键」放一起对比，一眼看出是否同源
        if self._ref_logged < 3:
            self._ref_logged += 1
            logger.info(
                "[QQBOT-BRIDGE] 引用索引 +1：键=(%s, %s) ← %s；当前共 %d 条",
                key[0], key[1], ref_idx, len(store),
            )

    def _quote_ref_for(self, adapter, target_id, chain, is_group):
        """chain 里**显式**带了 Reply 元素时，找出被引用消息的 REFIDX。

        只认显式引用：否则 KiraAI 会把"最后收到的消息"当作回复目标，
        我们若跟着发 message_reference，机器人每条消息都会变成引用上一条 —— 那是刷屏。
        """
        try:
            sid = f"{adapter.info.name}:{'gm' if is_group else 'dm'}:{target_id}"
        except Exception:
            return None
        store = ref_store_for(adapter)
        for ele in chain:
            if not isinstance(ele, Reply):
                continue
            display_id = str(getattr(ele, "message_id", "") or "")
            ref = store.get((sid, display_id))
            if ref:
                return ref
            # 兜底：sid 对不上时按 id 全局找（map 有上限，扫描很便宜）
            for (_, known_id), known_ref in store.items():
                if known_id == display_id:
                    return known_ref
            if not self._quote_miss_logged:
                self._quote_miss_logged = True
                known = ", ".join(sorted({str(k[1]) for k in store})[:6])
                logger.warning(
                    "[QQBOT-BRIDGE] 机器人想引用 %s，但没找到对应的 REFIDX（已知 %d 条：%s）——"
                    "本条按普通回复发出；如持续如此请把本条连同启动日志一起反馈",
                    display_id, len(store), known or "无",
                )
        return None

    def _log_ref_diag(self, body, ref) -> None:
        """一次性诊断：让用户一眼看出 REFIDX 到底有没有、长什么样。"""
        if self._ref_diag_done:
            return
        if ref:
            self._ref_diag_done = True
            logger.info(
                "[QQBOT-BRIDGE] 引用回复：已记录第 1 个 REFIDX（来自 message_scene.ext.msg_idx）"
                "—— 机器人之后可以引用这条消息"
            )
            return
        self._ref_miss += 1
        if self._ref_miss >= 3:
            self._ref_diag_done = True
            try:
                scene = body.get("message_scene")
            except Exception:
                scene = None
            logger.warning(
                "[QQBOT-BRIDGE] 连续 %d 条消息都没有 message_scene.ext.msg_idx —— "
                "机器人将无法「引用回复」。body 顶层键=%s；message_scene=%s",
                self._ref_miss, list(body)[:14], scene,
            )

    # ------------------------------------------------------------------ #
    # L3：发送增强（api 层，两版通用）
    # ------------------------------------------------------------------ #
    def _install_api_send(self, adapter, name: str, client) -> None:
        """把 markdown / keyboard / 引用补丁挂到 `client.api` 上。

        为什么是 api 层：`adapter._send_message` 在 3.0 不存在，
        而 `client.api.post_group_message` 两版结构一致，且 botpy 用
        `payload = locals()` 组装请求体 —— 多传一个 kwarg 就进 JSON（已实测）。
        """
        if name in self._api_send_installed:
            return
        try:
            if self.api_send.install(adapter, name, client):
                self._api_send_installed.add(name)
                logger.info(
                    "[QQBOT-BRIDGE] %s: 已装好发送增强（markdown=%s；键盘=%s；引用=%s；@自动转md=%s）",
                    name,
                    "开" if self.markdown_enabled else "关",
                    "开" if self.keyboard_enabled else "关",
                    "开" if self.quote_reply else "关",
                    "开" if self.at_markdown else "关",
                )
        except Exception as exc:
            logger.warning("[QQBOT-BRIDGE] %s: 安装发送增强失败: %s: %s",
                           name, type(exc).__name__, exc)

    def _patch_send_entry(self, adapter, name: str) -> None:
        """包装 adapter 的 send_group_message / send_direct_message（幂等）。"""
        holder = self.profiles.setdefault("__send_entry__", set())
        if name in holder:
            return
        ok = False
        for method_name, is_group in (("send_group_message", True),
                                      ("send_direct_message", False)):
            current = getattr(adapter, method_name, None)
            if not callable(current):
                continue
            if getattr(current, "_kira_bridge_entry", False):
                continue
            bridge = self
                        # 把 chain 里的 <markdown>/<keyboard> 提取出来（同步、零 I/O）
            async def wrapped(target_id, chain, _orig=current, _is_group=is_group, _name=name):
                md_text = kb = ref = None
                try:
                    if bridge.markdown_enabled or bridge.keyboard_enabled:
                        found_md, found_kb, changed = split_markdown_and_keyboard(chain)
                        if changed:
                            if bridge.markdown_enabled:
                                md_text = found_md
                            if bridge.keyboard_enabled:
                                kb = found_kb
                except Exception as exc:
                    logger.debug("[QQBOT-BRIDGE] 提取 markdown/keyboard 失败: %s", exc)
                if bridge.quote_reply:
                    try:
                        ref = bridge._quote_ref_for(adapter, str(target_id), chain, _is_group)
                    except Exception as exc:
                        logger.debug("[QQBOT-BRIDGE] 解析引用失败: %s", exc)

                md_token = PENDING_MD.set(md_text)
                kb_token = PENDING_KB.set(kb)
                ref_token = QUOTE_REF.set(ref)
                try:
                    return await _orig(target_id, chain)
                finally:
                    try:
                        PENDING_MD.reset(md_token)
                        PENDING_KB.reset(kb_token)
                        QUOTE_REF.reset(ref_token)
                    except Exception:
                        pass

            setattr(wrapped, "_kira_bridge_entry", True)
            setattr(adapter, method_name, wrapped)
            ok = True
        if ok:
            holder.add(name)

    def _unpatch_send_entry(self, adapter, name: str) -> None:
        holder = self.profiles.get("__send_entry__")
        if not holder or name not in holder:
            return
        for method_name in ("send_group_message", "send_direct_message"):
            current = getattr(adapter, method_name, None)
            if getattr(current, "_kira_bridge_entry", False):
                try:
                    delattr(adapter, method_name)
                except Exception:
                    pass
        holder.discard(name)

    # ------------------------------------------------------------------ #
    # intent 扩展（成员事件 / 互动回调位）—— 走 botpy.Client.start 包一层
    # ------------------------------------------------------------------ #
    def _apply_extra_intents(self, adapter, name: str, client) -> None:
        """给 botpy 客户端多订阅两个 intent 位。

        **为什么不在巡检里改**：`client.start()` 内部先 `_bot_login()`（建 session，把
        `self.intents` 写进 `session["intent"]`）再建连接。巡检晚于 start，只能改
        "下一次重连"用的值 —— 首连注定还是旧 intent。

        所以这里包的是 **`botpy.Client.start`（类级）**：进来时 `self.intents` 已赋值、
        session 还没建，改它是零竞态，而且**两版通用**（2.x/3.0 的 `start` 都是先
        `intents` 后 `_bot_login`）。
        """
        if not self.extra_intents:
            return
        # 装类级补丁（幂等；构造函数里已经装过一次）
        self._install_intent_patches()
        # 对**已经连上**的客户端补救：改它的 intents，让下次重连带上新订阅
        self._upgrade_live_client(adapter, name, client)

    #: 额外订阅位：1<<24 成员事件（GROUP_MEMBER_EVENT）/ 1<<26 互动回调（INTERACTION）
    _EXTRA_INTENT_BITS = (1 << 24) | (1 << 26)
    #: 注入后多久开始请重连（给启动流程留时间，避免打扰初始化）
    _RECONNECT_DELAY = 4.0
    #: 还没抓到存活网关时，隔多久再看一次（心跳最长约 45s 一次）
    _RECONNECT_RETRY = 20.0
    _RECONNECT_MAX_WAIT = 300.0

    # ------------------------------------------------------------------ #
    # 补丁安装（三个，都是类级、幂等、可还原）
    # ------------------------------------------------------------------ #
    def _install_intent_patches(self) -> None:
        """装好三个类级补丁：ws_identify（正主）/ send_msg（探针）/ Client.start（顺带）。

        幂等：用函数上的 `_kira_bridge_*` 标记判断是否已装。
        """
        flag = self.profiles.setdefault("__intents__", {})
        if flag.get("patched"):
            return
        try:
            import weakref

            import botpy
            from botpy.gateway import BotWebSocket

            bits = self._EXTRA_INTENT_BITS
            gateways = flag.setdefault("gateways", [])

            # ---- ① 正主：在 intent 真正发出去之前或上订阅位 ----
            orig_identify = BotWebSocket.ws_identify
            if not getattr(orig_identify, "_kira_bridge_intent", False):
                async def ws_identify(gw, _orig=orig_identify):
                    try:
                        sess = getattr(gw, "_session", None)
                        if isinstance(sess, dict):
                            sess["intent"] = int(sess.get("intent") or 0) | bits
                    except Exception:
                        pass
                    return await _orig(gw)

                ws_identify._kira_bridge_intent = True
                ws_identify._kira_bridge_orig = orig_identify
                BotWebSocket.ws_identify = ws_identify

            # ---- ② 探针：借每次心跳/鉴权抓住存活网关（弱引用，不拖住对象） ----
            orig_send = BotWebSocket.send_msg
            if not getattr(orig_send, "_kira_bridge_probe", False):
                async def send_msg(gw, event_json, _orig=orig_send):
                    try:
                        gateways.append(weakref.ref(gw))
                        if len(gateways) > 64:
                            del gateways[:-32]
                    except Exception:
                        pass
                    return await _orig(gw, event_json)

                send_msg._kira_bridge_probe = True
                send_msg._kira_bridge_orig = orig_send
                BotWebSocket.send_msg = send_msg

            # ---- ③ 顺带：新客户端构造/启动时也把 intents 置位，保持状态一致 ----
            orig_start = getattr(botpy.Client, "start", None)
            if callable(orig_start) and not getattr(orig_start, "_kira_bridge_intents", False):
                plugin = self

                async def patched_start(self, appid, secret, ret_coro=False, _orig=orig_start):
                    try:
                        target = getattr(self, "adapter", None)
                        if target is not None and plugin._is_qq_official(target):
                            self.intents = int(getattr(self, "intents", 0) or 0) | bits
                    except Exception as exc:
                        logger.debug("[QQBOT-BRIDGE] 注入 intent 失败: %s", exc)
                    return await _orig(self, appid, secret, ret_coro)

                patched_start._kira_bridge_intents = True
                patched_start._kira_bridge_orig = orig_start
                botpy.Client.start = patched_start

            flag["patched"] = True
        except Exception as exc:
            logger.debug("[QQBOT-BRIDGE] 安装 intent 补丁失败: %s", exc)

    # ------------------------------------------------------------------ #
    # 对"已经连上"的客户端：补状态 + 请一次重连
    # ------------------------------------------------------------------ #
    def _upgrade_live_client(self, adapter, name: str, client) -> None:
        """适配器先于插件连接时，让新订阅位**立刻**生效。

        为什么必须主动重连：`ws_identify` 只在**建连时**发一次 intent，
        当前这条连接早就鉴权过了 ⇒ 不重连就永远收不到成员事件。
        用户会以为"开了开关没用"（实测现象）。
        """
        if client is None or not self.extra_intents:
            return
        try:
            bits = self._EXTRA_INTENT_BITS
            before = int(getattr(client, "intents", 0) or 0)
            if before & bits == bits:
                return                      # 已经带上（说明我们的补丁在首连就生效了）
            client.intents = before | bits
            self._intents_patched_at = time.time()
            logger.info(
                "[QQBOT-BRIDGE] %s: 适配器先于插件连接，正在为其开通额外订阅"
                "（成员进出 / 加群申请 / 按钮回调）…", name,
            )
            self._request_reconnect(name, reason="初始订阅")
        except Exception as exc:
            logger.debug("[QQBOT-BRIDGE] 补写 intent 失败（忽略）: %s", exc)

    def _request_reconnect(self, name: str, reason: str = "") -> None:
        """请 botpy 断一次线，让它带着新订阅位重新鉴权。

        安全性：延迟数秒执行（避开启动高峰）；只做一次；
        失败只记日志。断线后 botpy 自己会重连 ——
        `ws_connect` 循环 break → `on_closed` 把 session 放回列表 →
        `_pool_init` 再跑一轮 `multi_run` → `ws_identify` 带上新位。
        """
        flag = self.profiles.setdefault("__intents__", {})
        if flag.get("reconnect_started"):
            return
        flag["reconnect_started"] = True
        plugin = self

        async def _worker():
            waited = 0.0
            try:
                await asyncio.sleep(plugin._RECONNECT_DELAY)
                while waited < plugin._RECONNECT_MAX_WAIT:
                    gateways = [r() for r in (flag.get("gateways") or [])]
                    gateways = [g for g in gateways if g is not None]
                    alive = [g for g in gateways
                             if not getattr(getattr(g, "_conn", None), "closed", True)]
                    if alive:
                        closed = 0
                        for gw in alive:
                            sess = getattr(gw, "_session", None)
                            if isinstance(sess, dict):
                                sess["intent"] = int(sess.get("intent") or 0) | \
                                    plugin._EXTRA_INTENT_BITS
                            try:
                                await gw._conn.close()
                                closed += 1
                            except Exception:
                                pass
                        if closed:
                            logger.info(
                                "[QQBOT-BRIDGE] %s: 已请 botpy 重连（%s，%d 条连接）—— "
                                "重连后成员进出 / 加群申请 / 按钮回调即可收到",
                                name, reason or "更新订阅", closed,
                            )
                            return
                    await asyncio.sleep(plugin._RECONNECT_RETRY)
                    waited += plugin._RECONNECT_RETRY
                logger.info(
                    "[QQBOT-BRIDGE] %s: 额外订阅位已就位 —— 将在下一次连接时自动生效"
                    "（若此刻收不到成员事件，重启 KiraAI 即可）", name,
                )
            except Exception as exc:
                logger.debug("[QQBOT-BRIDGE] 请求重连失败（忽略）: %s", exc)

        try:
            asyncio.get_running_loop().create_task(_worker())
        except RuntimeError:
            pass

    # ------------------------------------------------------------------ #
    # 自愈：若额外订阅位把连接搞挂，自动回退
    # ------------------------------------------------------------------ #
    #: 注入后多久才开始判定连接是否健康（给足建连/重连时间）
    _INTENT_PROBE_DELAY = 30.0

    def _health_check_intents(self) -> None:
        """若额外订阅位把连接搞挂了，自动回退（保住「能收消息」这个基本盘）。

        触发条件（同时满足才回退，宁可不回退也不误伤）：
          1. `extra_intents` 开着、补丁已装、还没回退过；
          2. 距上次注入已超过 `_INTENT_PROBE_DELAY`；
          3. 从探针抓到过网关，且它们进入"不可恢复"状态：
             * `_can_reconnect is False` —— botpy **只在** `WS_INVALID_SESSION`
               时这么设（`gateway.py:_is_system_event`），
               这正是"平台拒绝订阅/鉴权失败"的确切信号；或
             * 所有已知网关的 socket 都已关闭（连不上且无存活连接）。
        """
        if not self.extra_intents or self._intents_reverted:
            return
        flag = self.profiles.get("__intents__") or {}
        if not flag.get("patched") or not self._intents_patched_at:
            return
        if time.time() - self._intents_patched_at < self._INTENT_PROBE_DELAY:
            return

        gateways = [r() for r in (flag.get("gateways") or [])]
        gateways = [g for g in gateways if g is not None]
        if not gateways:
            return

        if any(getattr(g, "_can_reconnect", True) is False for g in gateways):
            self._intents_reverted = True
            logger.warning(
                "[QQBOT-BRIDGE] 检测到平台拒绝了额外订阅（botpy 标记 _can_reconnect=False，"
                "来自 WS_INVALID_SESSION）—— 已自动回退 intent 补丁，保证正常收发消息不受影响。"
                "请把配置项 extra_intents 关掉（重启后生效）"
            )
            self._revert_extra_intents()
            return

        if not any(not getattr(getattr(g, "_conn", None), "closed", True) for g in gateways):
            self._intents_reverted = True
            logger.warning(
                "[QQBOT-BRIDGE] 检测到网关 socket 全部关闭且未重连（疑与额外订阅位有关）—— "
                "已自动回退 intent 补丁以保证收发消息；若仍异常请把 extra_intents 关掉"
            )
            self._revert_extra_intents()

    def _revert_extra_intents(self) -> None:
        flag = self.profiles.get("__intents__")
        if not flag or not flag.get("done"):
            return
        try:
            import botpy

            current = getattr(botpy.Client, "start", None)
            if getattr(current, "_kira_bridge_intents", False):
                delattr(botpy.Client, "start")
            from botpy.gateway import BotWebSocket

            for attr, mark in (("ws_identify", "_kira_bridge_intent"),
                               ("send_msg", "_kira_bridge_probe"),
                               ("__init__", "_kira_bridge_probe")):
                cur = getattr(BotWebSocket, attr, None)
                if getattr(cur, mark, False):
                    original = getattr(cur, "_kira_bridge_orig", None)
                    if original is not None:
                        setattr(BotWebSocket, attr, original)
        except Exception:
            pass
        flag.clear()
        flag["gateways"] = []
        logger.warning("[QQBOT-BRIDGE] 已回退额外订阅位（成员事件 / 互动回调将不再收到）")

    # ------------------------------------------------------------------ #
    # 群名（2.x：构建事件时填；3.0：publish 包装，见 v3_support）
    # ------------------------------------------------------------------ #
    def _group_name_for(self, adapter_name: str, adapter, group_id: str, client) -> str:
        """取群名；没有就顺手丢一个后台任务去拉，并保持 openid（不阻塞）。"""
        if not self.group_name_enabled or not group_id:
            return str(group_id)
        cached = self.group_names.lookup(adapter_name, str(group_id))
        if cached:
            return cached
        try:
            self.group_names.schedule_fetch(adapter, adapter_name, str(group_id), client, logger)
        except Exception as exc:
            logger.debug("[QQBOT-BRIDGE] 排队群名拉取失败: %s", exc)
        return str(group_id)

    # ------------------------------------------------------------------ #
    # 成员事件（1<<24）：成员进出 / 加群申请
    # ------------------------------------------------------------------ #
    def _make_member_handler(self, adapter, name: str, event_name: str):
        async def _handler(payload):
            try:
                await self._on_member_event(adapter, name, payload, event_name)
            except Exception as exc:
                logger.debug("[QQBOT-BRIDGE] 处理成员事件失败（%s）: %s: %s",
                             event_name, type(exc).__name__, exc)

        return _handler

    async def _on_member_event(self, adapter, name: str, payload, event_name: str) -> None:
        """成员事件（1<<24）：成员进出 + 加群申请。

        ⚠ **两个开关是分开的**，因为官方门槛不同：

        | 事件 | 门槛 | 开关 |
        |---|---|---|
        | `GROUP_MEMBER_ADD` / `GROUP_MEMBER_REMOVE` | 无（文档未要求管理员） | `member_notice_enabled`（默认开） |
        | `GROUP_JOIN_REQUEST` | ★ **需要机器人是群管理员** | `admin_join_request_notice`（默认关） |

        官方「用户申请加群事件」文档原文：
        「**1.只有当机器人是群管理员时才可以收到此事件。**」
        所以机器人不是管理员时，平台**根本不会推**这个事件过来 ——
        这个开关打开也收不到，但不该因此把它算作"无需权限"。
        """
        if not self.enabled:
            return
        is_join_request = "join_request" in str(event_name or "")
        if is_join_request:
            if not self.join_request_notice_enabled:
                return
        elif not self.member_notice_enabled:
            return
        body = normalize_body(payload) or {}
        text = describe_member_event(event_name, body, self.group_names, name,
                                     identities=self.identities)
        if not text:
            return
        group_id = str(body.get("group_openid") or "")
        member_id = str(body.get("member_openid") or body.get("op_member_openid") or "")
        self._member_events += 1
        if self._member_events <= 3:
            logger.info("[QQBOT-BRIDGE] 成员事件 #%d：%s", self._member_events, text)
        self.publish_synthetic_event(
            target_id=group_id or name,
            sender_id=member_id or "system",
            is_group=bool(group_id),
            text=text,
        )

    # ------------------------------------------------------------------ #
    # 2.x：构建事件时把群名填进去
    # ------------------------------------------------------------------ #
    def _fill_group_name_v2(self, adapter, name: str, body: dict) -> str:
        group_id = str(body.get("group_openid") or "")
        try:
            client = adapter.get_client()
        except Exception:
            client = None
        return self._group_name_for(name, adapter, group_id, client)

    def _patch_send_path(self, adapter, name: str, client) -> None:
        if name in self._patched_sends:
            return
        original = getattr(adapter, "_send_message", None)
        if not callable(original):
            return

        async def _send_message(target_id, send_message_obj, is_group):
            ref = self._quote_ref_for(adapter, target_id, send_message_obj, is_group) \
                if self.quote_reply else None
            token = QUOTE_REF.set(ref)
            try:
                result = await original(target_id, send_message_obj, is_group)
            finally:
                QUOTE_REF.reset(token)
            if result is not None and bool(getattr(result, "ok", True)):
                return result
            err = str(getattr(result, "err", "") or "")
            # 被动 msg_id 已过期（官方 40034005「回复消息msg_id已过期」）：
            # 缓存的回复 id 已死，先清掉——否则之后每条消息都会先白失败一次。
            # 典型触发：跨会话合并路由来的轮（触发消息是合成控制消息，没有新鲜
            # msg_id，只能用到 5 分钟前的旧 id）。
            expired = "msg_id已过期" in err or "40034005" in err
            if expired:
                self._purge_dead_reply_id(adapter, str(target_id), is_group)
            if self.proactive_enabled and (
                "needs a received message" in err or expired
            ):
                logger.warning(
                    "[QQBOT-BRIDGE] 被动回复不可用（%s），改走主动消息兜底",
                    err[:80],
                )
                return await self._proactive_send(adapter, str(target_id), send_message_obj, is_group)
            return result

        adapter._send_message = _send_message
        self._patched_sends[name] = original
        if self.send_at_mention:
            self._patch_text_content(adapter)

    @staticmethod
    def _purge_dead_reply_id(adapter, target_id: str, is_group: bool) -> None:
        """清掉已过期的被动回复 id（40034005 之后它就是死的，留着只会让后续
        每条消息都先白失败一次）。best-effort，结构对不上就跳过。"""
        try:
            reply_ids = getattr(
                adapter, "_group_reply_ids" if is_group else "_direct_reply_ids", None
            )
            if isinstance(reply_ids, dict) and reply_ids.pop(target_id, None):
                logger.info(
                    "[QQBOT-BRIDGE] 已清除过期的被动回复 id（%s %s）",
                    "群" if is_group else "私聊", target_id,
                )
        except Exception:
            pass

    def _patch_text_content(self, adapter) -> None:
        """把发出的 @ 渲染成平台认的标记（`<qqbot-at-user id="..." />`）。

        KiraAI 原实现是 ``f"@{element.nickname or element.pid}"`` —— 那只是**纯文本**，
        QQ 不会渲染成真正的提及（用户实测：机器人 @ 人，群里显示的是一串 openid 文本）。
        平台发送侧要的是富文本标记，参考 AstrBot 的同一段实现。

        做法上只接管 At 元素：把它换成等价的 Text，其余元素原样交给原实现，
        这样框架以后新增元素类型也不会漏处理。
        """
        current = getattr(adapter, "_text_content", None)
        # 关键：如果已经有一层**别的实例**留下的补丁（热重载残留），要能顶掉它 ——
        # 否则我们会一直沿用旧实例的包装（它的配置/状态可能都是旧的）。
        original = getattr(adapter, "_qqbot_bridge_text_orig", None)
        if original is None:
            if getattr(current, "_kira_bridge_at", False):
                original = getattr(current, "_kira_bridge_orig", None) or current
                logger.warning(
                    "[QQBOT-BRIDGE] 检测到 adapter 上已存在 @ 补丁层（多为热重载残留），已接管"
                )
            else:
                original = current
        if not callable(original):
            return
        adapter._qqbot_bridge_text_orig = original

        style = self.at_markup_style

        def _text_content(send_message_obj):
            rewritten = []
            for element in send_message_obj:
                if isinstance(element, At):
                    pid = str(getattr(element, "pid", "") or "")
                    if pid and pid != "all" and pid.isalnum():
                        rewritten.append(Text(at_user_markup(pid, style)))
                    else:
                        name = getattr(element, "nickname", None) or pid or "全体成员"
                        rewritten.append(Text("@" + name))
                elif isinstance(element, Text) and getattr(element, "text", ""):
                    # 模型自己写进正文的 @ 标记 —— 它是普通字符串，会被原样发成文本
                    fixed, did = normalize_outgoing_markup(element.text, style)
                    if did:
                        if not self._llm_markup_logged:
                            self._llm_markup_logged = True
                            logger.info(
                                "[QQBOT-BRIDGE] 正文里出现了模型自己写的 @ 标记 —— 已按 "
                                "at_markup_style=%s 归一化（这就是「@ 显示成一串文本」的常见来源）",
                                style,
                            )
                        rewritten.append(Text(fixed))
                    else:
                        rewritten.append(element)
                else:
                    rewritten.append(element)
            return original(rewritten)

        _text_content._kira_bridge_at = True
        _text_content._kira_bridge_orig = original
        adapter._text_content = _text_content
        self._text_originals[getattr(adapter.info, "name", "?")] = original

    def _patch_api_quote(self, adapter, name: str, client) -> None:
        """在 botpy 的发送接口上注入 message_reference。

        botpy 的 ``post_group_message``/``post_c2c_message`` 用 ``payload = locals()``
        组装请求体，所以只要多传一个 ``message_reference`` 关键字参数，它就会进 JSON —— 
        不用去复制一遍适配器的发送逻辑。
        """
        api = getattr(client, "api", None)
        if api is None:
            return
        # 原始方法表挂在 api 上：新实例可以**顶掉**旧实例留下的补丁层（热重载残留），
        # 否则一直是旧实例的包装在跑（它的配置/状态可能已经过时）。
        patched = self._api_patched.setdefault(name, [])
        originals = getattr(api, "_qqbot_bridge_api_orig", None)
        if originals is None:
            originals = {}
            try:
                api._qqbot_bridge_api_orig = originals
            except Exception:
                return
        for method_name, is_group in (("post_group_message", True), ("post_c2c_message", False)):
            current = getattr(api, method_name, None)
            if not callable(current):
                continue
            if getattr(current, "_kira_bridge_quote", False):
                orig = originals.get(method_name) or getattr(current, "_kira_bridge_orig", None) or current
            else:
                orig = current
            originals[method_name] = orig
            store = {}

            async def _patched(*args, _orig=orig, _is_group=is_group, _store=store, **kwargs):
                ref = QUOTE_REF.get()
                if ref and not kwargs.get("message_reference"):
                    kwargs["message_reference"] = {"message_id": ref}

                # ★ 纯文本（msg_type=0）**没有 @ 能力** —— 正文里带提到标签时必须走
                #   markdown（msg_type=2），否则客户端只会把标签原样显示成一串文本。
                #   依据：官方《文本交互》页 + bunqq-core 开发文档
                #   （"含 <qqbot-at-user id> 提及标签 → 强制 md（纯文本无 @ 能力）"）。
                md_text = None
                text = kwargs.get("content")
                if self.at_markdown and isinstance(text, str) and (
                    "<@" in text or "qqbot-at-user" in text
                ):
                    md_text = text
                    kwargs = dict(kwargs)
                    kwargs["msg_type"] = 2
                    kwargs["markdown"] = {"content": text}
                    kwargs["content"] = None
                    if not self._at_md_logged:
                        self._at_md_logged = True
                        logger.info(
                            "[QQBOT-BRIDGE] 正文含 @ 标记 → 本条改按 markdown 发送"
                            "（纯文本消息没有 @ 能力，会被显示成文本）"
                        )

                if md_text is None:
                    result = await _orig(*args, **kwargs)
                else:
                    try:
                        result = await _orig(*args, **kwargs)
                    except Exception as md_exc:
                        # markdown 发不出去（例如未获原生 MD 权限）⇒ 退回纯文本，
                        # 但必须剥掉标记，否则又会把标签原样发出去
                        fallback = dict(kwargs)
                        fallback["msg_type"] = 0
                        fallback["markdown"] = None
                        fallback["content"] = strip_at_markup(md_text)
                        if not self._at_md_fallback_logged:
                            self._at_md_fallback_logged = True
                            logger.warning(
                                "[QQBOT-BRIDGE] markdown 发送失败（%s: %s），已退回纯文本并剥掉 @ 标记 —— "
                                "若群里看到的就是这种情况，说明该机器人没有 markdown 消息权限",
                                type(md_exc).__name__, md_exc,
                            )
                        result = await _orig(*args, **fallback)
                try:
                    sent_ref = extract_sent_ref_idx(result)
                    if sent_ref:
                        # 记住"机器人自己发的这条"的 ref_idx，以后才能引用它
                        target = kwargs.get("group_openid") if _is_group else kwargs.get("openid")
                        if target:
                            sent_id = result.get("id") if isinstance(result, dict) else None
                            if sent_id:
                                display = adapter._display_message_id(str(sent_id))
                                sid = f"{adapter.info.name}:{'gm' if _is_group else 'dm'}:{target}"
                                _store.setdefault("adapter", adapter)
                                self._remember_ref(adapter, sid, display, sent_ref)
                except Exception as exc:
                    logger.debug("[QQBOT-BRIDGE] 记录已发送消息的 ref_idx 失败: %s", exc)
                return result

            _patched._kira_bridge_quote = True
            _patched._kira_bridge_orig = orig
            setattr(api, method_name, _patched)
            patched.append((api, method_name, orig))
    async def _proactive_send(self, adapter, target_id, send_message_obj, is_group):
        client = adapter.get_client()
        if client is None:
            return KiraIMSentResult(ok=False, err="QQ official bot is not connected")

        today = time.strftime("%Y-%m-%d")
        if today != self._proactive_day:
            self._proactive_day = today
            self._proactive_count = 0
        now = time.time()
        if self.proactive_min_interval > 0 and \
                now - self._last_proactive.get(target_id, 0.0) < self.proactive_min_interval:
            return KiraIMSentResult(ok=False, err="proactive throttled by qqbot bridge")

        text_content = getattr(adapter, "_text_content", None)
        content = text_content(send_message_obj) if callable(text_content) else ""
        media_elements = [e for e in send_message_obj if isinstance(e, (File, Image))]
        if len(media_elements) > 1 or (not content and not media_elements):
            return KiraIMSentResult(ok=False, err="qqbot bridge cannot send this message shape")

        media = None
        if media_elements:
            upload_file = getattr(adapter, "_upload_file", None)
            media_payload = getattr(adapter, "_media_payload", None)
            if not callable(upload_file) or not callable(media_payload):
                return KiraIMSentResult(ok=False, err="qqbot bridge cannot upload media on this core version")
            try:
                upload = await upload_file(target_id, media_elements[0], is_group)
                media = media_payload(upload)
            except Exception as exc:
                return KiraIMSentResult(ok=False, err="proactive media upload failed: %s" % exc)
            if not media:
                return KiraIMSentResult(ok=False, err="proactive media upload returned no file_info")

        payload = {"msg_type": 7 if media else 0, "content": content or None, "msg_seq": 1}
        if media:
            payload["media"] = media
        # 主动通道同样遵守「纯文本没有 @ 能力」：正文带 @ 标记时改按 markdown 发，
        # 失败（无原生 MD 权限）退回剥掉标记的纯文本 —— 与被动路径（v1.1.9）同一语义
        md_text = None
        if not media and self.at_markdown and isinstance(content, str) and (
            "<@" in content or "qqbot-at-user" in content
        ):
            md_text = content
            payload["msg_type"] = 2
            payload["markdown"] = {"content": content}
            payload["content"] = None
        try:
            if is_group:
                result = await client.api.post_group_message(group_openid=target_id, **payload)
            else:
                result = await client.api.post_c2c_message(openid=target_id, **payload)
        except Exception as exc:
            if md_text is not None:
                try:
                    payload = {"msg_type": 0, "content": strip_at_markup(md_text), "msg_seq": 1}
                    if is_group:
                        result = await client.api.post_group_message(group_openid=target_id, **payload)
                    else:
                        result = await client.api.post_c2c_message(openid=target_id, **payload)
                    logger.warning(
                        "[QQBOT-BRIDGE] 主动 markdown 发送失败（%s），已退回纯文本并剥掉 @ 标记", exc
                    )
                except Exception as exc2:
                    logger.warning("[QQBOT-BRIDGE] 主动消息发送失败: %s", exc2)
                    return KiraIMSentResult(ok=False, err="proactive send failed: %s" % exc2)
            else:
                logger.warning("[QQBOT-BRIDGE] 主动消息发送失败: %s", exc)
                return KiraIMSentResult(ok=False, err="proactive send failed: %s" % exc)

        self._last_proactive[target_id] = now
        self._proactive_count += 1
        result_id = getattr(adapter, "_result_message_id", None)
        message_id = result_id(result) if callable(result_id) else None
        logger.info("[QQBOT-BRIDGE] 主动消息已发送（今日第 %d 条）", self._proactive_count)
        return KiraIMSentResult(message_id=message_id)

# --------------------------------------------------------------------------- #
# 标签：<markdown> / <keyboard>
#
# 靠核心的 TagSet 机制进提示词（两版一致）：
#   message_manager 在 ON_LLM_REQUEST 阶段 new TagSet() → 逐个 handler 传下去 →
#   tag_set.to_prompt() 拼进 format 提示词的 message_types 段。
# 标签的父级都是 "msg"（与 <text> 同级），所以模型写
#   <msg><markdown>## 标题</markdown><keyboard>{...}</keyboard></msg>
# 即可。真正的渲染由 api 层补丁（api_send）按 msg_type=2 + keyboard 发送。
# --------------------------------------------------------------------------- #
class _BridgeTag(BaseTag):
    """描述文案由插件按配置动态给的标签基类。"""

    def __init__(self, ctx=None, description: str = ""):
        super().__init__(ctx=ctx)
        if description:
            self.description = description

    def _make(self, element):
        return element


class MarkdownTag(_BridgeTag):
    name = "markdown"
    description = MARKDOWN_TAG_DESCRIPTION

    async def handle(self, value: str, **kwargs):
        text = (value or "").strip()
        if not text:
            return []
        return [MarkdownText(text)]


class KeyboardTag(_BridgeTag):
    name = "keyboard"
    description = KEYBOARD_TAG_DESCRIPTION

    async def handle(self, value: str, **kwargs):
        try:
            payload = validate_keyboard(value or "")
        except Exception as exc:
            logger.warning("[QQBOT-BRIDGE] <keyboard> 内容不合法，已丢弃：%s", exc)
            return []
        return [KeyboardMarker(payload)]


# --------------------------------------------------------------------------- #
# 插件钩子注册
#
# ⚠⚠ 这里有一个**必须遵守的硬约束**（核心 `plugin_registry.py` 的
#    `_register_plugin_hooks_for` 决定的）：
#
#        if plugin_instance is not None and hasattr(plugin_instance, bound_handler.__name__):
#            bound_handler = getattr(plugin_instance, bound_handler.__name__)
#
#    框架**只按函数 `__name__` 去插件实例上找同名属性**来绑定 self。
#    因此「注册用的函数名」必须与「绑到类上的属性名」**完全一致** ——
#    否则框架会注册一个**未绑定的裸函数**，调用时 self 错位（self 变成 event、
#    event 变成 request…），每次都抛异常并被 `exec_handler` 吞掉 ——
#    表现就是**工具与标签永远注入不进去**，而日志里只有一行 traceback。
#    （这个坑本次复审实测踩到：函数名 `_hook_llm_request` vs 属性名 `on_llm_request`。）
#
#    另外：装饰器 `on.llm_request` 靠 `inspect.getmodule(func)` 的模块名查
#    `_module_to_plugin` 映射来判定插件归属，模块级函数能正确定位到本插件。
# --------------------------------------------------------------------------- #
async def on_llm_request(self, event, request, tag_set, *_, **__):
    """ON_LLM_REQUEST：注入 L1 工具 + markdown/keyboard 标签 + 3.0 增量。

    注意：函数名 `on_llm_request` **必须**与下面绑到类上的属性名一致（见上方说明）。
    """
    try:
        self.inject_tools_and_tags(event, request, tag_set)
    except Exception as exc:
        logger.debug("[QQBOT-BRIDGE] 注入失败（忽略）: %s: %s", type(exc).__name__, exc)


on.llm_request(priority=Priority.MEDIUM)(on_llm_request)
QQOfficialGroupBridge.on_llm_request = on_llm_request
