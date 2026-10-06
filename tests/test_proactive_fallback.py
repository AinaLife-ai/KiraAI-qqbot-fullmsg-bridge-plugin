"""Standalone tests for the proactive fallback on expired msg_id (v1.2.0).

    python3 tests/test_proactive_fallback.py

背景（线上实测）：跨会话合并路由（session_merger handoff）过来的轮，触发消息
是合成控制消息，适配器只能拿到 5 分钟前的旧 msg_id → 腾讯返回
40034005「回复消息msg_id已过期」→ 旧的主动兜底只认 "needs a received message"
且默认关 → 消息无声丢失。

用最小 core 桩导入真实 main.py（插件类），驱动 _patch_send_path 包装后的
adapter._send_message：

  P1  msg_id 已过期 + proactive 开  → 主动兜底发出 + 死 id 被清
  P2  msg_id 已过期 + proactive 关  → 不兜底，但死 id 仍被清（不再每条白失败）
  P3  主动通道正文带 @ 标记        → 按 markdown（msg_type=2）发送
  P4  主动通道 markdown 失败        → 退回剥掉标记的纯文本
  P5  "needs a received message"   → 兜底仍触发（回归）
"""
import asyncio
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
PLUGIN_DIR = _ROOT if os.path.exists(os.path.join(_ROOT, "main.py")) else os.path.join(_ROOT, "bridge_plugin")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

# ---------------------------------------------------------------- 最小 core 桩
import logging

core = types.ModuleType("core")
core_plugin = types.ModuleType("core.plugin")
core_chat = types.ModuleType("core.chat")
core_elems = types.ModuleType("core.chat.message_elements")
core_utils = types.ModuleType("core.chat.message_utils")


class BasePlugin:
    def __init__(self, ctx, cfg):
        self.ctx = ctx
        self.cfg = cfg


core_plugin.BasePlugin = BasePlugin
core_plugin.logger = logging.getLogger("test")

# 插件会用到钩子装饰器；最小桩里补上（与真核心同形）
class _Priority:
    LOW = -50
    MEDIUM = 0
    HIGH = 50
    SYS_HIGH = 100


class _On:
    @staticmethod
    def llm_request(*a, **kw):
        def _deco(func):
            return func
        if a and callable(a[0]):
            return a[0]
        return _deco

core_plugin.Priority = _Priority
core_plugin.on = _On()


class Text:
    def __init__(self, text=""):
        self.text = text


class At:
    def __init__(self, pid="", nickname=""):
        self.pid = pid
        self.nickname = nickname


class _Unused:
    pass


core_chat.Group = _Unused
core_chat.User = _Unused
core_elems.At = At
core_elems.File = _Unused
core_elems.Image = _Unused
core_elems.Reply = _Unused
core_elems.Text = Text


class KiraIMSentResult:
    def __init__(self, message_id=None, ok=True, err=""):
        self.message_id = message_id
        self.ok = ok
        self.err = err


core_utils.KiraIMSentResult = KiraIMSentResult
core_utils.KiraIMMessage = _Unused
core_utils.KiraMessageEvent = _Unused

sys.modules.setdefault("core", core)
sys.modules["core.plugin"] = core_plugin
sys.modules["core.chat"] = core_chat
sys.modules["core.chat.message_elements"] = core_elems
sys.modules["core.chat.message_utils"] = core_utils

import main as M  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


# ---------------------------------------------------------------- 假适配器
class FakeAPI:
    """记录主动发送的 payload；fail_md=True 时 markdown（msg_type=2）发送抛错。"""

    def __init__(self, fail_md=False):
        self.sent = []
        self.fail_md = fail_md

    async def post_group_message(self, **payload):
        if self.fail_md and payload.get("msg_type") == 2:
            raise RuntimeError("markdown not allowed")
        self.sent.append(payload)
        return {"id": "new-msg-id-1"}

    async def post_c2c_message(self, **payload):
        if self.fail_md and payload.get("msg_type") == 2:
            raise RuntimeError("markdown not allowed")
        self.sent.append(payload)
        return {"id": "new-msg-id-1"}


class FakeClient:
    def __init__(self, api):
        self.api = api


class FakeAdapter:
    """形状对齐 QQOfficialAdapter 里本插件接触的部分。"""

    def __init__(self, send_err, api):
        self.info = types.SimpleNamespace(name="qqo", platform="QQ Official")
        self._group_reply_ids = {"G1": "OLD-EXPIRED-MSG-ID"}
        self._direct_reply_ids = {}
        self._client = FakeClient(api)
        self._send_err = send_err

    def get_client(self):
        return self._client

    async def _send_message(self, target_id, send_message_obj, is_group):
        # 原生实现：被动发送失败（msg_id 过期 / 没有可用 msg_id）
        return KiraIMSentResult(ok=False, err=self._send_err)

    def _text_content(self, send_message_obj):
        return "".join(getattr(e, "text", "") for e in send_message_obj)


