"""★★ LLM 流式通道的旁听（`llm_stream_bridge.py`）：**提速的来源就在这里**。

## 为什么需要（也是"这是不是真流式"的答案）

QQ 的 `stream_messages` 本身**不产生任何速度**（它只是"把文字写进哪条消息"的显示通道）。
真正让用户"早看到字"的是 **token 级增量**。KiraAI 核心的回复路径是非流式的，
但**提速器插件**会 patch LLM 客户端、改用 `chat_stream()` 收 SSE
（`/var/minis/shared/acc_bg_20260929/stream_engine.py`）。

所以我们**不动 client.chat()**，只在 `chat_stream` 上**旁听**（提速器的 proxy 明确
"chat_stream 原样透传不包一层"，所以我们是唯一的包装者）：

    LLMClientProxy.chat_stream → self._wrapped.chat_stream(request, **kwargs)
                                 ^^^^^^^^^^^^^^^^^^^ 我们包的就是这一层

⇒ 一次 LLM 调用还是**一次**（不重复请求、不额外烧 token），我们只是把每个 chunk 的
   增量文本顺手投到 QQ 的流式消息上 ⇒ 用户 ~0.5s 就能看到字。

## 本测试断言

1. `install`/`restore` 幂等，且**只包实例**（不改类、不影响其它 client）；
2. **纯透传**：chunk 逐个、原样、同序；内层异常照样抛；结束语义不变；
3. **身份匹配**：只对登记过的那一个 `LLMRequest` 生效 —— 人设生成器之类
   其它 `chat_stream` 调用**绝不会**被误投到用户会话；
4. 工具调用轮**不产出预览**（那不是给用户看的文字）；
5. 观察过程出错**绝不影响**模型调用；
6. 关掉开关 ⇒ 连包装都不装（行为与从前完全一致）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR

import asyncio
import sys

sys.path.insert(0, _BR())

import llm_stream_bridge as LB  # noqa: E402


PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class Chunk:
    def __init__(self, delta="", tool=False):
        self.delta_text = delta
        self.tool_calls_delta = {"x": 1} if tool else None


class FakeClient:
    """冒充一个 LLM 客户端：chat_stream 逐个吐 chunk。"""

    def __init__(self, chunks):
        self.chunks = chunks
        self.calls = 0

    async def chat_stream(self, request, **kwargs):
        self.calls += 1
        for c in self.chunks:
            yield c


class FakeManager:
    def __init__(self):
        self.enabled = True
        self.seen = []

    def observe_raw(self, adapter, client, target, msg_id, raw):
        self.seen.append((target, msg_id, raw))


class FakePlugin:
    def __init__(self, manager):
        self.c2c_stream = manager


class Log:
    def __init__(self):
        self.lines = []

    def info(self, *a):
        self.lines.append(("info", a[0] % tuple(a[1:]) if len(a) > 1 else str(a[0])))

    def warning(self, *a):
        self.lines.append(("warn", a[0] % tuple(a[1:]) if len(a) > 1 else str(a[0])))


async def main():
    print("═══ LLM 流式通道旁听（提速来源）═══")

    print("\n[1] 包装与还原（幂等、只包实例）")
    mgr = FakeManager()
    bridge = LB.LLMStreamBridge(FakePlugin(mgr), Log(), enabled=True)
    client = FakeClient([Chunk("<msg><text>你好")])
    orig = client.chat_stream
    check("★ install 返回 True", bridge.install(client) is True)
    check("★ 实例方法已被包装", client.chat_stream is not orig
          and getattr(client.chat_stream, LB.MARK, False))
    check("★ 重复 install 幂等（返回 False）", bridge.install(client) is False)
    other = FakeClient([])
    check("★ 没碰别的 client（只包实例）",
          not getattr(other.chat_stream, LB.MARK, False))
    check("★ 类方法没被改", not getattr(type(client).chat_stream, LB.MARK, False))

    print("\n[2] ★★ 纯透传：chunk 逐个、原样、同序；异常照抛")
    req = object()
    mgr2 = FakeManager()
    b2 = LB.LLMStreamBridge(FakePlugin(mgr2), Log(), enabled=True)
    c2 = FakeClient([Chunk("a"), Chunk("b"), Chunk("c")])
    b2.install(c2)
    b2.begin_turn(req, adapter="AD", target="OPENID", msg_id="MID", client=c2)
    got = [ch.delta_text async for ch in c2.chat_stream(req)]
    check("★★ 三个 chunk 一个不少、顺序不变", got == ["a", "b", "c"], str(got))
    check("★ 只发生一次真实调用（不重复请求、不额外烧 token）", c2.calls == 1, str(c2.calls))
    check("★ 观察器收到了累积文本（a / ab / abc）",
          [r for _t, _m, r in mgr2.seen] == ["a", "ab", "abc"], str(mgr2.seen))
    check("★ 目标与被动消息 id 传对了",
          all(t == "OPENID" and m == "MID" for t, m, _r in mgr2.seen), str(mgr2.seen[:1]))

    class BoomClient:
        async def chat_stream(self, request, **kwargs):
            yield Chunk("x")
            raise RuntimeError("流断了")

    bc = BoomClient()
    b3 = LB.LLMStreamBridge(FakePlugin(FakeManager()), Log(), enabled=True)
    b3.install(bc)
    raised = None
    try:
        async for _ in bc.chat_stream(object()):
            pass
    except RuntimeError as exc:
        raised = str(exc)
    check("★★ 内层异常照样抛出（不改结束/错误语义）", raised == "流断了", repr(raised))

    print("\n[3] ★★★ 身份匹配：只对登记过的那一个 request 生效")
    mgr4 = FakeManager()
    b4 = LB.LLMStreamBridge(FakePlugin(mgr4), Log(), enabled=True)
    c4 = FakeClient([Chunk("用户可见的文字")])
    b4.install(c4)
    turn_req = object()
    b4.begin_turn(turn_req, adapter="AD", target="OPENID", msg_id="MID", client=c4)
    other_req = object()        # 人设生成器等：没登记过的请求
    async for _ in c4.chat_stream(other_req):
        pass
    check("★★★ 没登记的 request **一条预览都不发**（不会把人设文本投给用户）",
          mgr4.seen == [], str(mgr4.seen))
    async for _ in c4.chat_stream(turn_req):
        pass
    check("★ 登记过的 request 正常产出预览", len(mgr4.seen) == 1, str(mgr4.seen))
    check("★ end_turn 之后不再产出", (b4.end_turn(turn_req), len(mgr4.seen))[1] == 1)
    mgr4.seen.clear()
    async for _ in c4.chat_stream(turn_req):
        pass
    check("★ 已结束的轮次不再产出", mgr4.seen == [], str(mgr4.seen))

    print("\n[4] ★★ 工具调用轮不产出预览")
    mgr5 = FakeManager()
    b5 = LB.LLMStreamBridge(FakePlugin(mgr5), Log(), enabled=True)
    c5 = FakeClient([Chunk("<msg><text>看看"), Chunk("", tool=True), Chunk("</text></msg>")])
    b5.install(c5)
    req5 = object()
    b5.begin_turn(req5, adapter="AD", target="OPENID", msg_id="MID", client=c5)
    async for _ in c5.chat_stream(req5):
        pass
    check("★★ 出现工具调用后完全停手（与提速器同一条规矩）",
          all("tool_calls" not in r for _t, _m, r in mgr5.seen), str(mgr5.seen))

    print("\n[5] 观察过程出错绝不影响模型调用")
    class BoomManager:
        enabled = True

        def observe_raw(self, *a, **k):
            raise RuntimeError("观察器炸了")

    b6 = LB.LLMStreamBridge(FakePlugin(BoomManager()), Log(), enabled=True)
    c6 = FakeClient([Chunk("还是能收到")])
    b6.install(c6)
    req6 = object()
    b6.begin_turn(req6, adapter="AD", target="OPENID", msg_id="MID", client=c6)
    out = [ch.delta_text async for ch in c6.chat_stream(req6)]
    check("★★ 模型调用照常返回", out == ["还是能收到"], str(out))

    print("\n[6] 关掉开关 ⇒ 连包装都不装（行为与从前完全一致）")
    off = LB.LLMStreamBridge(FakePlugin(FakeManager()), Log(), enabled=False)
    c7 = FakeClient([])
    check("★ install 返回 False（不装）", off.install(c7) is False)
    check("★ 方法保持原样", not getattr(c7.chat_stream, LB.MARK, False))

    print("\n[7] 还原")
    check("★ restore 还原了包装", b2.restore() >= 1)
    check("★ 还原后不再是我们的函数",
          not getattr(c2.chat_stream, LB.MARK, False))

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
