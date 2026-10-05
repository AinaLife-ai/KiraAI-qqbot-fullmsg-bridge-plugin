"""QQ官方bot兼容与增强补丁 — KiraAI plugin.

把 QQ 官方机器人适配器对齐到 NapCat 语义：

  ① **修掉 `_parser unknown event group_message_create`** —— 为 qq-botpy 补上
     群全量消息解析器，让不 @ 机器人的消息也能进 KiraAI（围观 / 关键词唤醒）；
  ② **@ 消息与单聊也统一接管** —— 原生实现把 `nickname` 写成 32 位 OpenID，
     这里改成事件自带的 `author.username`（真实 QQ 昵称）；
  ③ `is_mentioned` 按 NapCat 语义判定（只有真 @ 才算唤醒）；
  ④ 同一 `msg_id` 去重（官方明说会重推）；跨事件重复按「绝不丢唤醒」处理，并留了
     一个可选的等待窗口（`at_grace_seconds`，默认 0）；
  ⑤ 自动记住昵称通讯录（零维护，改名自动跟随）；
  ⑥ 可选：官方「主动消息」通道，兜底超出 5 分钟被动窗口的持续/主动回复。

设计要点见 `qqbot_bridge.py` 的模块说明。本文件只负责 KiraAI 侧的生命周期、
配置、适配器定位与补丁安装。

性能、阻塞与可逆性约定
----------------------
* 消息路径上**没有同步 I/O、没有锁、没有无界循环**：每条消息只有几次 dict
  查找 + 一次有界 LRU 更新（实测约 6.5 µs/条）；
* 昵称通讯录落盘走 `asyncio.to_thread`，且只在「脏了」的时候写；
* 每个异常都被兜住并降级成一条日志，绝不把异常抛回 botpy 的事件循环；
* 所有 KiraAI 私有属性都用 `getattr` 防御式读取，框架版本变动只会打一条清晰的
  错误然后停用桥接，不会每条消息崩一次；
* **补丁可逆**：把 `enabled` 关掉（或关掉对应 unify 开关）后，下一次巡检会把
  botpy 解析器与客户端处理器**还原成框架原生实现**，不需要重启进程。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import contextvars
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
from core.chat import Group, User
from core.chat.message_elements import At, File, Image, Reply, Text
from core.chat.message_utils import KiraIMMessage, KiraMessageEvent, KiraIMSentResult

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
    extract_msg_idx,
    extract_sent_ref_idx,
    normalize_outgoing_markup,
    inject_live_parser,
    install_class_parser,
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


#: 这一次发送要引用哪条消息（REFIDX）。用 contextvar 传给 api 层的包装函数，
#: 避免为了注入 message_reference 去复制一遍适配器的发送逻辑。
_QUOTE_REF: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "qqbot_bridge_quote_ref", default=None)


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
        self.pinned_self_openid = str(basic.get("self_openid", "") or "").strip()

        proactive = cfg.get("section_proactive", {}) or {}
        self.proactive_enabled = bool(proactive.get("proactive_enabled", False))
        try:
            self.proactive_min_interval = max(0.0, float(proactive.get("proactive_min_interval", 0)))
        except (TypeError, ValueError):
            self.proactive_min_interval = 0.0

        self.dedup = MessageDedup(ttl=self.dedup_ttl)
        self.identities = IdentityStore(path=_identity_path() if self.remember_nicknames else None)

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
        self._ref_logged = 0
        self._last_proactive = {}
        #: 仅用于日志观测（今日主动消息条数），不做任何限制——配额由官方判
        self._proactive_day = ""
        self._proactive_count = 0
        self._last_report = ""

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
            await self._flush_identities()

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
        patches = self._ensure_class_patch()
        adapters = self._find_adapters()
        if not adapters:
            if report:
                logger.info("[QQBOT-BRIDGE] 暂未发现 QQ Official 适配器实例（插件可能先于适配器加载）")
            return
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
            for api, method_name, orig in self._api_patched.pop(name, []):
                try:
                    setattr(api, method_name, orig)
                    changed.append(f"{name}.api.{method_name}")
                except Exception:
                    pass
        if changed and not self._restore_reported:
            self._restore_reported = True
            logger.info("[QQBOT-BRIDGE] 已还原 %d 处补丁：%s", len(changed), ", ".join(changed[:6]))

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
        missing = check_adapter_capabilities(adapter)
        if missing:
            if name not in self._broken_adapters:
                self._broken_adapters.add(name)
                logger.error(
                    "[QQBOT-BRIDGE] %s: 适配器缺少必要接口 %s —— 桥接对该适配器停用。"
                    "通常是 KiraAI core 版本变动导致，请到插件仓库反馈",
                    name, missing,
                )
            return

        try:
            client = adapter.get_client()
        except Exception as exc:
            logger.debug("[QQBOT-BRIDGE] %s: get_client() 失败: %s", name, exc)
            return
        if client is None:
            return

        conn = getattr(client, "_connection", None)
        state = getattr(conn, "state", None)

        # (event, handler attr, kind, is_group, wanted, allow_shadow)
        plan = [
            (EVENT_GROUP_MESSAGE, "on_group_message_create", KIND_FULL, True, True, False),
            (EVENT_GROUP_AT_MESSAGE, "on_group_at_message_create", KIND_AT, True,
             bool(self.unify_at), True),
            (EVENT_C2C_MESSAGE, "on_c2c_message_create", KIND_DM, False,
             bool(self.unify_dm), True),
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
            live_plan = [
                (EVENT_GROUP_MESSAGE, False, True),
                (EVENT_GROUP_AT_MESSAGE, True, bool(self.unify_at)),
                (EVENT_C2C_MESSAGE, True, bool(self.unify_dm)),
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
            )
        except Exception as exc:
            logger.warning("[QQBOT-BRIDGE] 构造事件失败（%s）: %s: %s", kind, type(exc).__name__, exc)
            return

        if event is None:
            if reason != "duplicate":
                logger.debug("[QQBOT-BRIDGE] 事件被丢弃（%s）: %s", kind, reason)
            return

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

    def _patch_send_path(self, adapter, name: str, client) -> None:
        if name in self._patched_sends:
            return
        original = getattr(adapter, "_send_message", None)
        if not callable(original):
            return

        async def _send_message(target_id, send_message_obj, is_group):
            ref = self._quote_ref_for(adapter, target_id, send_message_obj, is_group) \
                if self.quote_reply else None
            token = _QUOTE_REF.set(ref)
            try:
                result = await original(target_id, send_message_obj, is_group)
            finally:
                _QUOTE_REF.reset(token)
            if result is not None and bool(getattr(result, "ok", True)):
                return result
            err = str(getattr(result, "err", "") or "")
            if self.proactive_enabled and "needs a received message" in err:
                return await self._proactive_send(adapter, str(target_id), send_message_obj, is_group)
            return result

        adapter._send_message = _send_message
        self._patched_sends[name] = original
        if self.send_at_mention:
            self._patch_text_content(adapter)
        if self.quote_reply:
            self._patch_api_quote(adapter, name, client)

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
                ref = _QUOTE_REF.get()
                if ref and not kwargs.get("message_reference"):
                    kwargs["message_reference"] = {"message_id": ref}
                result = await _orig(*args, **kwargs)
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
        try:
            if is_group:
                result = await client.api.post_group_message(group_openid=target_id, **payload)
            else:
                result = await client.api.post_c2c_message(openid=target_id, **payload)
        except Exception as exc:
            logger.warning("[QQBOT-BRIDGE] 主动消息发送失败: %s", exc)
            return KiraIMSentResult(ok=False, err="proactive send failed: %s" % exc)

        self._last_proactive[target_id] = now
        self._proactive_count += 1
        result_id = getattr(adapter, "_result_message_id", None)
        message_id = result_id(result) if callable(result_id) else None
        logger.info("[QQBOT-BRIDGE] 主动消息已发送（今日第 %d 条）", self._proactive_count)
        return KiraIMSentResult(message_id=message_id)
