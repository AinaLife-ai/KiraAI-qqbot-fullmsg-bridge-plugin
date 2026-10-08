"""★ 新能力：私聊「输入中…」状态（msg_type=6）。官方有、KiraAI 核心没有。

## 依据

* 腾讯官方 Node SDK：`bot.sendTyping(target, 30)`
  —— 源码注释「**仅在 `target.scope === "c2c"` 时可用**」，载荷
  `{msg_type: 6, msg_id, input_notify: {input_type: 1, input_second: N}}`
  （`src/protocol/api/messages.ts`：`input_notify: { input_type: 1, input_second: inputSecond }`）。
* QQ 官方推荐的 Hermes（`gateway/platforms/qqbot/adapter.py`）：`send_typing()`
  —— C2C-only、60 秒时长、50 秒防抖、必须有入站 `msg_id`。

## 本测试断言

1. 单聊 + 有入站 msg_id ⇒ 真的发出 `msg_type=6` 且 `input_notify` 形状正确；
2. 群里**不发**（官方只支持 C2C）；
3. 没有入站 msg_id ⇒ 不发（发了也会被拒）；
4. 50 秒防抖：同会话短时间内只发一次；
5. 发失败**绝不影响**调用方（内部吞掉）；
6. 请求走底层 Route —— **不能用 botpy 的 `post_c2c_message`**
   （它 `payload = locals()`，多传的 `input_notify` 会被静默丢掉）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT

import asyncio
import sys
from types import SimpleNamespace

CORE = str(_CORE_ROOT("3"))
# ⚠ 顺序：先核心、后插件 —— 插件要排在 sys.path[0]，
#   否则 `import main` 会命中**核心仓库根目录的 main.py**（踩过）
sys.path.insert(0, CORE)
sys.path.insert(0, _BR())

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class HTTP:
    def __init__(self, boom=False):
        self.calls = []
        self.boom = boom

    async def request(self, route, **kw):
        if self.boom:
            raise RuntimeError("平台抽风了")
        self.calls.append({"path": getattr(route, "path", ""), "json": kw.get("json") or {}})
        return {"id": "X"}


class Client:
    def __init__(self, boom=False):
        self.api = SimpleNamespace(_http=HTTP(boom))


class Adapter:
    def __init__(self, client, reply_ids):
        self.info = SimpleNamespace(name="qqo")
        self._direct_reply_ids = reply_ids
        self._client = client

    def get_client(self):
        return self._client


def event(is_group: bool, uid: str):
    return SimpleNamespace(
        adapter=SimpleNamespace(name="qqo"),
        message=SimpleNamespace(
            group=SimpleNamespace(group_id="G1") if is_group else None,
            sender=SimpleNamespace(user_id=uid),
        ),
    )


async def main():
    print("═══ 私聊「输入中…」状态 ═══")
    try:
        import main as bridge_main
    except Exception as exc:
        print(f"  skip  需要真实核心（{exc}）")
        return 0

    client = Client()
    adapter = Adapter(client, {"OPENID": "MSGID-1"})

    class Mgr:
        def get_adapter(self, n):
            return adapter if n == "qqo" else None

        def get_adapters(self):
            return {"qqo": adapter}

    ctx = SimpleNamespace(adapter_mgr=Mgr())
    plugin = bridge_main.QQOfficialGroupBridge(
        ctx, {"section_basic": {"enabled": True, "typing_enabled": True}})

    print("\n[1] 单聊 + 有入站 msg_id ⇒ 发出 msg_type=6")
    sent = plugin._maybe_send_typing(event(False, "OPENID"))
    check("★ 已排队", sent is True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    calls = client.api._http.calls
    check("★ 真的发出了一个请求", len(calls) == 1, str(len(calls)))
    body = calls[0]["json"] if calls else {}
    check("★ msg_type=6", body.get("msg_type") == 6, str(body))
    check("★ input_notify 形状正确",
          body.get("input_notify") == {"input_type": 1, "input_second": 60},
          str(body.get("input_notify")))
    check("★ 带上入站 msg_id", body.get("msg_id") == "MSGID-1", str(body.get("msg_id")))
    check("★ 路径是单聊消息接口", calls[0]["path"] == "/v2/users/{openid}/messages",
          calls[0]["path"] if calls else "")
    check("★ msg_seq 与核心的 1..N 错开（避免撞重复）",
          isinstance(body.get("msg_seq"), int) and body["msg_seq"] > 100,
          str(body.get("msg_seq")))

    print("\n[2] 50 秒防抖：同会话短时间内第二次不发")
    check("★ 第二次被防抖挡下",
          plugin._maybe_send_typing(event(False, "OPENID")) is False)
    await asyncio.sleep(0)
    check("★ 请求数仍然是 1", len(client.api._http.calls) == 1, str(len(client.api._http.calls)))

    print("\n[3] 群里**不发**（官方只支持 C2C）")
    check("★ 群事件直接返回 False",
          plugin._maybe_send_typing(event(True, "OPENID")) is False)

    print("\n[4] 没有入站 msg_id ⇒ 不发")
    adapter2 = Adapter(Client(), {})
    mgr2 = SimpleNamespace(get_adapter=lambda n: adapter2, get_adapters=lambda: {"qqo": adapter2})
    plugin2 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=mgr2),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    check("★ 无 msg_id ⇒ 不排队", plugin2._maybe_send_typing(event(False, "OPENID")) is False)

    print("\n[5] 开关关掉 ⇒ 不发")
    plugin3 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(
            get_adapter=lambda n: adapter, get_adapters=lambda: {"qqo": adapter})),
        {"section_basic": {"enabled": True, "typing_enabled": False}})
    check("★ 关掉后不排队", plugin3._maybe_send_typing(event(False, "NEWOPENID")) is False)

    print("\n[6] 发送失败绝不影响调用方（异常被吞）")
    boom_client = Client(boom=True)
    adapter4 = Adapter(boom_client, {"X": "MSGID-2"})
    plugin4 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(
            get_adapter=lambda n: adapter4, get_adapters=lambda: {"qqo": adapter4})),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    sent = plugin4._maybe_send_typing(event(False, "X"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    check("★ 调度成功（不抛异常）", sent is True)
    check("★ 失败被吞掉（任务已结束、无 pending 异常）",
          all(t.done() for t in plugin4._typing_tasks) or plugin4._typing_tasks == [])

    print("\n[7] 不走 botpy 的 post_c2c_message（它会丢掉 input_notify）")
    src = open(_BR() + "/main.py", encoding="utf-8").read()
    seg = src.split("async def _send_typing")[1].split("def _adapter_attr")[0]
    # 去掉文档字符串再判断（注释里提到 post_c2c_message 是"解释为什么不用它"）
    body = seg.split('"""', 2)[2] if seg.count('"""') >= 2 else seg
    check("★★ 用的是底层 Route（不是 post_c2c_message）",
          "Route(" in body and "post_c2c_message" not in body,
          body.strip()[:80])

    print("\n[8] ★★ 群聊里：流式 / 输入中**都不生效**（官方只支持 C2C）")
    check("★ 群事件拿不到 C2C 目标（判据在 main 里共用）",
          bridge_main.QQOfficialGroupBridge._c2c_target_of(event(True, "OPENID")) == "")
    _calls_before = len(client.api._http.calls)
    before_turns = dict(plugin.llm_stream._turns)
    plugin._register_c2c_turn(event(True, "OPENID"), request=object(), target="")
    check("★ 群聊不登记 LLM 流式轮次 ⇒ 旁听通道对群聊零动作",
          plugin.llm_stream._turns == before_turns == {})
    check("★ 群聊不打开流式消息会话", plugin.c2c_stream._sessions == {})
    check("★ 群聊也不会发送「输入中」状态（官方：仅单聊）",
          plugin._maybe_send_typing(event(True, "OPENID")) is False)
    check("★ 群聊没有新增任何请求（既不发输入中、也不发流式）",
          len(client.api._http.calls) == _calls_before,
          str(client.api._http.calls[_calls_before:]))

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
