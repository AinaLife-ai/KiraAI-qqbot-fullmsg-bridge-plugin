"""★★★ 挂载链路回归：补丁必须在**真实 `_attach` 流程**里被装上。

## 为什么单独写这一个（2026-10-07 第二次踩到同一个坑）

`_patch_send_path` 原来被放在 `_attach` 的 **2.x 段**（`profile.is_v3` 分支之后），
而 3.0 在 `profile.is_v3` 处**提前 return** ⇒ **3.0 上它从未被调用**。

而当时我的验证方式是**手工调一次 `_patch_send_path`** 再看报文 ——
所以"通过"了，但真实挂载流程里根本没跑。用户线上依旧看到
`[Unsupported message element]`。

⇒ **补丁类改动的验证，必须走真实挂载入口**（`_attach`），不能手工调函数自证。
本测试就是钉住这一点：只调 `_attach`，然后断言补丁真的挂到了
**框架真正走的那个对象**上。

如果将来有人再把 `_patch_send_path` 挪到某个世代分支的后面，本测试会红。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import os
import sys

ROOT = _BR()
GEN = os.environ.get("KIRA_CORE_GEN", "3")
sys.path.insert(0, str(_CORE_ROOT("3")) if GEN == "3" else str(_CORE_ROOT("2")))
sys.path.insert(0, ROOT)
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


async def main():
    from core.adapter.adapter_info import AdapterInfo
    from core.chat import MessageChain
    from rich_content import MarkdownText
    from core.chat.message_elements import Text

    print(f"═══ 挂载链路回归 GEN={GEN} ═══")

    if GEN == "3":
        from core.adapter.context import AdapterContext
        from core.adapter.capabilities import IMCapability
        from core.adapter.src.qq_official.im import QQOfficialIMCapability
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                           platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
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
    if GEN == "3":
        ad.im._group_reply_ids = {"G1": "RAW_IN"}
    else:
        ad._group_reply_ids = {"G1": "RAW_IN"}

    import main as bridge_main

    class _Mgr:
        def get_adapters(self):
            return {"qqo": ad}

        def get_adapter(self, n):
            return ad if n == "qqo" else None

    class Ctx:
        adapter_mgr = _Mgr()

    # 只走**真实挂载入口** —— 绝不手工调 _patch_send_path
    p = bridge_main.QQOfficialGroupBridge(
        Ctx(), {"section_basic": {"enabled": True},
                "section_proactive": {"proactive_enabled": True}})
    if GEN == "3":
        _before = QQOfficialIMCapability._send_message
    else:
        _before = None
    p._attach(ad, "qqo", {})

    # ---- 断言：补丁真的挂上了（在框架真正走的对象上）----
    check("挂载流程记录了该适配器（_patched_sends）",
          "qqo" in p._patched_sends, f"{list(p._patched_sends)}")

    if GEN == "3":
        cap = ad.get_capability(IMCapability)
        # ★ bound method 与 function 用 `is` 比**永远不等**（假绿）——比较底层函数
        _now = getattr(cap._send_message, "__func__", cap._send_message)
        check("★★ 3.0：capability._send_message 已被替换（真实落点）",
              _now is not _before, "仍是原函数 ⇒ _attach 里没装上")
    else:
        # ★ 判定"真的挂上了"：实例上的 _send_message 已不是类里的原方法
        _cls_orig = type(ad).__dict__.get("_send_message")
        check("★★ 2.x：adapter._send_message 已被替换（真实落点）",
              getattr(ad, "_send_message", None) is not _cls_orig
              and "qqo" in p._patched_sends,
              f"_patched_sends={list(p._patched_sends)}")

    # ---- 端到端：走框架真实发送入口，报文必须是 markdown ----
    if GEN == "3":
        send = ad.get_capability(IMCapability).send_group_message
    else:
        send = ad.send_group_message

    await send("G1", MessageChain([Text("预热")]))
    ad.client.api.calls.clear()
    await send("G1", MessageChain([MarkdownText("## 标题\n正文")]))
    kw = ad.client.api.calls[-1]
    check("★★ 真实挂载后：纯 md 走 markdown（msg_type=2）", kw.get("msg_type") == 2,
          str(kw)[:160])
    check("★★ 不含 [Unsupported message element]",
          "[Unsupported message element]" not in str(kw.get("content") or ""))

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
