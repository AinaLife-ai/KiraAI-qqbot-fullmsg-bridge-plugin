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
    if not isinstance(payload, dict):
        return None
    inner = payload.get("d")
    return inner if isinstance(inner, dict) else payload


class InteractionBridge:
    """把 INTERACTION_CREATE 接上：先回执，再转事件。"""

    def __init__(self, plugin: Any, logger: Any):
        self.plugin = plugin
        self.logger = logger
        self._acked: set = set()          # 同一 interaction_id 只回执一次
        self._logged = False
        self._miss_logged = False

    # ------------------------------------------------------------------ #
    def install(self, client: Any) -> str:
        """把 handler 挂到 botpy 客户端**实例**上（不污染类）。

        返回 ``attached`` / ``already`` / ``native``。
        """
        if client is None:
            return "no-client"
        current = getattr(client, _HANDLER_ATTR, None)
        if getattr(current, "_kira_bridge_owner", None) is not None:
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
            return False
        try:
            await asyncio.wait_for(fn(interaction_id, 0), timeout=_ACK_TIMEOUT)
            return True
        except Exception as exc:
            self.logger.debug("[QQBOT-BRIDGE] 互动回执失败（已忽略）: %s: %s",
                              type(exc).__name__, exc)
            return False
