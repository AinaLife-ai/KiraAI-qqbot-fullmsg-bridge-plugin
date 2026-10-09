"""★★ INTERACTION_CREATE（按钮回调）端到端：从 **botpy 真派发路径** 到回执 + 转事件。

## 为什么值得单开一套（2026-10-10 用户实测事故）

用户点**回调按钮**（action.type=1）时：客户端提示「请求第三方失败」、机器人毫无反应。
排查发现一个**真断点**（不是平台限制）：

    botpy 的派发形状是 `Interaction` **对象**：
        parse_interaction_create(payload) → Interaction(api, payload["id"], payload["d"])
        → _dispatch("interaction_create", 对象) → client.on_interaction_create(对象)
    而我们的 `_body()` 当时只认 dict ⇒ **对象形态被静默丢弃**：
    不回执（客户端一直 loading / 报错）、也不转成消息 ⇒ 用户体感"事件没接进来"。

## 本套件断言（用**真 botpy** 走真派发）

1. `InteractionBridge.install` 能把 handler 挂上（botpy 客户端类里没有原生
   `on_interaction_create` ⇒ 必须由我们挂）；
2. 订阅位正确：我们注入的额外位包含 `1 << 26`，且与 `botpy.flags.Intents.interaction` 一致；
3. **群聊**：走真实派发（`client.ws_dispatch("interaction_create", 对象)`）⇒
   ① 3 秒内回执（`api.on_interaction_result(id, 0)`，id 取 **d.id**）
   ② 转成一条 Kira 事件：target=group_openid、sender=group_member_openid、正文含按钮 data；
4. **单聊**：同上，target/sender 取 user_openid；
5. 原始 dict 形状（`{"d": {...}}`，webhook/兼容用）也能处理；
6. 形状完全不认识时**响亮一次**（不再静默）；
7. 同一 interaction_id 只回执一次（官方：同一 id 只能回应一次）。
"""
import asyncio
import logging
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import botpy_parent as _BOTPY_DIR, bridge_root as _BR  # noqa: E402
from _env import core_root as _CORE_ROOT  # noqa: E402

_sys.path.insert(0, str(_CORE_ROOT("3")))
_sys.path.insert(0, str(_BR()))
_sys.path.insert(0, str(_BOTPY_DIR()))

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class L:
    def __init__(self):
        self.lines = []

    def _p(self, lv, a):
        self.lines.append((lv, (a[0] % tuple(a[1:])) if len(a[1:]) else str(a[0])))

    def info(self, *a):
        self._p("info", a)

    def warning(self, *a):
        self._p("warning", a)

    def debug(self, *a):
        pass


class FakeAPI:
    """只为回执：botpy 的 on_interaction_result 就是 PUT /interactions/{id}。"""

    def __init__(self):
        self.acks = []

    async def on_interaction_result(self, interaction_id, code):
        self.acks.append((interaction_id, code))
        return {}


class FakePlugin:
    def __init__(self):
        self.events = []

    def publish_synthetic_event(self, **kw):
        self.events.append(kw)
        return True


def _client():
    """真 botpy 客户端（不 start，只用来跑真 ws_dispatch）。"""
    from botpy import Client
    from botpy.flags import Intents

    return Client(intents=Intents(public_messages=True, interaction=True))


def _interaction(api, *, itype=11, d_id="INT-1", outer_id="EVENT-OUTER",
                 button_data="/签到", group=None, member=None, user=None):
    """构造一个**真** botpy `Interaction`（与 connection.parse_interaction_create 同构）。"""
    from botpy.interaction import Interaction

    data = {
        "id": d_id,
        "type": itype,
        "scene": "group" if group else "c2c",
        "data": {"type": 1, "resolved": {"button_id": "b1", "button_data": button_data,
                                         "message_id": "MSG1", "user_id": "U9"}},
    }
    if group:
        data["group_openid"] = group
        data["group_member_openid"] = member
    if user:
        data["user_openid"] = user
    return Interaction(api, outer_id, data)


