"""按钮「查账」工具（v1.6.32）—— 让模型自己查/管按钮的账。

模型能做什么
------------
* **查账**（`qq_button_stats`）：这个按钮被谁点了、各几次、还剩几个名额、是否已截止；
* **截止**（`qq_button_close`）：提前结束（"报名结束"）；
* **重开**（`qq_button_reset`）：清空计数，重新开始一轮；
* **加名额 / 延时**（`qq_button_extend`）。

定位规则（"对应账"）
--------------------
优先用 `message_id`（精确到某一条消息），否则用 **当前会话 + button_id / label**；
**命中不唯一时绝不乱猜** —— 返回候选列表让模型自己选（再次调用时带上 `label` 即可）。

本模块只读内存账本（O(1)），不发任何网络请求、不写盘（落盘由插件巡检负责）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

try:
    from core.plugin import logger  # noqa: F401
except Exception:  # pragma: no cover
    import logging as _logging

    logger = _logging.getLogger("qqbot_bridge")  # type: ignore

PLUGIN_ID = "qqbot-fullmsg-bridge"

_SELECTORS = {
    "button_id": {"type": "string", "description": "可选。按钮的 id（消息里 `\"id\":\"b1\"` 那个）"},
    "message_id": {"type": "string", "description": "可选。要查的那条消息的 id（最精确）"},
    "label": {"type": "string", "description": "可选。按钮的文字（render_data.label），用于区分同 id 的多个按钮"},
}


class _ButtonToolBase:
    """共享：拿到插件实例 + 账本 + 定位某一本账。"""

    name = ""
    description = ""
    parameters: Dict[str, Any] = {}

    def __init__(self, ctx=None, **kwargs):
        self.ctx = ctx
        for k, v in (kwargs or {}).items():
            setattr(self, k, v)

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
        """会话 id 的候选（发送端按**原始 openid** 记账；事件里是 `qq:gm:<openid>`）。"""
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
        """定位一本账：``(policy, 候选, 说明)``。"""
        plugin, store = self._store(event)
        if store is None:
            return None, [], "按钮策略功能未启用或插件未加载"
        for sid in self._sids(event):
            hit, cands = store.find(sid, button_id=button_id, message_id=message_id,
                                    label=label)
            if hit is not None:
                return hit, cands, ""
            if cands:
                return None, cands, ""
        return None, [], "当前会话下没有找到按钮账本（这个会话还没发过带策略的按钮？）"

    @staticmethod
    def _candidate_text(cands: List[Any]) -> str:
        lines = ["命中不唯一，从下面挑一个（下次调用带上 label 或 message_id）："]
        import time as _t

        for p in cands:
            lab = p.get("label") or "（无名字）"
            when = _t.strftime("%m-%d %H:%M", _t.localtime(float(p.get("created") or 0)))
            lines.append(
                f"- label={lab}｜已点 {int(p.get('total') or 0)} 次"
                f"｜{'已截止' if p.get('closed') else '进行中'}｜发出于 {when}"
                f"｜按钮 {','.join((p.get('button_ids') or [])[:5])}")
        return "\n".join(lines)


class QQButtonStatsTool(_ButtonToolBase):
    name = "qq_button_stats"
    description = (
        "查一个按钮的账：被点了几次、多少人来点、每个人各几次、还剩几个名额、是否已截止、"
        "什么时候截止。用在你需要知道「谁报名了 / 还剩几个位置 / 是不是已经结束了」的时候。"
        "不给参数时会列当前会话最近的按钮账本让你选。"
    )
    parameters = {
        "type": "object",
        "properties": dict(_SELECTORS, limit={
            "type": "integer",
            "description": "可选。最多列几个人的明细（默认 20，最多 50）"}),
    }

    async def execute(self, event, *args, button_id: str = "", message_id: str = "",
                      label: str = "", limit: int = 20, **kwargs) -> str:
        try:
            import button_policy as BP
        except Exception as exc:                              # noqa: BLE001
            return f"查账失败：策略模块不可用（{exc}）"
        plugin, store = self._store(event)
        if store is None:
            return "查账失败：按钮策略功能未启用。"
        hit, cands, why = self._locate(event, button_id=button_id,
                                       message_id=message_id, label=label)
        if hit is None:
            if cands:
                return self._candidate_text(cands)
            recent = []
            for sid in self._sids(event):
                recent = store.list_for(sid)[:5]
                if recent:
                    break
            if recent:
                return (why + "\n最近几条：" + "\n" +
                        self._candidate_text(recent).split("\n", 1)[1])
            return why
        lim = max(1, min(50, int(limit or 20)))
        st = store.stats(hit, limit=lim)

        def _name(uid: str) -> str:
            try:
                return plugin.identities.lookup_any(str(uid)) or ""
            except Exception:
                return ""

        return BP.render_stats_text(st, name_of=_name)


class QQButtonCloseTool(_ButtonToolBase):
    name = "qq_button_close"
    description = (
        "立刻截止一个按钮（之后没人能再点，点了也不会计数、按策略该不该告诉你由策略决定）。"
        "用在「报名结束 / 活动截止」这类场景。"
    )
    parameters = {
        "type": "object",
        "properties": dict(_SELECTORS, reason={
            "type": "string", "description": "可选。截止原因（会写进账本，便于日后查）"}),
    }

    async def execute(self, event, *args, button_id: str = "", message_id: str = "",
                      label: str = "", reason: str = "manual", **kwargs) -> str:
        plugin, store = self._store(event)
        if store is None:
            return "截止失败：按钮策略功能未启用。"
        hit, cands, why = self._locate(event, button_id=button_id,
                                       message_id=message_id, label=label)
        if hit is None:
            return self._candidate_text(cands) if cands else why
        store.close(hit["key"], str(reason or "manual"))
        lab = hit.get("label") or hit.get("key")
        return (f"已截止「{lab}」：共被点 {int(hit.get('total') or 0)} 次，"
                f"{len(hit.get('users') or {})} 人参与。")


class QQButtonResetTool(_ButtonToolBase):
    name = "qq_button_reset"
    description = "清空一个按钮的计数并重新开启（重开一轮）。用在「上一轮结束、再来一轮」的时候。"
    parameters = {"type": "object", "properties": dict(_SELECTORS)}

    async def execute(self, event, *args, button_id: str = "", message_id: str = "",
                      label: str = "", **kwargs) -> str:
        plugin, store = self._store(event)
        if store is None:
            return "重置失败：按钮策略功能未启用。"
        hit, cands, why = self._locate(event, button_id=button_id,
                                       message_id=message_id, label=label)
        if hit is None:
            return self._candidate_text(cands) if cands else why
        old_total = int(hit.get("total") or 0)
        store.reset(hit["key"])
        return (f"已重置「{hit.get('label') or hit['key']}」（原计数 {old_total} 已清空，"
                "按钮重新可用）。")


class QQButtonExtendTool(_ButtonToolBase):
    name = "qq_button_extend"
    description = (
        "给按钮加名额或延长时间（重新开启已截止的按钮）。"
        "用在「大家太热情，再加几个位置 / 再给半小时」的时候。"
    )
    parameters = {
        "type": "object",
        "properties": dict(_SELECTORS, add_slots={
            "type": "integer", "description": "可选。增加多少个名额（加到原上限上）"},
            minutes={
                "type": "integer", "description": "可选。延长多少分钟（从当前时间起算）"}),
    }

    async def execute(self, event, *args, button_id: str = "", message_id: str = "",
                      label: str = "", add_slots: int = 0, minutes: int = 0,
                      **kwargs) -> str:
        plugin, store = self._store(event)
        if store is None:
            return "调整失败：按钮策略功能未启用。"
        hit, cands, why = self._locate(event, button_id=button_id,
                                       message_id=message_id, label=label)
        if hit is None:
            return self._candidate_text(cands) if cands else why
        if not add_slots and not minutes:
            return "调整失败：请至少给 add_slots（加名额）或 minutes（延时）其中之一。"
        store.extend(hit["key"], max_add=int(add_slots or 0), minutes=int(minutes or 0))
        st = store.stats(hit, limit=1)
        bits = []
        if add_slots:
            bits.append(f"加 {int(add_slots)} 个名额（现共 {st.get('max')} 个）")
        if minutes:
            import time as _t

            bits.append("延时到 " + _t.strftime("%m-%d %H:%M",
                                              _t.localtime(float(hit.get("until") or 0))))
        return f"已调整「{hit.get('label') or hit['key']}」：" + "；".join(bits) + "。"


def build_button_tools(cfg: dict) -> list:
    """按开关返回要注入的工具类。"""
    if not (cfg or {}).get("button_policy_enabled", True):
        return []
    return [QQButtonStatsTool, QQButtonCloseTool, QQButtonResetTool, QQButtonExtendTool]


__all__ = ["build_button_tools", "QQButtonStatsTool", "QQButtonCloseTool",
           "QQButtonResetTool", "QQButtonExtendTool"]
