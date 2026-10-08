"""★ 引用误报回归：核心已有 REFIDX 时，**不得**打「没找到」的 WARNING。

## 背景（2026-10-07 线上）

用户日志：

    WARNING [QQBOT-BRIDGE] 机器人想引用 qqo-fe30659912，但没找到对应的 REFIDX
    （已知 0 条：无）—— 本条按普通回复发出

但截图里 **reply 明明是成功的**。

原因：**3.0 的核心自带引用索引**（`im._message_references` +
`im._resolve_reference`，发送时自己填 `message_reference`）。
而桥接在 3.0 上**刻意不接管事件**（核心已自带全量群消息），
⇒ 我们那份 `ref_store` 在 3.0 上**必然是空的** —— 这不是故障。

原实现拿空 store 找不到就报警 ⇒ **误报**。

修法：`_quote_ref_for` **先问核心**（`_resolve_reference`），拿不到再回退到
我们自己的 store；两边都没有才报警（此时措辞也改为「核心与桥接都没有」）。

## 本测试断言

1. 3.0：核心登记过 REFIDX ⇒ `_quote_ref_for` **返回核心的 REFIDX**；
2. 3.0：**不得**打「没找到」的 WARNING；
3. 两边都没有 ⇒ 才返回 None 并报警（2.x 无核心索引，走 store）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import logging
import os
import sys

ROOT = _BR()
GEN = os.environ.get("KIRA_CORE_GEN", "3")
sys.path.insert(0, str(_CORE_ROOT("3")) if GEN == "3" else str(_CORE_ROOT("2")))
sys.path.insert(0, ROOT)
sys.path.insert(0, f"{ROOT}/tests")
sys.path.insert(0, _BOTPY_DIR())

os.makedirs(f"{ROOT}/data", exist_ok=True)
open(f"{ROOT}/data/log.log", "a").close()

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        try:
            self.msgs.append(record.getMessage() % record.args if record.args
                             else record.getMessage())
        except Exception:
            self.msgs.append(str(record.msg))


async def main():
    from core.adapter.adapter_info import AdapterInfo
    from core.chat import MessageChain
    from core.chat.message_elements import Reply, Text

    print(f"═══ 引用误报回归 GEN={GEN} ═══")

    if GEN == "3":
        from core.adapter.context import AdapterContext
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                           platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        ad = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
    else:
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                           platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        ad = QQOfficialAdapter(info, asyncio.Queue())

    class FakeTask:
        def done(self):
            return False

    class API:
        def __init__(self):
            self.calls = []

        async def post_group_message(self, **kw):
            self.calls.append(kw)
            return {"id": "ROBOT1.0_o", "ext_info": {"ref_idx": "REFIDX_o=="}}

        async def post_c2c_message(self, **kw):
            self.calls.append(kw)
            return {"id": "ROBOT1.0_oc", "ext_info": {"ref_idx": "REFIDX_oc=="}}

    ad.client = type("C", (), {})()
    ad.client.api = API()
    ad.client._connection = None
    ad._client_task = FakeTask()

    import main as bridge_main

    class _Mgr:
        def get_adapters(self):
            return {"qqo": ad}

        def get_adapter(self, n):
            return ad if n == "qqo" else None

    class Ctx:
        adapter_mgr = _Mgr()

    p = bridge_main.QQOfficialGroupBridge(
        Ctx(), {"section_basic": {"enabled": True},
                "section_proactive": {"proactive_enabled": True}})
    p._attach(ad, "qqo", {})

    # ---- 场景 A：核心已登记 REFIDX（3.0 生产路径） ----
    if GEN == "3":
        ad.im._message_references[(True, "G1", "RAW-MSG")] = "REFIDX_core=="
        ad.im._reply_id_aliases[(True, "G1", "qqo-display")] = "RAW-MSG"
        chain = MessageChain([Reply("qqo-display"), Text("回复")])

        cap = _Capture()
        import main as _bm
        lg = _bm.logger          # ← bridge 用的是 core.plugin.logger
        lg.addHandler(cap)

        ref = p._quote_ref_for(ad, "G1", chain, True)
        lg.removeHandler(cap)

        check("★ A1 核心已索引 ⇒ _quote_ref_for 返回核心的 REFIDX",
              ref == "REFIDX_core==", f"拿到 {ref!r}")
        miss = [m for m in cap.msgs if "没找到" in m or "都没有" in m]
        check("★★ A2 不得误报「没找到 REFIDX」", not miss, f"误报: {miss[:1]}")

    # ---- 场景 B：两边都没有 ⇒ 才报警 ----
    chain2 = MessageChain([Reply("qqo-不存在"), Text("回复")])
    cap2 = _Capture()
    import main as _bm2
    lg = _bm2.logger
    lg.addHandler(cap2)
    ref2 = p._quote_ref_for(ad, "G1", chain2, True)
    lg.removeHandler(cap2)
    check("B1 两边都没有 ⇒ 返回 None", ref2 is None, f"拿到 {ref2!r}")
    check("B2 并给出可见 WARNING（真问题时不能静默）",
          any("REFIDX" in m for m in cap2.msgs), f"日志: {cap2.msgs[:1]}")

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