def make_plugin(proactive=True):
    cfg = {
        "section_basic": {"enabled": True, "remember_nicknames": False},
        "section_proactive": {"proactive_enabled": proactive, "proactive_min_interval": 0},
    }
    return M.QQOfficialGroupBridge(types.SimpleNamespace(), cfg)


EXPIRED_ERR = "Failed to send QQ official message: 回复消息msg_id已过期"


def run(coro):
    return asyncio.run(coro)


def test_p1_expired_triggers_proactive():
    print("\n[P1] msg_id 过期 + proactive 开 → 主动兜底 + 清死 id")
    plugin = make_plugin(proactive=True)
    adapter = FakeAdapter(EXPIRED_ERR, FakeAPI())
    plugin._patch_send_path(adapter, "qqo", adapter.get_client())

    result = run(adapter._send_message("G1", [Text("哥 在吗")], True))
    check("主动兜底发送成功", bool(getattr(result, "ok", True)), repr(getattr(result, "err", "")))
    check("主动发送不带 msg_id（主动消息语义）",
          len(adapter.get_client().api.sent) == 1
          and "msg_id" not in adapter.get_client().api.sent[0],
          repr(adapter.get_client().api.sent))
    check("过期的群回复 id 已被清除", adapter._group_reply_ids.get("G1") is None,
          repr(adapter._group_reply_ids))


def test_p2_expired_no_proactive_still_purges():
    print("\n[P2] msg_id 过期 + proactive 关 → 不兜底，但死 id 仍被清")
    plugin = make_plugin(proactive=False)
    adapter = FakeAdapter(EXPIRED_ERR, FakeAPI())
    plugin._patch_send_path(adapter, "qqo", adapter.get_client())

    result = run(adapter._send_message("G1", [Text("哥 在吗")], True))
    check("未走主动兜底（保持原失败）", not getattr(result, "ok", True)
          and len(adapter.get_client().api.sent) == 0)
    check("死 id 仍被清除（避免之后每条都白失败）",
          adapter._group_reply_ids.get("G1") is None, repr(adapter._group_reply_ids))


def test_p3_proactive_at_uses_markdown():
    print("\n[P3] 主动通道正文带 @ 标记 → 按 markdown 发送")
    plugin = make_plugin(proactive=True)
    adapter = FakeAdapter(EXPIRED_ERR, FakeAPI())
    plugin._patch_send_path(adapter, "qqo", adapter.get_client())

    result = run(adapter._send_message("G1", [Text("哥ww <@9CD54739> 来啦")], True))
    check("兜底成功", bool(getattr(result, "ok", True)), repr(getattr(result, "err", "")))
    sent = adapter.get_client().api.sent
    check("主动通道按 markdown 发送（msg_type=2）",
          len(sent) == 1 and sent[0].get("msg_type") == 2
          and sent[0].get("markdown", {}).get("content", "").find("<@9CD54739>") >= 0,
          repr(sent))


def test_p4_proactive_markdown_fallback():
    print("\n[P4] 主动通道 markdown 失败 → 退回剥掉标记的纯文本")
    plugin = make_plugin(proactive=True)
    adapter = FakeAdapter(EXPIRED_ERR, FakeAPI(fail_md=True))
    plugin._patch_send_path(adapter, "qqo", adapter.get_client())

    result = run(adapter._send_message("G1", [Text("哥ww <@9CD54739> 来啦")], True))
    check("退回纯文本后发送成功", bool(getattr(result, "ok", True)), repr(getattr(result, "err", "")))
    sent = adapter.get_client().api.sent
    check("最终发出的是剥掉标记的纯文本（msg_type=0，无 @ 标记残留）",
          len(sent) == 1 and sent[0].get("msg_type") == 0
          and "<@" not in (sent[0].get("content") or "")
          and "qqbot-at-user" not in (sent[0].get("content") or ""),
          repr(sent))


def test_p5_needs_received_message_regression():
    print("\n[P5] needs a received message → 兜底仍触发（回归）")
    err = "QQ official bot needs a received message before replying to this conversation"
    plugin = make_plugin(proactive=True)
    adapter = FakeAdapter(err, FakeAPI())
    adapter._group_reply_ids.clear()  # 这种错误下本来就没有 id
    plugin._patch_send_path(adapter, "qqo", adapter.get_client())

    result = run(adapter._send_message("G1", [Text("主动冒个泡")], True))
    check("主动兜底发送成功", bool(getattr(result, "ok", True)), repr(getattr(result, "err", "")))
    check("走主动通道发出", len(adapter.get_client().api.sent) == 1)


def main():
    test_p1_expired_triggers_proactive()
    test_p2_expired_no_proactive_still_purges()
    test_p3_proactive_at_uses_markdown()
    test_p4_proactive_markdown_fallback()
    test_p5_needs_received_message_regression()
    print(f"\n{'PASSED' if _FAIL == 0 else 'FAILED'} ({_PASS} ok / {_FAIL} fail)")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()
