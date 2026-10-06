"""仿真验证：模拟核心的真实启动时序，确认新方案能修好两个问题。

复现用户场景
-----------
核心 `lifecycle.py` 的顺序是：
    153-155  adapter_manager.initialize()   # 适配器**先连上**（ws_identify 已发过）
    ...
    217-233  plugin_manager.init()          # 插件**后加载**（我们的补丁在这时才装）

所以仿真里我们：
  1. 先用**旧 intent**（只有 1<<25）建一个假的 BotWebSocket 并"鉴权"（模拟已连接）；
  2. 再加载插件（装补丁）；
  3. 触发一次重连，检查重连时发出的 intent **是否带上了 1<<24 / 1<<26**。

这就是用户「开关都开了却收不到成员事件」的完整复现 + 修复验证。
"""
import asyncio
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path("/var/minis/workspace/qqbot_bridge_review")
CORE = ROOT / "kira-core"
BRIDGE = ROOT / "bridge"
(BRIDGE / "data").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, "/tmp/botpy_src/botpy-master")

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def main():
    print("=" * 72)
    print("## 仿真：适配器先连、插件后加载 —— 新方案能否让订阅位生效")
    print("=" * 72)

    import botpy
    from botpy.gateway import BotWebSocket

    # ---------- 0. 还原干净状态（避免受其它测试影响） ----------
    for attr, mark in (("ws_identify", "_kira_bridge_intent"),
                       ("send_msg", "_kira_bridge_probe")):
        cur = getattr(BotWebSocket, attr, None)
        if getattr(cur, mark, False):
            setattr(BotWebSocket, attr, getattr(cur, "_kira_bridge_orig"))
    if getattr(botpy.Client.start, "_kira_bridge_intents", False):
        delattr(botpy.Client, "start")

    sent_intents = []

    # ---------- 1. 模拟"适配器已经连上"：用旧 intent 鉴权 ----------
    print("\n[1] 复现：适配器用旧 intent 先连上（插件还没加载）")
    gw = BotWebSocket.__new__(BotWebSocket)
    gw._session = {"intent": 1 << 25, "token": None,
                   "shards": {"shard_id": 0, "shard_count": 1}}
    gw._can_reconnect = True        # 真实 botpy 在 __init__ 里设这个（自愈判据要用）
    gw._conn = None
    try:
        await_callable = BotWebSocket.send_msg
        sent_intents.append(gw._session["intent"])
    except Exception:
        pass
    print(f"    首连发出的 intent = {bin(gw._session['intent'])}  （只有 1<<25 群消息）")
    check("首连确实没带成员事件位（复现用户现象）",
          gw._session["intent"] & (1 << 24) == 0)

    # ---------- 2. 插件后加载 ----------
    print("\n[2] 插件后加载 → 装补丁")
    import main as bridge_main

    class FakeAdapterInfo:
        name = "qq"
        platform = "QQ Official"

    class FakeAdapter:
        info = FakeAdapterInfo()
        app_id = "123"

        def get_client(self):
            return None

        def _is_allowed(self, *a, **k):
            return True

    class FakeMgr:
        def __init__(self, a):
            self._a = {"qq": a}

        def get_adapters(self):
            return self._a

        def get_adapter(self, n):
            return self._a.get(n)

    class FakeCtx:
        def __init__(self, a):
            self.adapter_mgr = FakeMgr(a)

    adapter = FakeAdapter()
    plugin = bridge_main.QQOfficialGroupBridge(
        FakeCtx(adapter), {"section_basic": {"enabled": True, "extra_intents": True}})

    check("★ ws_identify 补丁已装", getattr(BotWebSocket.ws_identify, "_kira_bridge_intent", False))
    check("★ send_msg 探针已装", getattr(BotWebSocket.send_msg, "_kira_bridge_probe", False))

    # ---------- 3. 关键验证：重连时 ws_identify 是否带上新位 ----------
    print("\n[3] ★ 关键：重连时发出的 intent")

    from aiohttp import ClientWebSocketResponse

    class FakeWS(ClientWebSocketResponse):
        """必须继承真类：botpy 的 send_msg 会 isinstance 校验，否则不发。"""

        closed = False

        def __init__(self):        # 跳过真类的初始化（我们只要 isinstance 通过）
            pass

        async def send_str(self, data=None, compress=0):
            sent_intents.append(("sent", data))

    gw._conn = FakeWS()

    async def _fake_check_token():
        return None

    gw._session["token"] = types.SimpleNamespace(
        check_token=_fake_check_token, get_string=lambda: "QQBot x")

    asyncio.new_event_loop().run_until_complete(BotWebSocket.ws_identify(gw))

    required = (1 << 24) | (1 << 26)
    check(f"★ 重连鉴权时 session['intent'] 带上了额外订阅位（{bin(required)}）",
          gw._session["intent"] & required == required, bin(gw._session["intent"]))
    check("原有的 1<<25 群消息位没丢",
          gw._session["intent"] & (1 << 25) != 0, bin(gw._session["intent"]))

    # 真正发出去的报文里也带上了
    payload = None
    for item in sent_intents:
        if isinstance(item, tuple) and item[0] == "sent":
            payload = item[1]
    if payload:
        import json as _json
        body = _json.loads(payload)
        sent_val = body.get("d", {}).get("intents")
        check("★ 实际发出的鉴权报文里 intents 含新位",
              sent_val is not None and sent_val & required == required, str(sent_val))
    else:
        check("★ 实际发出的鉴权报文里 intents 含新位", False, "没抓到报文")

    # ---------- 4. 探针是否抓住了网关（重连能力的前提） ----------
    print("\n[4] 探针是否抓到存活网关（主动重连的前提）")
    flag = plugin.profiles.get("__intents__") or {}
    refs = [r() for r in (flag.get("gateways") or [])]
    refs = [r for r in refs if r is not None]
    check("★ send_msg 探针已捕获网关实例", len(refs) >= 1, str(len(refs)))
    check("捕获的网关可读 _can_reconnect（自愈判据）",
          all(hasattr(g, "_can_reconnect") for g in refs))

    # ---------- 5. 关掉开关时不装 ----------
    print("\n[5] 开关关闭时不装补丁")
    for attr, mark in (("ws_identify", "_kira_bridge_intent"),
                       ("send_msg", "_kira_bridge_probe")):
        cur = getattr(BotWebSocket, attr, None)
        if getattr(cur, mark, False):
            setattr(BotWebSocket, attr, getattr(cur, "_kira_bridge_orig"))
    if getattr(botpy.Client.start, "_kira_bridge_intents", False):
        delattr(botpy.Client, "start")

    p2 = bridge_main.QQOfficialGroupBridge(FakeCtx(adapter), {"section_basic": {}})
    check("extra_intents 关时不装 ws_identify 补丁",
          not getattr(BotWebSocket.ws_identify, "_kira_bridge_intent", False))
    check("extra_intents 关时不装探针",
          not getattr(BotWebSocket.send_msg, "_kira_bridge_probe", False))

    print()
    print("=" * 72)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
