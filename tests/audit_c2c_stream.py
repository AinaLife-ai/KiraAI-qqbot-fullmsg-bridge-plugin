"""★★ C2C 流式消息（`stream_messages`）：**一条 `<msg>` = 一条消息，绝不合并**。

## 契约（本测试逐条钉死）

KiraAI 核心是**逐条发送**的（`core/message_manager.py`）：

    for action in actions:                 # 每个 <msg> 段
        result = await self.send_message_chain(event.sid, action, ...)
        for handler in sent_handlers: ...  # 每发一条广播一次 ON_MESSAGE_SENT
        await asyncio.sleep(random.uniform(min_message_delay, max_message_delay))

⇒ **分段是模型/框架的意图**（框架甚至刻意在两条之间留随机间隔）。
本插件的铁律：**一次发送 = 一条消息 = 一条流**，两段绝不合并。

## 流式只在"已经预览出半句"时才接管

* 没有预览 ⇒ 走流式与普通发送观感一样（都是一次到位），却多吃一次 API 调用 ⇒
  **直接交给普通发送**（行为与从前完全一致）；
* 有预览 ⇒ 用权威内容把那条消息补全并补 `input_state=10` 收尾（否则它会永远停在"生成中"）。

## 断言

1. 判据保守（群聊/富媒体/md/键盘/引用/过短都不接管）；
2. **没有预览 ⇒ 不接管**（返回 None ⇒ 调用方普通发送，报文与从前一致）；
3. 有预览 ⇒ 接管：首片 `input_state=1` + 收尾 `input_state=10`，内容 = 权威内容；
4. ★★★ **两段 = 两条独立消息**（各自的 `msg_seq` / `stream_msg_id`，内容各自独立，绝不拼接）；
5. 预览与权威内容对不上 ⇒ 预览如实收尾 + 本条普通发送（不显示错文字）；
6. 预览等不到权威内容 ⇒ 空闲自动收尾；
7. 失败 ⇒ 返回 None（消息不丢）+ 连续失败冷却；限流（429）指数退避并推进 index；
8. `close_all()` 干净收尾；关掉开关 ⇒ 完全不接管；
9. api 层集成：没预览时 C2C 纯文本**照旧走普通接口**（兼容性回归）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR

import asyncio
import sys
import time

sys.path.insert(0, _BR())

import api_send  # noqa: E402
import c2c_stream as CS  # noqa: E402

try:                      # 预热：botpy 首次 import 约 0.5s，别让它落进计时敏感区
    import botpy.http  # noqa: F401
except Exception:
    pass

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class _Log:
    def __init__(self):
        self.lines = []

    def _p(self, lv, a):
        self.lines.append((lv, (a[0] % tuple(a[1:])) if len(a) > 1 else str(a[0])))

    def info(self, *a):
        self._p("info", a)

    def warning(self, *a):
        self._p("warning", a)

    def debug(self, *a):
        pass


class HTTP:
    """记录每一片；每条流给一个独立的 id（便于断言"这是两条消息"）。"""

    def __init__(self, fail_times=0, rate_limit_times=0):
        self.frames = []
        self.fail = fail_times
        self.rate = rate_limit_times
        self.n = 0

    async def request(self, route, **kw):
        if self.rate > 0:
            self.rate -= 1
            raise RuntimeError("429 Too Many Requests")
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("Server Disconnected")
        body = kw.get("json") or {}
        self.frames.append(body)
        if body.get("stream_msg_id") is None:
            self.n += 1
            return {"id": f"SMID-{self.n}", "timestamp": "t",
                    "ext_info": {"ref_idx": f"REF-{self.n}"}}
        return {"id": body["stream_msg_id"], "timestamp": "t"}


class Adapter:
    def __init__(self, http):
        self.info = type("I", (), {"name": "qqo"})()
        self._direct_reply_ids = {"OPENID": "MSGID-1"}
        self._http = http

    def get_client(self):
        api = type("A", (), {})()
        api._http = self._http
        return type("C", (), {"api": api})()


class Plugin:
    def __init__(self, enabled=True):
        self.remembered = []

    @staticmethod
    def _adapter_attr(adapter, attr, default=None):
        return getattr(adapter, attr, default)

    def remember_sent_ref(self, *a, **k):
        self.remembered.append(a)


async def main():
    # 正常收尾靠"权威内容到达 / 下一轮开始"；空闲只是**兜底**，别设短
    # （设短会把"模型中途歇几秒"的预览误当结束，导致半句定格 + 后面重复一条）
    CS.IDLE_DONE_SECONDS = 3.0
    CS.MIN_FRAME_INTERVAL = 0.05
    CS.MAX_RATE_LIMIT_RETRIES = 3
    CS.RATE_LIMIT_BASE_DELAY = 0.02

    print("═══ C2C 流式消息：一条 <msg> 一条消息 ═══")

    print("\n[1] 判据保守（宁可不流式，也不改变既有行为）")
    mgr = CS.C2CStreamManager(Plugin(), _Log(), enabled=True)
    plain = {"msg_type": 0, "content": "这是一段足够长的正常回复文本", "msg_id": "M1"}
    check("★ 纯文本私聊 ⇒ 可接管", mgr.eligible("T", plain) == "text")
    check("★ markdown 不接管", mgr.eligible("T", {**plain, "markdown": {"content": "x"}}) is None)
    check("★ 键盘不接管", mgr.eligible("T", {**plain, "keyboard": {"id": "1"}}) is None)
    check("★ 富媒体不接管", mgr.eligible("T", {**plain, "media": {"file_info": "f"}}) is None)
    check("★ 引用不接管",
          mgr.eligible("T", {**plain, "message_reference": {"message_id": "r"}}) is None)
    check("★ 太短不接管（<8 字）", mgr.eligible("T", {"msg_type": 0, "content": "嗯嗯"}) is None)
    check("★ msg_type=2 不接管", mgr.eligible("T", {**plain, "msg_type": 2}) is None)
    check("★ 关掉开关 ⇒ 永不接管",
          CS.C2CStreamManager(Plugin(), _Log(), enabled=False).eligible("T", plain) is None)

    print("\n[2] ★★ 没有预览 ⇒ **不接管**（报文与从前完全一致）")
    http = HTTP()
    ad = Adapter(http)
    r = await mgr.maybe_stream(ad, ad.get_client(), "T0",
                               {"msg_type": 0, "content": "没预览过就直接发的文本", "msg_id": "M1"})
    check("★★ 返回 None（调用方照常普通发送）", r is None, repr(r))
    check("★★ 一个流式请求都没发", http.frames == [], str(http.frames))

    print("\n[3] 有预览 ⇒ 接管：首片 input_state=1 + 收尾 input_state=10")
    http = HTTP()
    ad = Adapter(http)
    mgr = CS.C2CStreamManager(Plugin(), _Log(), enabled=True)
    mgr.observe_raw(ad, ad.get_client(), "T1", "M1", "<msg><text>哥ww 我在呀，这是一句够长的预览")
    await asyncio.sleep(CS.MIN_FRAME_INTERVAL + 0.1)
    check("★ 预览帧已发出（input_state=1）",
          bool(http.frames) and http.frames[0].get("input_state") == 1, str(http.frames[:1]))
    r = await mgr.maybe_stream(ad, ad.get_client(), "T1",
                               {"msg_type": 0, "content": "哥ww 我在呀，这是一句够长的预览",
                                "msg_id": "M1"})
    check("★ 接管成功并返回结果（框架当成一次正常发送）",
          isinstance(r, dict) and str(r.get("id", "")).startswith("SMID-"), str(r))
    check("★★ 收尾帧 input_state=10 且内容 = 权威内容",
          http.frames[-1].get("input_state") == 10
          and http.frames[-1].get("content_raw") == "哥ww 我在呀，这是一句够长的预览",
          str(http.frames[-1]))
    check("★ 同一条流共用一个 msg_seq / stream_msg_id",
          len({f.get("msg_seq") for f in http.frames}) == 1
          and len({f.get("stream_msg_id") for f in http.frames if f.get("stream_msg_id")}) == 1,
          str([(f.get("msg_seq"), f.get("stream_msg_id")) for f in http.frames]))

    print("\n[4] ★★★ 两段 = 两条独立消息（**绝不合并**）")
    http2 = HTTP()
    ad2 = Adapter(http2)
    mgr2 = CS.C2CStreamManager(Plugin(), _Log(), enabled=True)
    # 第 1 段：预览 → 权威
    mgr2.observe_raw(ad2, ad2.get_client(), "T2", "M1", "<msg><text>第一句在这里，够长了吧。")
    await asyncio.sleep(CS.MIN_FRAME_INTERVAL + 0.1)
    await mgr2.maybe_stream(ad2, ad2.get_client(), "T2",
                            {"msg_type": 0, "content": "第一句在这里，够长了吧。", "msg_id": "M1"})
    # 第 2 段：预览 → 权威
    mgr2.observe_raw(ad2, ad2.get_client(), "T2", "M1", "<msg><text>第一句在这里，够长了吧。"
                     "</text></msg><msg><text>第二句接着往下说。")
    await asyncio.sleep(CS.MIN_FRAME_INTERVAL + 0.15)
    await mgr2.maybe_stream(ad2, ad2.get_client(), "T2",
                            {"msg_type": 0, "content": "第二句接着往下说。", "msg_id": "M1"})
    seqs = [f.get("msg_seq") for f in http2.frames]
    check("★★★ 两条消息用**不同的** msg_seq（不是同一条流）",
          len(set(seqs)) == 2, str(seqs))
    ids = {f.get("stream_msg_id") for f in http2.frames if f.get("stream_msg_id")}
    check("★★★ 两条消息各有自己的 stream_msg_id",
          len(ids) == 2, str(ids))
    done = [f for f in http2.frames if f.get("input_state") == 10]
    check("★★ 两条消息各自收尾（各有一个 input_state=10）", len(done) == 2, str(len(done)))
    check("★★★ 第二条的内容**只有第二句**（第一句没有被拼进去）",
          done[-1].get("content_raw") == "第二句接着往下说。",
          repr(done[-1].get("content_raw")))
    check("★★★ 第一条的内容**只有第一句**（没有被第二句覆盖）",
          done[0].get("content_raw") == "第一句在这里，够长了吧。",
          repr(done[0].get("content_raw")))

    print("\n[5] 预览与权威内容对不上 ⇒ 预览如实收尾 + 本条普通发送")
    http3 = HTTP()
    ad3 = Adapter(http3)
    mgr3 = CS.C2CStreamManager(Plugin(), _Log(), enabled=True)
    mgr3.observe_raw(ad3, ad3.get_client(), "T3", "M1", "<msg><text>这是下一段的预览文字")
    await asyncio.sleep(CS.MIN_FRAME_INTERVAL + 0.1)
    r3 = await mgr3.maybe_stream(ad3, ad3.get_client(), "T3",
                                 {"msg_type": 0, "content": "完全对不上的另一段内容", "msg_id": "M1"})
    check("★★ 返回 None（不给框架错误的结果）", r3 is None, repr(r3))
    check("★★ 预览被**如实收尾**（不留「生成中」的消息）",
          http3.frames[-1].get("input_state") == 10
          and http3.frames[-1].get("content_raw") == "这是下一段的预览文字",
          str(http3.frames[-1]))

    print("\n[6] 预览等不到权威内容 ⇒ 空闲自动收尾")
    http4 = HTTP()
    ad4 = Adapter(http4)
    mgr4 = CS.C2CStreamManager(Plugin(), _Log(), enabled=True)
    _idle_save = CS.IDLE_DONE_SECONDS
    CS.IDLE_DONE_SECONDS = 0.15          # 这一节专门测"越过空闲时限"的兜底收尾
    mgr4.observe_raw(ad4, ad4.get_client(), "T4", "M1", "<msg><text>只有预览没有权威版本")
    await asyncio.sleep(CS.MIN_FRAME_INTERVAL + 0.1)
    n = len(http4.frames)
    await asyncio.sleep(0.4)
    CS.IDLE_DONE_SECONDS = _idle_save     # ★ 立刻还原，别污染后面的小节
    check("★ 空闲后补了收尾帧", len(http4.frames) > n and http4.frames[-1].get("input_state") == 10,
          str(len(http4.frames)))
    check("★ 收尾内容是那句预览", http4.frames[-1].get("content_raw") == "只有预览没有权威版本",
          repr(http4.frames[-1].get("content_raw")))

    print("\n[7] ★ token 预览：从原始 XML 里安全抽文字")
    check("★ 正常一段", CS.display_text_of("<msg><text>哥ww 我在</text></msg>") == "哥ww 我在")
    check("★ 还没闭合也能给已成型部分", CS.display_text_of("<msg><text>哥ww 我") == "哥ww 我")
    check("★★ 半截标签绝不外传", CS.display_text_of("<msg><text>哥ww 我<") == "哥ww 我")
    check("★★ 工具调用轮不展示",
          CS.display_text_of('<msg><tool_call>{"name":"x"}</tool_call>') == "")
    check("★ 只取最后一段（前几段由它们自己的消息负责）",
          CS.display_text_of("<msg><text>第一段</text></msg><msg><text>第二段") == "第二段")
    check("★ markdown 段也能预览", CS.display_text_of("<msg><markdown>## 标题") == "## 标题")
    check("★ 空/无正文 ⇒ 空", CS.display_text_of("") == "" and CS.display_text_of("<msg>") == "")

    print("\n[8] 失败 ⇒ 返回 None（回退普通发送）+ 冷却；限流退避")
    bad_http = HTTP(fail_times=99)
    bad_ad = Adapter(bad_http)
    mgr5 = CS.C2CStreamManager(Plugin(), _Log(), enabled=True)
    mgr5.observe_raw(bad_ad, bad_ad.get_client(), "T5", "M1", "<msg><text>这段发不出去的预览")
    await asyncio.sleep(CS.MIN_FRAME_INTERVAL + 0.1)
    r5 = await mgr5.maybe_stream(bad_ad, bad_ad.get_client(), "T5",
                                 {"msg_type": 0, "content": "这段发不出去的预览", "msg_id": "M1"})
    check("★★ 失败时返回 None（消息交给普通路径发，不会丢）", r5 is None, repr(r5))
    for _ in range(3):
        mgr5.observe_raw(bad_ad, bad_ad.get_client(), "T5", "M1", "<msg><text>再来一段失败的预览")
        await asyncio.sleep(0.02)
    check("★ 连续失败后该会话被冷却",
          mgr5.eligible("T5", {"msg_type": 0, "content": "冷却期内不该接管了", "msg_id": "M1"}) is None)
    check("★ 其它会话不受影响",
          mgr5.eligible("T6", {"msg_type": 0, "content": "另一个会话照常可用", "msg_id": "M1"}) == "text")

    rl_http = HTTP(rate_limit_times=2)
    rl_ad = Adapter(rl_http)
    mgr6 = CS.C2CStreamManager(Plugin(), _Log(), enabled=True)
    mgr6.observe_raw(rl_ad, rl_ad.get_client(), "T7", "M1", "<msg><text>被限流也要最终发出去")
    await asyncio.sleep(0.8)          # 等后台节流任务把限流重试跑完
    rl = await mgr6.maybe_stream(rl_ad, rl_ad.get_client(), "T7",
                                 {"msg_type": 0, "content": "被限流也要最终发出去", "msg_id": "M1"})
    check("★ 限流后仍然成功", isinstance(rl, dict), str(rl))
    check("★★ 重试时 index 被推进（官方做法）",
          bool(rl_http.frames) and max(f.get("index", 0) for f in rl_http.frames) >= 2,
          str(rl_http.frames[-1] if rl_http.frames else None))

    print("\n[9] close_all() 收尾且不抛")
    await mgr4.close_all()
    check("★ 不抛异常", True)

    print("\n[10] api 层集成：没有预览时 C2C 纯文本**照旧走普通接口**（兼容性回归）")
    calls = []

    class API2:
        def __init__(self):
            self._http = HTTP()
            self.sent = calls

        async def post_c2c_message(self, **kw):
            self.sent.append(("c2c", kw))
            return {"id": "REAL-1"}

        async def post_group_message(self, **kw):
            self.sent.append(("group", kw))
            return {"id": "REAL-2"}

    api = API2()
    client = type("C", (), {"api": api})()
    plugin = Plugin()
    plugin.c2c_stream = CS.C2CStreamManager(plugin, _Log(), enabled=True)
    patcher = api_send.ApiSendPatcher(plugin, _Log())
    patcher.install(None, "qqo", client)

    await api.post_c2c_message(openid="OPENID", msg_type=0,
                               content="私聊纯文本没有被预览过，应该走普通发送", msg_id="M1")
    check("★★★ 走的还是普通消息接口（报文与从前完全一致）",
          any(k == "c2c" for k, _ in calls) and api._http.frames == [],
          f"calls={calls} stream_frames={api._http.frames}")
    calls.clear()
    await api.post_group_message(group_openid="G", msg_type=0, content="群聊纯文本照旧")
    check("★ 群聊照旧走普通消息接口（官方只支持 C2C）",
          any(k == "group" for k, _ in calls), str(calls))
    calls.clear()
    await api.post_c2c_message(openid="OPENID", msg_type=2, markdown={"content": "# 标题"},
                               content=None, msg_id="M1")
    check("★ markdown 照旧走普通接口", any(k == "c2c" for k, _ in calls), str(calls))

    # 有预览时才接管
    calls.clear()
    plugin.c2c_stream.observe_raw(None, client, "OPENID", "M1",
                                  "<msg><text>这次先有预览的文本")
    await asyncio.sleep(CS.MIN_FRAME_INTERVAL + 0.1)
    await api.post_c2c_message(openid="OPENID", msg_type=0, content="这次先有预览的文本",
                               msg_id="M1")
    check("★★ 有预览 ⇒ 被流式接管（不再发普通消息）",
          not any(k == "c2c" for k, _ in calls) and len(api._http.frames) >= 2,
          f"calls={calls} frames={len(api._http.frames)}")

    patcher.restore("qqo")
    await plugin.c2c_stream.close_all()

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
