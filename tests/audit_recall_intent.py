"""验证两处修复：
  A. 撤回：展示态 id（qqo-xxx）能反查成真实 id
  B. intent：插件构造时就预装 Client.start 补丁 + 对已连接客户端补救
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(_BR())
CORE = pathlib.Path(str(_CORE_ROOT("2")))
BRIDGE = pathlib.Path(str(_BR()))
(BRIDGE / "data").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, _BOTPY_DIR())

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
    print("## 修复验证：撤回 id 反查 + intent 时序")
    print("=" * 72)

    import main as bridge_main
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
    from admin_tools import build_tools

    # ---------------- A. 撤回：展示态 id → 真实 id ----------------
    print("\n[A] 撤回：展示态 id 反查")
    info = AdapterInfo(adapter_id="t", enabled=True, name="qq", platform="QQ Official",
                       config={"app_id": "a", "app_secret": "b",
                               "permission_mode": "deny_list",
                               "group_deny_list": [], "user_deny_list": []})
    adapter = QQOfficialAdapter(info, asyncio.Queue())
    calls = []

    class FakeHTTP:
        async def request(self, route, **kw):
            calls.append({"url": route.url, "params": route.parameters})
            return {}

    adapter.client = type("C", (), {})()
    adapter.client.api = type("A", (), {})()
    adapter.client.api._http = FakeHTTP()

    # 模拟核心登记过的别名（展示态 id → 真实 id），与真实适配器同一张表
    REAL = "ROBOT1.0_AbCdEf0123456789AbCdEf0123456789AbCdEf0123456789"
    DISPLAY = adapter._display_message_id(REAL)
    adapter._reply_id_aliases[(True, "G1", DISPLAY)] = REAL

    print(f"    真实 id : {REAL[:36]}…")
    print(f"    展示 id : {DISPLAY}")

    class FakeSession:
        adapter_name = "qq"
        session_type = "gm"
        session_id = "G1"

    class FakeEv:
        def __init__(self):
            self.session = FakeSession()

        def is_group_message(self):
            return True

    class FakeCtxMgr:
        def get_adapter(self, n):
            return adapter

    class FakeCtx:
        adapter_mgr = FakeCtxMgr()

    tools = {t.name: t(ctx=FakeCtx()) for t in build_tools({})}
    ev = FakeEv()

    r = asyncio.new_event_loop().run_until_complete(
        tools["recall_qq_msg"].execute(ev, message_id=DISPLAY))
    used = calls[-1]["params"].get("message_id") if calls else None
    check("★ 展示态 id 被反查成真实 id（不再直接用 qqo-xxx 请求）",
          used == REAL, f"实际发出: {used}")
    check("撤回请求返回成功文案", "成功" in r, r)

    # 真实 id 直接传入时应当原样使用（不误改）
    calls.clear()
    asyncio.new_event_loop().run_until_complete(
        tools["recall_qq_msg"].execute(ev, message_id=REAL))
    check("传真实 id 时原样使用", calls[-1]["params"].get("message_id") == REAL)

    # 查不到的展示态 id：原样传（让官方如实报错，而不是静默乱改）
    calls.clear()
    asyncio.new_event_loop().run_until_complete(
        tools["recall_qq_msg"].execute(ev, message_id="qqo-unknown123"))
    check("查不到映射时原样透传（不编造 id）",
          calls[-1]["params"].get("message_id") == "qqo-unknown123")

    # ---------------- B. intent 时序 ----------------
    print("\n[B] intent：构造时预装 + 已连接客户端补救")
    import botpy

    # 恢复干净的 botpy.Client.start（避免受前面测试影响）
    if getattr(botpy.Client.start, "_kira_bridge_intents", False):
        delattr(botpy.Client, "start")

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

    plugin = bridge_main.QQOfficialGroupBridge(
        FakeCtx(adapter), {"section_basic": {"enabled": True, "extra_intents": True}})
    # 三个类级补丁都应在构造时就装好
    from botpy.gateway import BotWebSocket
    check("★ 构造函数里就装好 ws_identify 补丁（真正的 intent 注入点）",
          getattr(BotWebSocket.ws_identify, "_kira_bridge_intent", False))
    check("★ 构造函数里就装好 send_msg 探针（用来抓存活网关）",
          getattr(BotWebSocket.send_msg, "_kira_bridge_probe", False))
    check("构造函数里也顺带置位 Client.start",
          getattr(botpy.Client.start, "_kira_bridge_intents", False))

    # 模拟"适配器已先连上"：client 有 intents + session 列表
    class FakeConn:
        def __init__(self):
            self._session_list = [{"session_id": "s1", "intent": 1 << 25}]

    class FakeClient:
        def __init__(self):
            self.intents = 1 << 25
            self._connection = FakeConn()

    fc = FakeClient()
    plugin._upgrade_live_client(adapter, "qq", fc)
    want = (1 << 25) | (1 << 24) | (1 << 26)
    check("★ 已连接的客户端被补上订阅位（1<<24 成员 / 1<<26 互动）",
          fc.intents == want, bin(fc.intents))
    plugin._upgrade_live_client(adapter, "qq", fc)
    check("已带上时直接返回（幂等）", fc.intents == want)

    # ws_identify 补丁本身要能把 intent 或上去（不依赖 session 表状态）
    import inspect
    from botpy.gateway import BotWebSocket as _BW
    src_identify = inspect.getsource(_BW.ws_identify)
    check("★ ws_identify 补丁会给 session['intent'] 或上订阅位",
          "intent" in src_identify and "|" in src_identify, src_identify[:80])

    # 关掉开关时不应该动
    p2 = bridge_main.QQOfficialGroupBridge(FakeCtx(adapter), {"section_basic": {"extra_intents": False}})
    fc2 = FakeClient()
    p2._upgrade_live_client(adapter, "qq", fc2)
    check("extra_intents 关时不改客户端", fc2.intents == (1 << 25), bin(fc2.intents))

    print()
    print("=" * 72)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
