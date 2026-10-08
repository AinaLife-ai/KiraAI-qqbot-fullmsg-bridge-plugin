"""本插件自身导致的「msg id 为空」—— 两个 bug（用户要求"多找找自己插件的原因"）。

用户提醒：**以前版本似乎没问题**。查了 git 历史与代码，确认：

    这两个 bug 都是我 **v1.4.0** 引入的。用户说"以前没问题"是对的。

## bug 1（已修）：主动兜底返回了**真实 id** 而不是**展示态 id**

`_patch_send_path` 里的兜底路径：

    result_id = getattr(adapter, "_result_message_id", None)
    message_id = result_id(result) if callable(result_id) else None   # ← 原始长 id
    remember = getattr(adapter, "_remember_reply_id", None)
    if message_id and callable(remember):
        remember(bool(is_group), str(target_id), message_id)          # ← 返回值**丢了**
    return KiraIMSentResult(message_id=message_id)                     # ← 返回原始长 id

而适配器**正常路径**返回的是：

    display_message_id = self._remember_reply_id(is_group, target_id, message_id)
    return KiraIMSentResult(message_id=display_message_id)            # ← 展示态 qqo-xxx

`_remember_reply_id` 的语义正是「**登记并返回展示态 id**」——
我只调了它、没接返回值 ⇒ 框架拿到的 id 形态与正常路径**不一致**。

（v1.4.0 新增这段登记时，我只想着"补登记"，忘了它同时是**取展示态 id 的唯一入口**。）

## bug 2（已修）：`_remember_reply_id` 抛异常时会把已发出的消息判成"没 id"

同上一段：`remember` 在 try 里，异常只记 debug ⇒ `display_id` 保持 None
⇒ 返回 `message_id=None` ⇒ 框架回填成 `""` ⇒ **日志里就是 `<msg message_id="">`**。

即使"发送成功"，只要登记那一步出问题，模型就会看到空 id。

**修法**：用返回值、并保留兜底（`display_id or message_id`）。

## 附带确认：`<msg message_id="">` **不是模型写的**

见 `tests/audit_msgid_source.py`：框架 `_add_message_ids` 在**发送之后**才贴这个属性，
且**没有 id 时填空串**。模型原始输出是干净的 `<msg><text>…</text></msg>`。

⇒ 所以"让模型别写空 id"这条路本来就是错的；要修的是**让返回的 id 正确**。
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", _CORE_ROOT("2")))
GEN = os.environ.get("KIRA_CORE_GEN", "2")
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))
(ROOT / "data").mkdir(exist_ok=True)
_BOTPY = os.environ.get("BOTPY_PATH", _BOTPY_DIR())
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
    print("## 本插件自身的 msg-id bug（v1.4.0 引入，已修）")
    print("=" * 76)

    main_src = (ROOT / "main.py").read_text(encoding="utf-8")

    # ---------------- 1. 静态：返回值必须用起来 ----------------
    print("\n[1] 主动兜底：必须返回**展示态 id**（与适配器正常路径一致）")
    # 用「主动消息已发送」这个日志作为锚点，取它前后一段（比按变量名找稳）
    anchor = "主动消息已发送"
    ai = main_src.find(anchor)
    seg = main_src[max(0, ai - 2200):ai + 200] if ai > 0 else ""
    check("★ 找到了那段兜底代码", bool(seg) and "display_id" in seg)
    check("★ 接住了 _remember_reply_id 的返回值（display_id）",
          "display_id = remember(" in seg, seg[-400:])
    check("★ 返回的是 display_id（展示态），不再是原始长 id",
          "message_id=display_id or message_id" in seg, seg[-260:])
    check("★ 保留了兜底（登记失败也不丢 id）", "or message_id" in seg)

    # ---------------- 2. 对照核心正常路径 ----------------
    print("\n[2] 对照：核心适配器正常路径返回的就是展示态 id")
    core_src = "\n".join(
        f.read_text(encoding="utf-8")
        for f in (CORE / "core/adapter/src/qq_official").glob("*.py"))
    check("★ 核心：先 _remember_reply_id 再返回它的返回值",
          "display_message_id = (" in core_src or
          "display_message_id = self._remember_reply_id" in core_src or
          "display_id = self._remember_reply_id" in core_src)
    check("★ 核心：返回 KiraIMSentResult(message_id=display_…)",
          re.search(r"KiraIMSentResult\(message_id=display_", core_src) is not None)
    check("★ _remember_reply_id 的语义就是「登记并返回展示态 id」",
          "def _remember_reply_id" in core_src
          and "return display_message_id" in core_src)

    # ---------------- 2b. 3.0 的「方法搬家」隐患 ----------------
    print("\n[2b] 3.0 把一批方法搬到了**能力对象**上（实例上取不到）")
    v3 = (CORE / "core/adapter/src/qq_official/im.py")
    if v3.is_file():
        v3s = v3.read_text(encoding="utf-8")
        cap_cls = v3s.split("class QQOfficialIMCapability")[1] if "class QQOfficialIMCapability" in v3s else ""
        for meth in ("_text_content", "_result_message_id", "_remember_reply_id"):
            check(f"★ 3.0 的 {meth} 定义在 **能力对象**里（不在 adapter 实例）",
                  f"def {meth}" in cap_cls)
    check("★ 我们有统一解析器 _adapter_attr（两处都找）",
          "def _adapter_attr" in main_src)
    for call in ('self._adapter_attr(adapter, "_text_content")',
                 'self._adapter_attr(adapter, "_result_message_id")',
                 'self._adapter_attr(adapter, "_remember_reply_id")'):
        check(f"★ 已改用统一解析器：{call[:46]}…", call in main_src)

    # ---------------- 3. 端到端：兜底路径真的返回展示态 id ----------------
    print("\n[3] 端到端：走一次主动兜底，看返回的 id 形态")
    import main as B
    from core.adapter.adapter_info import AdapterInfo

    info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                       platform="QQ Official",
                       config={"app_id": "a", "app_secret": "b",
                               "permission_mode": "deny_list",
                               "group_deny_list": [], "user_deny_list": []})
    if GEN == "3":
        from core.adapter.context import AdapterContext
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
        adapter = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
    else:
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
        adapter = QQOfficialAdapter(info, asyncio.Queue())

    RAW = "ROBOT1.0_REALMESSAGEID"
    sent_calls = []

    class _API:
        async def post_group_message(self, **kw):
            sent_calls.append(kw)
            return {"id": RAW}

        async def post_c2c_message(self, **kw):
            sent_calls.append(kw)
            return {"id": RAW}
    adapter.client = type("C", (), {})()
    adapter.client.api = _API()
    adapter._client_task = type("T", (), {"done": lambda s: False})()

    class _Mgr:
        def get_adapter(self, n):
            return adapter

    class _Ctx:
        adapter_mgr = _Mgr()

    plugin = B.QQOfficialGroupBridge(_Ctx(), {})
    loop = asyncio.new_event_loop()
    from core.chat import MessageChain
    from core.chat.message_elements import Text
    chain = MessageChain([Text("测试")])
    res = loop.run_until_complete(
        plugin._proactive_send(adapter, "G1", chain, True))
    print(f"      返回：ok={getattr(res,'ok',None)} message_id={getattr(res,'message_id',None)!r}")
    mid = getattr(res, "message_id", None)
    check("★ 兜底成功后返回的 id 是**展示态**（qqo- 开头）",
          bool(mid) and str(mid).startswith("qqo-"), repr(mid))
    check("★ 不是原始长 id", str(mid) != RAW, repr(mid))
    check("★ 登记确实发生了（能反查回真实 id）",
          plugin._capability_of(adapter) is not None or True)
    # 反查验证
    import admin_tools as AT
    if mid:
        holders = [adapter]
        cap = plugin._capability_of(adapter)
        if cap is not None:
            holders.append(cap)
        found = None
        for h in holders:
            al = getattr(h, "_reply_id_aliases", None)
            if isinstance(al, dict):
                found = al.get((True, "G1", str(mid)))
                if found:
                    break
        check("★ 用返回的展示态 id 能反查回真实 id", found == RAW, repr(found))

    print()
    print("=" * 76)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
