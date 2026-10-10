"""INTERACTION_CREATE（键盘按钮点击）→ KiraAI 事件。

官方流程（《消息交互概述》原文）
------------------------------
1. 机器人下发带 `keyboard` 的消息 → 用户看到按钮；
2. 用户点击 → 平台推 `INTERACTION_CREATE`；
3. 机器人 **必须** 调 `PUT /interactions/{id}` 回应，**超时 3 秒**，否则客户端一直转圈。

现状（两版核心都一样）
-------------------
* `botpy.connection.ConnectionState.parse_interaction_create` **存在**（会 `_dispatch`）；
* 但 `_QQOfficialClient` 没有 `on_interaction_create` ⇒ `ws_dispatch` 找不到就**静默丢弃**。

所以"发键盘"必须配"接回调"，否则按钮点了只会转圈 —— 比不放按钮更糟。

设计要点
-------
* **先回执，再干活**：回执只有一次 HTTP，且带 2.5s 超时；任何后续失败都不会让用户看到转圈。
* **转成标准 Kira 事件**：`is_notice=True`、`is_mentioned=True`，正文是被点按钮的 `data`，
  这样模型看到的是"用户点了『签到』"，可以自然接话。
  核心有 `PluginContext.publish_notice`，但那是给插件用的；这里我们直接构造事件再
  `adapter.publish()`，两版都能走通。
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Optional

from qqbot_bridge import _MARK as _BRIDGE_MARK

#: 按钮互动类型
_TYPE_BUTTON = 11        # 消息按钮回调（INLINE_KEYBOARD）
_TYPE_MENU = 12          # 单聊快捷菜单回调
#: 只需要回执的类型（其余如消息反馈/清空会话无需回执）
_TYPES_NEEDING_ACK = {_TYPE_BUTTON, _TYPE_MENU}

#: 回执超时（官方要求 3 秒内）
_ACK_TIMEOUT = 2.5

_HANDLER_ATTR = "on_interaction_create"


def client_has_native_handler(client: Any, attr_name: str) -> bool:
    """核心自己实现了同名处理器时，桥接**让位**（与 qqbot_bridge 同一原则）。"""
    return attr_name in type(client).__dict__


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _body(payload: Any) -> Optional[dict]:
    """把 botpy 的 `Interaction` 对象 / 原始 payload 统一成**扁平 dict**。

    ★★★ 2026-10-10 修的真断点（用户实测："回调按钮点了没反应 / 客户端提示
      请求第三方失败"，香里查到"适配层没把 INTERACTION 事件接进来"）：

    botpy 的派发形状是 `Interaction` **对象**（见 `botpy/interaction.py` 与
    `botpy/connection.py:165`）：

        parse_interaction_create(payload)
            → Interaction(api, payload["id"], payload["d"])
            → _dispatch("interaction_create", 对象)
            → client.on_interaction_create(对象)      # ← 我们收到的就是这个对象

    而原来的实现只认 dict（`payload.get("d")`）⇒ **对象形态被直接丢弃**：
    不回执（客户端一直 loading / 报错）、也不转成消息 ⇒ 看起来就像"没接进来"。

    现在两种形状都认：
      * `Interaction` 对象 ⇒ 取 `id/type/scene/chat_type/user_openid/group_openid/
        group_member_openid/data.resolved.{button_id,button_data,message_id,user_id}`；
      * 原始 dict ⇒ `{"d": {...}}`（网关包一层）或内层 dict 本身（webhook/测试用）。
    """
    if isinstance(payload, dict):
        inner = payload.get("d")
        return inner if isinstance(inner, dict) else payload

    # ---- botpy 的 Interaction 对象（真派发形状）----
    if not hasattr(payload, "type") and not hasattr(payload, "data"):
        return None
    data = getattr(payload, "data", None)
    if isinstance(data, dict):
        resolved = data.get("resolved") or {}
    else:
        resolved = getattr(data, "resolved", None) or {}

    def _r(name: str) -> Any:
        if isinstance(resolved, dict):
            return resolved.get(name)
        return getattr(resolved, name, None)

    return {
        # ⚠ 回执要用**内层 d.id**（官方：interaction_id 取自事件的 d.id，不带前缀）；
        #  botpy 的 Interaction.id 正是 d.id（外层信封 id 存在 event_id 里）。
        "id": getattr(payload, "id", None) or getattr(payload, "event_id", None),
        "type": getattr(payload, "type", None),
        "scene": getattr(payload, "scene", None),
        "chat_type": getattr(payload, "chat_type", None),
        "user_openid": getattr(payload, "user_openid", None),
        "group_openid": getattr(payload, "group_openid", None),
        "group_member_openid": getattr(payload, "group_member_openid", None),
        "data": {"resolved": {
            "button_id": _r("button_id"),
            "button_data": _r("button_data"),
            "message_id": _r("message_id"),
            "user_id": _r("user_id"),
        }},
    }


class InteractionBridge:
    """把 INTERACTION_CREATE 接上：先回执，再转事件。"""

    def __init__(self, plugin: Any, logger: Any):
        self.plugin = plugin
        self.logger = logger
        self._acked: set = set()          # 同一 interaction_id 只回执一次
        self._logged = False
        self._miss_logged = False
        #: 载荷形状不认识时只喊一次（真出过"事件到了却被丢弃"的事故）
        self._shape_logged = False
        #: 回执失败/不可用时只喊一次（回执是官方硬要求，静默失败=用户看到一直转圈）
        self._ack_fail_logged = False

    # ------------------------------------------------------------------ #
    def install(self, client: Any) -> str:
        """把 handler 挂到 botpy 客户端**实例**上（不污染类）。

        返回 ``attached`` / ``already`` / ``native``。
        """
        if client is None:
            return "no-client"
        current = getattr(client, _HANDLER_ATTR, None)
        if getattr(current, "_kira_bridge_owner", None) is not None:
            # ★ 2026-10-10：能走到这里说明**已经有人挂了** handler。
            #   若那是"另一份还在跑的旧副本"，它的行为会盖住我们（版本号很新、
            #   行为却是旧的）—— 这种"说不清"的现场必须留下证据，所以大声喊。
            try:
                _owner = getattr(current, "_kira_bridge_owner", None)
                _omod = getattr(sys.modules.get(getattr(current, "__module__", ""), None),
                                "__file__", "?")
                if getattr(self, "_conflict_logged", False) is not True:
                    self._conflict_logged = True
                    self.logger.warning(
                        "[QQBOT-BRIDGE] 互动回调已被**别的实例**接管（owner=%s，定义于 %s）"
                        " ⇒ 本次不抢；若你刚更新过代码却仍是旧行为，"
                        "多半就是那份**旧副本**在跑（找找 KiraAI 插件目录下有没有两份 "
                        "qqbot-fullmsg-bridge / 或没完全重启核心）",
                        _owner, _omod)
            except Exception:
                pass
            return "already"
        if client_has_native_handler(client, _HANDLER_ATTR):
            return "native"

        bridge = self

        async def on_interaction_create(payload):
            try:
                await bridge._on_interaction(client, payload)
            except Exception as exc:   # 绝不抛回 botpy 事件循环
                bridge.logger.debug("[QQBOT-BRIDGE] 处理互动事件失败: %s: %s",
                                    type(exc).__name__, exc)

        # 复用 qqbot_bridge 的标记体系 ⇒ _restore_all 能认出来并干净摘除
        setattr(on_interaction_create, _BRIDGE_MARK, True)
        setattr(on_interaction_create, "_kira_bridge_owner", id(self))
        setattr(client, _HANDLER_ATTR, on_interaction_create)
        return "attached"

    @staticmethod
    def uninstall(client: Any) -> bool:
        from qqbot_bridge import detach_client_handler

        return detach_client_handler(client, _HANDLER_ATTR)

    # ------------------------------------------------------------------ #
    async def _on_interaction(self, client: Any, payload: Any) -> None:
        body = _body(payload)
        if not isinstance(body, dict):
            if not self._shape_logged:
                self._shape_logged = True
                self.logger.warning(
                    "[QQBOT-BRIDGE] 互动事件载荷形状不认识（%s）—— 已忽略；"
                    "请把这条连同 botpy 版本一起反馈（事件可能到了但没被接住）",
                    type(payload).__name__)
            return
        interaction_id = str(body.get("id") or "")
        itype = body.get("type")
        try:
            itype = int(itype) if itype is not None else -1
        except (TypeError, ValueError):
            itype = -1

        if not self._logged:
            self._logged = True
            self.logger.info(
                "[QQBOT-BRIDGE] 已接上互动回调（INTERACTION_CREATE）：按钮点击会先回执、"
                "再作为一条消息转给模型，用户不会看到转圈"
            )

        # ---- ① 先回执（3 秒硬要求）----
        acked = False  # 已回执标记（仅用于日志观测）
        if interaction_id and itype in _TYPES_NEEDING_ACK:
            acked = await self._ack(client, interaction_id)

        # ---- ② 再转成 Kira 事件 ----
        if itype not in _TYPES_NEEDING_ACK:
            # 消息反馈 / 清空会话 / 切换模型 / 授权…… 只记录，不打扰模型
            self.logger.debug("[QQBOT-BRIDGE] 互动事件 type=%s（无需回执，已忽略）", itype)
            return

        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        resolved = data.get("resolved") if isinstance(data.get("resolved"), dict) else {}
        button_data = str(resolved.get("button_data") or "").strip()
        button_id = str(resolved.get("button_id") or "").strip()

        desc = button_data or button_id or "（未命名按钮）"
        text = f"[按钮] 用户点击了：{desc}"

        group_openid = str(body.get("group_openid") or "")
        member_openid = str(body.get("group_member_openid") or "")
        user_openid = str(body.get("user_openid") or "")
        is_group = bool(group_openid)

        target_id = group_openid if is_group else user_openid
        sender_id = member_openid if is_group else user_openid
        if not target_id or not sender_id:
            if not self._miss_logged:
                self._miss_logged = True
                self.logger.debug(
                    "[QQBOT-BRIDGE] 互动事件缺少会话标识（group_openid/user_openid 都没有），"
                    "无法转成消息：keys=%s", list(body)[:12],
                )
            return

        self.plugin.publish_synthetic_event(
            target_id=target_id,
            sender_id=sender_id,
            is_group=is_group,
            text=text,
        )

    async def _ack(self, client: Any, interaction_id: str) -> bool:
        """`PUT /interactions/{id}` —— 带超时，失败也不影响后续。"""
        if interaction_id in self._acked:
            return False
        self._acked.add(interaction_id)
        if len(self._acked) > 2048:
            self._acked.clear()
            self._acked.add(interaction_id)
        api = getattr(client, "api", None)
        fn = getattr(api, "on_interaction_result", None)
        if not callable(fn):
            if not self._ack_fail_logged:
                self._ack_fail_logged = True
                self.logger.warning(
                    "[QQBOT-BRIDGE] 接口层没有 on_interaction_result ⇒ **无法回执**"
                    "（官方要求 3 秒内回执，否则客户端一直 loading）——"
                    "请把这条连同 botpy 版本一起反馈")
            return False
        try:
            await asyncio.wait_for(fn(interaction_id, 0), timeout=_ACK_TIMEOUT)
            return True
        except Exception as exc:
            if not self._ack_fail_logged:
                self._ack_fail_logged = True
                self.logger.warning(
                    "[QQBOT-BRIDGE] 互动回执失败（%s: %s）—— 客户端可能一直转圈；"
                    "把这条连同 botpy 版本反馈即可（不影响其它功能）",
                    type(exc).__name__, str(exc)[:160])
            else:
                self.logger.debug("[QQBOT-BRIDGE] 互动回执失败（忽略）: %s", exc)
            return False