async def main():
    import botpy
    from botpy.flags import Intents

    import interactions as I

    print("═══ 1. 订阅位：我们注入的额外位必须包含互动事件 1<<26 ═══")
    try:
        import main as bridge_main

        bits = bridge_main.QQOfficialGroupBridge._EXTRA_INTENT_BITS
        check("★★ 额外订阅位含 1<<26（互动回调）", bits & (1 << 26) == (1 << 26), hex(bits))
        check("★ 额外订阅位含 1<<24（成员事件）", bits & (1 << 24) == (1 << 24), hex(bits))
        check("★★ 1<<26 与 botpy 的 Intents.interaction 位一致（平台/库口径对齐）",
              Intents.VALID_FLAGS.get("interaction") == (1 << 26),
              str(Intents.VALID_FLAGS.get("interaction")))
    except Exception as exc:
        check("★ 订阅位用例无异常", False, f"{type(exc).__name__}: {exc}")

    print("\n═══ 2. 挂载：botpy 客户端类里没有原生 on_interaction_create ⇒ 我们必须挂上 ═══")
    api = FakeAPI()
    client = _client()
    client.api = api                       # 注入假 api（真 api 没 token 会真发 HTTP）
    plugin = FakePlugin()
    log = L()
    bridge = I.InteractionBridge(plugin, log)
    status = bridge.install(client)
    check("★★ handler 已挂到客户端实例上", status == "attached", status)
    check("★ 类上没有原生实现（否则我们会「让位」）",
          I._HANDLER_ATTR not in type(client).__dict__, str(I._HANDLER_ATTR))
    check("★ 幂等：再挂一次返回 already", bridge.install(client) == "already")

    print("\n═══ 3. 群聊：走**真 botpy 派发**（ws_dispatch）⇒ 回执 + 转事件 ═══")
    payload = _interaction(api, group="G1", member="M1", button_data="/点歌 香香唱歌")
    client.ws_dispatch("interaction_create", payload)
    for _ in range(20):                      # ws_dispatch 是 create_task，等它跑完
        await asyncio.sleep(0.02)
        if api.acks and plugin.events:
            break
    check("★★ 3 秒内回执了（官方硬要求：不回执客户端一直转圈）",
          api.acks == [("INT-1", 0)], str(api.acks))
    check("★★ 转成了一条事件（群聊：target=group_openid / sender=group_member_openid）",
          len(plugin.events) == 1
          and plugin.events[0]["target_id"] == "G1"
          and plugin.events[0]["sender_id"] == "M1"
          and plugin.events[0]["is_group"] is True,
          str(plugin.events[:1]))
    check("★ 正文带上按钮 data（模型能看到「用户点了什么」）",
          "点歌 香香唱歌" in (plugin.events[0]["text"] if plugin.events else ""),
          str(plugin.events[:1]))
    check("★ 回执用的是**内层 d.id**（不是外层信封 id）",
          api.acks and api.acks[0][0] == "INT-1", f"acks={api.acks} 外层=EVENT-OUTER")

    print("\n═══ 4. 单聊：同上（user_openid 作 target/sender）═══")
    api2 = FakeAPI()
    plugin2 = FakePlugin()
    client2 = _client()
    client2.api = api2
    I.InteractionBridge(plugin2, L()).install(client2)
    client2.ws_dispatch("interaction_create",
                        _interaction(api2, d_id="INT-2", user="U-C2C", button_data="/戳香香"))
    for _ in range(20):
        await asyncio.sleep(0.02)
        if api2.acks and plugin2.events:
            break
    check("★★ 单聊：回执 + 事件（target=user_openid）",
          api2.acks == [("INT-2", 0)] and plugin2.events
          and plugin2.events[0]["target_id"] == "U-C2C"
          and plugin2.events[0]["sender_id"] == "U-C2C"
          and plugin2.events[0]["is_group"] is False,
          f"acks={api2.acks} events={plugin2.events[:1]}")

    print("\n═══ 5. 兼容：原始 dict 形状（d 包一层）也能处理 ═══")
    api3 = FakeAPI()
    plugin3 = FakePlugin()
    client3 = _client()
    client3.api = api3
    I.InteractionBridge(plugin3, L()).install(client3)
    client3.ws_dispatch("interaction_create", {"d": {
        "id": "INT-3", "type": 11, "group_openid": "G3", "group_member_openid": "M3",
        "data": {"resolved": {"button_id": "b9", "button_data": "/问卷"}}}})
    for _ in range(20):
        await asyncio.sleep(0.02)
        if api3.acks and plugin3.events:
            break
    check("★ dict 形状：回执 + 事件都对",
          api3.acks == [("INT-3", 0)] and plugin3.events
          and plugin3.events[0]["target_id"] == "G3"
          and "问卷" in plugin3.events[0]["text"],
          f"acks={api3.acks} events={plugin3.events[:1]}")

    print("\n═══ 6. 形状完全不认识 ⇒ 响亮一次（不再静默丢弃）═══")
    api4 = FakeAPI()
    plugin4 = FakePlugin()
    client4 = _client()
    log4 = L()
    I.InteractionBridge(plugin4, log4).install(client4)

    class Weird:
        pass

    await client4.on_interaction_create(Weird())
    await client4.on_interaction_create(Weird())
    _warns = [m for lv, m in log4.lines if lv == "warning" and "形状不认识" in m]
    check("★★ 陌生形状 ⇒ 一条 WARNING（只喊一次）", len(_warns) == 1, str(log4.lines))
    check("★ 陌生形状不炸（事件循环不受影响）", not api4.acks and not plugin4.events)

    print("\n═══ 7. 同一 interaction_id 只回执一次（官方：只能回应一次）═══")
    api5 = FakeAPI()
    plugin5 = FakePlugin()
    client5 = _client()
    client5.api = api5
    I.InteractionBridge(plugin5, L()).install(client5)
    p5 = _interaction(api5, d_id="INT-5", group="G5", member="M5")
    client5.ws_dispatch("interaction_create", p5)
    client5.ws_dispatch("interaction_create", p5)
    for _ in range(20):
        await asyncio.sleep(0.02)
        if len(api5.acks) >= 1:
            break
    await asyncio.sleep(0.1)
    check("★ 回执只有一次（重复派发不重复回执）",
          api5.acks == [("INT-5", 0)], str(api5.acks))

    print("\n═══ 8. 回执超时 < 3 秒（官方硬要求）═══")
    src = open(_os.path.join(str(_BR()), "interactions.py"), encoding="utf-8").read()
    check("★★ _ACK_TIMEOUT 小于 3 秒", I._ACK_TIMEOUT < 3.0, str(I._ACK_TIMEOUT))
    check("★ 回执走 botpy 的 on_interaction_result（PUT /interactions/{id}）",
          "on_interaction_result" in src)

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


_sys.exit(asyncio.run(main()))
