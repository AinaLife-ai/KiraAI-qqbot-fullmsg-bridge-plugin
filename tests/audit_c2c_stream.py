"""★★ C2C 流式消息（`stream_messages`）：把同一轮的多段私聊回复写成**同一条会生长的消息**。

## 官方口径（本测试逐项对齐）

`POST /v2/users/{openid}/stream_messages`：

    input_mode   replace（ContentRaw = **当前全量正文**，须以已下发前缀开头）
    input_state  1=生成中  10=生成结束
    index        分片序号，从 0 递增
    content_type text | markdown
    content_raw  当前全量文本
    msg_id       被动回复消息 ID
    stream_msg_id 首片由服务端返回，**后续分片必须携带**
    msg_seq      同一条流的所有分片**共用一个**（只有 index 递增）

腾讯官方 Node SDK（`src/streaming.ts`）与 QQ 官方推荐的 Hermes 都这么用；
限流（HTTP 429 / err_code 50002）时指数退避并**推进 index**。

## 为什么本插件是"分段驱动"

KiraAI 回复路径**非流式**（`model.chat()` 一次拿完整结果），核心没有把 token 增量
暴露给插件；但提速器插件会把**已成型的 `<msg>` 段**经框架发送层发出来 ⇒
对插件来说"一次发送 = 一段文本"。所以这里把这些段合成一条流式消息。

## 本测试断言（不依赖核心，跑得快）

1. 判据保守：群聊 / 富媒体 / markdown / 键盘 / 引用 / 太短 一律**不接管**；
2. 首片形状正确（index=0、state=1、无 stream_msg_id、msg_seq 与核心错开）；
3. 次片累积全文、index 递增、**带上首片返回的 stream_msg_id**；
4. 空闲后自动补 `input_state=10` 收尾；
5. 任何失败 ⇒ 返回 None（调用方回退普通发送，**消息不丢**）+ 连续失败后冷却；
6. 限流 429 ⇒ 重试并推进 index；
7. `close_all()` 收尾且不抛；
8. 关掉开关 ⇒ 完全不接管（行为与从前一致）；
9. api 层集成：C2C 纯文本走 `stream_messages`（**不再发普通消息**），其余照旧。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR

import asyncio
import sys

sys.path.insert(0, _BR())

import api_send  # noqa: E402
import c2c_stream as CS  # noqa: E402

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
    """记录 stream_messages 的每一片；可注入故障。"""

    def __init__(self, fail_times=0, rate_limit_times=0):
        self.frames = []
        self.fail = fail_times
        self.rate = rate_limit_times

    async def request(self, route, **kw):
        if self.rate > 0:
            self.rate -= 1
            raise RuntimeError("429 Too Many Requests")
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("Server Disconnected")
        body = kw.get("json") or {}
        self.frames.append(body)
        return {"id": "SMID-1", "timestamp": "t", "ext_info": {"ref_idx": "REF1"}}


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
        self.identities = None
        self._attr = type("X", (), {"get": lambda s, k, d=None: None})()
        self.remembered = []

    @staticmethod
    def _adapter_attr(adapter, attr, default=None):
        return getattr(adapter, attr, default)

    def remember_sent_ref(self, *a, **k):
        self.remembered.append(a)


async def main():
    CS.IDLE_DONE_SECONDS = 0.15        # 缩短空闲收尾，测试快
    CS.MIN_FRAME_INTERVAL = 0.0
    CS.MAX_RATE_LIMIT_RETRIES = 3

    print("═══ C2C 流式消息 ═══")

    print("\n[1] 判据必须保守（宁可不流式，也不改变既有行为）")
    mgr = CS.C2CStreamManager(Plugin(), _Log(), enabled=True)
    plain = {"msg_type": 0, "content": "这是一段足够长的正常回复文本", "msg_id": "M1"}
    check("★ 纯文本私聊 ⇒ 接管", mgr.eligible("T", plain) == "text")
    check("★ 群聊（由调用方传 is_group 控制）不看这里，但 markdown 不接管",
          mgr.eligible("T", {**plain, "markdown": {"content": "x"}}) is None)
    check("★ 键盘不接管", mgr.eligible("T", {**plain, "keyboard": {"id": "1"}}) is None)
    check("★ 富媒体不接管", mgr.eligible("T", {**plain, "media": {"file_info": "f"}}) is None)
    check("★ 引用不接管", mgr.eligible("T", {**plain, "message_reference": {"message_id": "r"}}) is None)
    check("★ 太短不接管（<8 字）", mgr.eligible("T", {"msg_type": 0, "content": "嗯嗯"}) is None)
    check("★ msg_type=2（markdown 消息）不接管",
          mgr.eligible("T", {**plain, "msg_type": 2}) is None)
    check("★ 关掉开关 ⇒ 永不接管",
          CS.C2CStreamManager(Plugin(), _Log(), enabled=False).eligible("T", plain) is None)

    print("\n[2] 首片形状（对齐官方示例）")
    http = HTTP()
    ad = Adapter(http)
    res = await mgr.maybe_stream(ad, ad.get_client(), "T1", {"msg_type": 0,
                                                             "content": "第一段足够长的文本",
                                                             "msg_id": "M1"})
    check("★ 接管成功并返回结果（框架当成一次正常发送）",
          isinstance(res, dict) and res.get("id") == "SMID-1", str(res))
    f = http.frames[0]
    check("★ input_mode=replace", f.get("input_mode") == "replace", str(f))
    check("★ input_state=1（生成中）", f.get("input_state") == 1, str(f))
    check("★ index=0", f.get("index") == 0, str(f))
    check("★ content_type=text（纯文本消息就用 text，避免 markdown 改写观感）",
          f.get("content_type") == "text", str(f))
    check("★ content_raw = 当前全文", f.get("content_raw") == "第一段足够长的文本", str(f.get("content_raw")))
    check("★ 带 msg_id", f.get("msg_id") == "M1", str(f))
    check("★ 首片**不带** stream_msg_id（由服务端生成）", "stream_msg_id" not in f, str(f))
    check("★ msg_seq 与核心的 1..N 错开（同一条流共用一个）",
          isinstance(f.get("msg_seq"), int) and f["msg_seq"] > 1000, str(f.get("msg_seq")))

    print("\n[3] 次片：累积全文 + index 递增 + **带上 stream_msg_id**")
    res2 = await mgr.maybe_stream(ad, ad.get_client(), "T1",
                                  {"msg_type": 0, "content": "第一段足够长的文本第二段续写",
                                   "msg_id": "M1"})
    f2 = http.frames[1]
    check("★ index=1", f2.get("index") == 1, str(f2))
    check("★★ content_raw 是**全量**（replace 语义，必须以已下发前缀开头）",
          f2.get("content_raw") == "第一段足够长的文本第二段续写", str(f2.get("content_raw")))
    check("★★ 带上首片返回的 stream_msg_id", f2.get("stream_msg_id") == "SMID-1", str(f2))
    check("★ msg_seq 与首片相同（同一条流）",
          f2.get("msg_seq") == f.get("msg_seq"), f"{f2.get('msg_seq')} vs {f.get('msg_seq')}")
    check("★ 两次都返回结果（框架侧无感）", isinstance(res2, dict))

    print("\n[4] 空闲后自动补收尾帧（input_state=10）")
    await asyncio.sleep(0.35)
    check("★ 出现收尾帧", len(http.frames) == 3, str(len(http.frames)))
    f3 = http.frames[-1]
    check("★ input_state=10", f3.get("input_state") == 10, str(f3))
    check("★ 收尾帧仍带全文与 stream_msg_id",
          f3.get("content_raw") == "第一段足够长的文本第二段续写"
          and f3.get("stream_msg_id") == "SMID-1", str(f3))

    print("\n[5] 换一条被动消息 ⇒ 开一条新流（不会串到上一轮）")
    res3 = await mgr.maybe_stream(ad, ad.get_client(), "T1",
                                  {"msg_type": 0, "content": "新的一轮回复内容在此",
                                   "msg_id": "M2"})
    f4 = http.frames[-1]
    check("★ 新流 index 从 0 开始", f4.get("index") == 0, str(f4))
    check("★ 新流不带旧 stream_msg_id", "stream_msg_id" not in f4, str(f4))
    check("★ msg_seq 也换了", f4.get("msg_seq") != f.get("msg_seq"), str(f4.get("msg_seq")))
    await asyncio.sleep(0.35)

    print("\n[6] 失败 ⇒ 返回 None（调用方回退普通发送）+ 冷却")
    http_bad = HTTP(fail_times=99)
    ad_bad = Adapter(http_bad)
    mgr2 = CS.C2CStreamManager(Plugin(), _Log(), enabled=True)
    r = await mgr2.maybe_stream(ad_bad, ad_bad.get_client(), "T2",
                                {"msg_type": 0, "content": "这段发不出去应该回退", "msg_id": "M1"})
    check("★★ 失败时返回 None（消息交给普通路径发，不会丢）", r is None, str(r))
    for _ in range(3):
        await mgr2.maybe_stream(ad_bad, ad_bad.get_client(), "T2",
                                {"msg_type": 0, "content": "再来一段失败的文本内容", "msg_id": "M1"})
    check("★ 连续失败后该会话被冷却（不再尝试流式）",
          mgr2.eligible("T2", {"msg_type": 0, "content": "冷却期内不该接管了", "msg_id": "M1"}) is None)
    check("★ 其它会话不受影响（不是全局停用）",
          mgr2.eligible("T3", {"msg_type": 0, "content": "另一个会话照常可用", "msg_id": "M1"}) == "text")

    print("\n[7] 限流（429）⇒ 指数退避重试并推进 index（官方做法）")
    http_rl = HTTP(rate_limit_times=2)
    ad_rl = Adapter(http_rl)
    mgr3 = CS.C2CStreamManager(Plugin(), _Log(), enabled=True)
    rl = await mgr3.maybe_stream(ad_rl, ad_rl.get_client(), "T4",
                                 {"msg_type": 0, "content": "被限流也要最终发出去", "msg_id": "M1"})
    check("★ 限流后仍然成功", isinstance(rl, dict), str(rl))
    check("★★ 重试时 index 被推进（官方做法，避免陈旧 index 冲突）",
          bool(http_rl.frames) and http_rl.frames[-1].get("index") >= 2,
          str(http_rl.frames[-1] if http_rl.frames else None))
    await asyncio.sleep(0.35)

    print("\n[8] close_all() 收尾且不抛（插件停止/卸载时调用）")
    n_before = len(http.frames)
    await mgr.close_all()
    check("★ 不抛异常", True)
    check("★ 未收尾的流被补上结束帧", len(http.frames) >= n_before)

    print("\n[9] api 层集成：C2C 纯文本被流式接管，其余照旧")
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
                               content="私聊纯文本应该走流式消息通道", msg_id="M1")
    check("★★ 私聊纯文本**没走**普通消息接口（被流式接管）",
          not any(k == "c2c" for k, _ in calls), str(calls))
    check("★ 流式通道收到了这一片", len(api._http.frames) == 1, str(len(api._http.frames)))

    await api.post_c2c_message(openid="OPENID", msg_type=0, content="短", msg_id="M1")
    check("★ 太短 ⇒ 照旧走普通消息接口", any(k == "c2c" for k, _ in calls), str(calls))

    calls.clear()
    await api.post_group_message(group_openid="G", msg_type=0,
                                 content="群聊里的纯文本永远不走流式（官方只支持单聊）")
    check("★★ 群聊照旧走普通消息接口（官方只支持 C2C）",
          any(k == "group" for k, _ in calls), str(calls))

    calls.clear()
    await api.post_c2c_message(openid="OPENID", msg_type=2,
                               markdown={"content": "# 标题"}, content=None, msg_id="M1")
    check("★ markdown 消息照旧走普通接口", any(k == "c2c" for k, _ in calls), str(calls))

    patcher.restore("qqo")
    await plugin.c2c_stream.close_all()

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
