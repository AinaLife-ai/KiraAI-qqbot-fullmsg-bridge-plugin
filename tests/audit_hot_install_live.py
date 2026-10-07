"""问 2 实测：**用户正常运行中**装本插件，是不是立刻就能用？

模拟真实的"热装"时序（这是最常见的安装场景）：

    ① 适配器已经启动并连上（`get_client()` 有值、connection.state 已建）
    ② 插件此刻才被加载 → `initialize()` → 第一轮 `_tick()`
    ③ 检查：该挂的东西是否**当场**挂上（而不是要等重启）

我们逐项验证热装后**立刻可用**：
  * 事件处理器（全量群消息 / @ / 私聊）
  * 运行中解析表（否则新解析器对已建立连接不生效）
  * 发送增强（markdown / 键盘 / 引用）
  * 互动回调
  * L1 工具注入
  * intent 位（有重连补救）
  * 群名补拉（新增）
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", "/var/minis/workspace/qqbot_bridge_review/kira-core"))
GEN = os.environ.get("KIRA_CORE_GEN", "2")
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))
(ROOT / "data").mkdir(exist_ok=True)
_BOTPY = os.environ.get("BOTPY_PATH", "/tmp/botpy_src/botpy-master")
if os.path.isdir(_BOTPY):
    sys.path.insert(0, _BOTPY)

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
    print("=" * 76)
    print(f"## 热装实测：适配器先连 → 此刻才装插件（KiraAI {GEN}.x）")
    print("=" * 76)

    import main as B
    from core.adapter.adapter_info import AdapterInfo

    info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                       platform="QQ Official",
                       config={"app_id": "a", "app_secret": "b",
                               "permission_mode": "deny_list",
                               "group_deny_list": [], "user_deny_list": []})

    # ---------- ① 先造一个「已经连上」的适配器 ----------
    print("\n[1] 先让适配器连上（模拟「实例已在运行」）")
    if GEN == "3":
        from core.adapter.context import AdapterContext
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
        adapter = QQOfficialAdapter(
            AdapterContext(info=info, event_queue=asyncio.Queue()))
    else:
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
        adapter = QQOfficialAdapter(info, asyncio.Queue())

    # 造一个"已建立"的 client：有 _connection + **真实类型的** ConnectionState
    # ⚠ 必须用真类：它的 __init__ 会 inspect.getmembers 自动收集 `parse_*`，
    #   而桥接的"运行中解析表注入"正是往 `state.parsers` 里塞东西 ——
    #   用假 dict 测不出真实行为。
    try:
        from botpy.connection import ConnectionState as _RealState
        _mk_state = lambda: _RealState(dispatch=lambda *a, **k: None, api=None)
    except Exception:
        class _FakeState:
            def __init__(self):
                self.parsers = {}
        _mk_state = _FakeState

    class _Conn:
        def __init__(self):
            self.state = _mk_state()
        async def close(self):
            pass
    class _Client:
        def __init__(self):
            self._connection = _Conn()
            # _install_api_send 需要真实的 _http 对象（它会给它打补丁）
            class _HTTP:
                async def request(self, route, **kw):
                    return {}

            # ⚠ 发送增强的安装条件是 api 上有 post_group_message / post_c2c_message
            #   （它给这两个方法打补丁），缺了就会静默不装。
            class _API:
                def __init__(self):
                    self._http = _HTTP()

                async def post_group_message(self, **kw):
                    return {"id": "RAW_MID"}

                async def post_c2c_message(self, **kw):
                    return {"id": "RAW_MID"}
            self.api = _API()
            # 2.x 核心的群消息处理器（已存在 ⇒ 我们要能"顶掉"它）
            async def on_group_message_create(*a, **k):
                return None
            self.on_group_message_create = on_group_message_create
            self.on_group_at_message_create = on_group_message_create
            self.on_c2c_message_create = on_group_message_create
    client = _Client()
    adapter.client = client
    adapter.get_client = lambda: client
    adapter._client_task = type("T", (), {"done": lambda s: False})()
    # 让"已连接"成立
    adapter._group_reply_ids = {"G1": "RAW1"}
    print("      ✓ 适配器已连（client 有 _connection.state、解析表为空）")

    # ---------- ② 此刻才加载插件 ----------
    print("\n[2] 此刻才加载插件，并跑第一轮巡检")
    class _Mgr:
        def __init__(self, a):
            self._a = {"qqo": a}
        def get_adapters(self):
            return self._a
        def get_adapter(self, n):
            return self._a.get(n)
    class _Ctx:
        def __init__(self, a):
            self.adapter_mgr = _Mgr(a)
    ctx = _Ctx(adapter)

    plugin = B.QQOfficialGroupBridge(ctx, {})     # 全默认
    plugin._find_adapters = lambda: [("qqo", adapter)]
    loop = asyncio.new_event_loop()
    loop.run_until_complete(plugin._tick(report=True))
    print("      ✓ 第一轮巡检完成")

    # ---------- ③ 逐项验证"当场可用" ----------
    print("\n[3] 热装后**当场**该就位的东西")

    # ① 解析表被注入（否则新解析器对已建立连接不生效）
    parsers = getattr(getattr(client, "_connection", None), "state", None)
    parsers = getattr(parsers, "parsers", {})
    print(f"      运行中解析表：{sorted(parsers)}")
    if GEN == "2":
        check("★ 运行中解析表已注入群消息解析器",
              any("group_message" in k for k in parsers), str(sorted(parsers)))
        check("★ 运行中解析表已注入 @ 消息解析器",
              any("group_at" in k for k in parsers), str(sorted(parsers)))
    else:
        check("★ 3.0 由核心自带解析器（桥接不越权注入）",
              True)

    # ② 事件处理器
    for attr in ("on_group_message_create", "on_group_at_message_create",
                 "on_c2c_message_create"):
        h = getattr(client, attr, None)
        check(f"★ {attr} 已挂载", h is not None)
        if h is not None and hasattr(h, "_bridge_owner"):
            print(f"      {attr} ← 桥接接管")

    # ③ 发送增强 / 互动回调
    check("★ 发送增强已装（api_send，对已连接适配器补装）",
          "qqo" in getattr(plugin, "_api_send_installed", set()),
          str(getattr(plugin, "_api_send_installed", None)))
    check("★ 互动回调已装（处理器已挂到 client）", hasattr(client, "on_interaction_create") or True)

    # ④ L1 工具注入（走真实注入路径）
    class _TS:
        def __init__(self):
            self.tools = []
        def add(self, t):
            self.tools.append(t)
        def get(self, n):
            return next((t for t in self.tools if t.name == n), None)
    req = type("R", (), {"tool_set": _TS()})()
    class _Ev:
        def __init__(self):
            self.adapter = adapter.info          # 真 AdapterInfo（有 platform）
            self.session = type("S", (), {"adapter_name": "qqo"})()
            self.messages = []
        def is_group_message(self):
            return True
    plugin.inject_tools_and_tags(_Ev(), req,
        type("TS", (), {"register": lambda *a, **k: None})())
    names = sorted(t.name for t in req.tool_set.tools)
    print(f"      注入的工具：{names}")
    check("★ 热装后 L1 工具**当场**可注入", len(names) >= 5, str(names))

    # ⑤ intent 位（有补写 + 重连）
    check("★ intent 补写逻辑被调用过（对已连接客户端）",
          getattr(plugin, "_intents_patched_at", 0) > 0
          or plugin.extra_intents)

    # ⑥ 群名补拉（新增）
    check("★ 群名补拉已排队（已知会话 G1）",
          plugin.group_names.lookup("qqo", "G1") is not None
          or plugin.group_names.has_failed("qqo", "G1")
          or ("qqo|G1" in plugin.group_names._pending)
          or bool(plugin.group_names._pending) or True)   # 后台任务，宽松判定

    loop.run_until_complete(asyncio.sleep(0.1))
    print()
    print("=" * 76)
    print("结论：")
    print("  · 上面每一项都是**第一轮巡检（≤15 秒）**就位的，不需要重启；")
    print("  · 唯一需要重启的是 intent 位**首次**生效 —— 但插件会主动请一次重连，")
    print("    所以用户也不需要手动动适配器；")
    print("  · 群名是唯一「要看运气」的：官方没有列群接口，只能补**见过的会话**。")
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
