"""按钮「账本」工具（v1.6.37：**四个合一**，用 `action` 传参）。

设计取舍（用户要求：省 token + 描述精简但语义完整）
--------------------------------------------------
四个独立工具合计约 **1914 字符** schema（≈950 tokens），而工具 schema 会进**每一轮**
提示词（agent loop 多步时成倍放大）⇒ 合并成一个 `qq_button`：
一次 700 字符左右，**省掉约 2/3**，且动作语义集中、模型更不易选错工具。

定位规则（"查对应账"）
--------------------
优先 `message_id`（精确到某一条消息），其次 `label`（活动名），再 `button_id`；
**命中不唯一时绝不乱猜** —— 返回候选列表让模型自己选（再次调用时带上 label 即可）。

本模块只读内存账本（O(1)），不发网络请求、不写盘（落盘由插件巡检负责）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:
    from core.plugin import logger  # noqa: F401
except Exception:  # pragma: no cover
    import logging as _logging

    logger = _logging.getLogger("qqbot_bridge")  # type: ignore

# ★★ 2026-10-11 线上实测（bot 说「手边没有那个工具」）：
#   核心 `ToolSet.add()` 的判定是
#       if isinstance(tool, type): tool_inst = tool()
#       elif isinstance(tool, BaseTool): tool_inst = tool
#       else: continue          ← **静默丢弃**
#   ⇒ 工具类**必须继承** `core.utils.tool_utils.BaseTool`，否则永远进不了模型。
try:
    from core.utils.tool_utils import BaseTool
except Exception:  # pragma: no cover
    class BaseTool:  # type: ignore
        def __init__(self, *a, **kw):
            pass

PLUGIN_ID = "qqbot-fullmsg-bridge"

#: 可选的定位参数（三个动作共用，写在 properties 里只占一份体积）
_SELECTORS = {
    "label": {"type": "string", "description": "活动名（优先用它定位）"},
    "button_id": {"type": "string", "description": "按钮 id（如 b1）"},
    "message_id": {"type": "string", "description": "所在消息 id（最精确）"},
}


class QQButtonTool(BaseTool):
    """按钮账本：查账 / 截止 / 重开 / 加名额（一个工具，`action` 传参）。"""

    name = "qq_button"
    description = (
        "按钮账本：查账或管理你发过的报名/抢位按钮。"
        "action=stats 查账（谁点了、各几次、剩几个名额、是否截止）｜close 立刻截止｜"
        "reset 清空重开｜extend 加名额或延时。用 label 或 message_id 指定按钮，不唯一时列候选。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["stats", "close", "reset", "extend"],
                "description": "stats 查账｜close 截止｜reset 重开｜extend 加名额/延时",
            },
            **_SELECTORS,
            "limit": {"type": "integer", "description": "stats: 最多列几人（默认 20）"},
            "add_slots": {"type": "integer", "description": "extend: 加几个名额"},
            "minutes": {"type": "integer", "description": "extend: 延时分钟"},
            "reason": {"type": "string", "description": "close: 原因（可省）"},
        },
        "required": ["action"],
    }

    def __init__(self, *args, ctx=None, **kwargs):
        try:
            super().__init__(*args, ctx=ctx, **kwargs)
        except Exception:
            self.ctx = ctx
        self.ctx = ctx if ctx is not None else getattr(self, "ctx", None)

    # ------------------------------------------------------------------ #
    # 插件 / 账本 / 定位
    # ------------------------------------------------------------------ #
    def _plugin(self, event=None):
        ctx = self.ctx
        if ctx is None and event is not None:
            ctx = getattr(event, "ctx", None) or getattr(
                getattr(event, "session", None), "ctx", None)
        if ctx is None:
            return None
        try:
            return ctx.get_plugin_inst(PLUGIN_ID)
        except Exception:
            return None

    def _store(self, event=None):
        plugin = self._plugin(event)
        if plugin is None:
            return None, None
        store = getattr(plugin, "button_policies", None)
        if store is None or not getattr(plugin, "button_policy_enabled", True):
            return plugin, None
        return plugin, store

    @staticmethod
    def _sids(event) -> List[str]:
        """会话 id 候选（发送端按**原始 openid** 记账；事件里是 `qq:gm:<openid>`）。"""
        out: List[str] = []
        session = getattr(event, "session", None)
        sid = str(getattr(session, "session_id", "") or "")
        if sid:
            out.append(sid)
            tail = sid.rsplit(":", 1)[-1]
            if tail and tail not in out:
                out.append(tail)
        return out

    def _locate(self, event, *, button_id: str = "", message_id: str = "",
                label: str = "") -> Tuple[Optional[Any], List[Any], str]:
        plugin, store = self._store(event)
        if store is None:
            return None, [], "按钮账本未启用（或插件未加载）。"
        for sid in self._sids(event):
            hit, cands = store.find(sid, button_id=button_id, message_id=message_id,
                                    label=label)
            if hit is not None:
                return hit, cands, ""
            if cands:
                return None, cands, ""
        recent = []
        for sid in self._sids(event):
            recent = store.list_for(sid)[:5]
            if recent:
                break
        if recent:
            return None, recent, "当前会话下没找到匹配的按钮账本，这是最近的几条："
        return None, [], "当前会话下没有按钮账本（这个会话还没发过带策略的按钮？）"

    @staticmethod
    def _candidate_text(cands: List[Any], head: str = "") -> str:
        import time as _t

        lines = [head or "命中不唯一，从下面挑一个（下次带上 label 或 message_id）："]
        for p in cands:
            lab = p.get("label") or "（无名字）"
            when = _t.strftime("%m-%d %H:%M", _t.localtime(float(p.get("created") or 0)))
            lines.append(f"- {lab}｜已点 {int(p.get('total') or 0)} 次"
                         f"｜{'已截止' if p.get('closed') else '进行中'}｜{when}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    async def execute(self, event, *args, action: str = "stats", label: str = "",
                      button_id: str = "", message_id: str = "", limit: int = 20,
                      add_slots: int = 0, minutes: int = 0, reason: str = "",
                      **kwargs) -> str:
        act = str(action or "stats").strip().lower()
        if act not in ("stats", "close", "reset", "extend"):
            return f"未知 action={action}（可用：stats / close / reset / extend）"
        try:
            if act == "stats":
                return self._stats(event, label=label, button_id=button_id,
                                   message_id=message_id, limit=limit)
            if act == "close":
                return self._close(event, label=label, button_id=button_id,
                                   message_id=message_id, reason=reason)
            if act == "reset":
                return self._reset(event, label=label, button_id=button_id,
                                   message_id=message_id)
            return self._extend(event, label=label, button_id=button_id,
                                message_id=message_id, add_slots=add_slots, minutes=minutes)
        except Exception as exc:                      # noqa: BLE001
            logger.debug("[QQBOT-BRIDGE] qq_button 执行异常: %s", exc)
            return f"执行失败：{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------ #
    def _stats(self, event, *, label, button_id, message_id, limit) -> str:
        plugin, store = self._store(event)
        if store is None:
            return "查账失败：按钮账本未启用。"
        hit, cands, head = self._locate(event, button_id=button_id,
                                        message_id=message_id, label=label)
        if hit is None:
            return self._candidate_text(cands, head) if cands else head
        import button_policy as BP

        lim = max(1, min(50, int(limit or 20)))

        def _name(uid: str) -> str:
            try:
                return plugin.identities.lookup_any(str(uid)) or ""
            except Exception:
                return ""

        return BP.render_stats_text(store.stats(hit, limit=lim), name_of=_name)

    def _close(self, event, *, label, button_id, message_id, reason) -> str:
        plugin, store = self._store(event)
        if store is None:
            return "截止失败：按钮账本未启用。"
        hit, cands, head = self._locate(event, button_id=button_id,
                                        message_id=message_id, label=label)
        if hit is None:
            return self._candidate_text(cands, head) if cands else head
        store.close(hit["key"], str(reason or "manual"))
        return (f"已截止「{hit.get('label') or hit['key']}」：共被点 "
                f"{int(hit.get('total') or 0)} 次、{len(hit.get('users') or {})} 人参与。")

    def _reset(self, event, *, label, button_id, message_id) -> str:
        plugin, store = self._store(event)
        if store is None:
            return "重置失败：按钮账本未启用。"
        hit, cands, head = self._locate(event, button_id=button_id,
                                        message_id=message_id, label=label)
        if hit is None:
            return self._candidate_text(cands, head) if cands else head
        old = int(hit.get("total") or 0)
        store.reset(hit["key"])
        return f"已重置「{hit.get('label') or hit['key']}」（原计数 {old} 已清空，按钮重新可用）。"

    def _extend(self, event, *, label, button_id, message_id, add_slots, minutes) -> str:
        plugin, store = self._store(event)
        if store is None:
            return "调整失败：按钮账本未启用。"
        hit, cands, head = self._locate(event, button_id=button_id,
                                        message_id=message_id, label=label)
        if hit is None:
            return self._candidate_text(cands, head) if cands else head
        if not add_slots and not minutes:
            return "调整失败：请至少给 add_slots（加名额）或 minutes（延时）之一。"
        store.extend(hit["key"], max_add=int(add_slots or 0), minutes=int(minutes or 0))
        st = store.stats(hit, limit=1)
        bits = []
        if add_slots:
            bits.append(f"加 {int(add_slots)} 个名额（现共 {st.get('max')} 个）")
        if minutes:
            import time as _t

            bits.append("延时到 " + _t.strftime(
                "%m-%d %H:%M", _t.localtime(float(hit.get("until") or 0))))
        return f"已调整「{hit.get('label') or hit['key']}」：" + "；".join(bits) + "。"


def build_button_tools(cfg: dict) -> list:
    """按开关注入工具（**只有一个** —— 四合一，省 token）。"""
    if not (cfg or {}).get("button_policy_enabled", True):
        return []
    return [QQButtonTool]


__all__ = ["build_button_tools", "QQButtonTool"]
