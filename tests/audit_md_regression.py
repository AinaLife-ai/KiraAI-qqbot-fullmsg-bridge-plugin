"""v1.4.5：回归 —— 「`<reply>` + `<markdown>` 同一条消息」会发出 `[Unsupported message element]`。

用户报：原本 md 发得好好的，更新后变成这样（群里只显示 `[Unsupported message element]`）。

## 根因（已复现）

2.x 的发送补丁 `_patch_send_path` 里，`_send_message` 包装**只设了 `QUOTE_REF`**，
**漏了 `PENDING_MD` / `PENDING_KB`**（3.0 那条路径 `_patch_send_entry` 是有的）。

⇒ `api_send._send` 读不到 `PENDING_MD`
⇒ 不会走 `msg_type=2 + markdown.content`
⇒ 把**原链**交给适配器
⇒ 适配器 `_text_content()` **不认识我们的自定义元素**（`MarkdownText`）
⇒ 拼出 `"[Unsupported message element]"` 当正文发出去（用户看到的就是这句）。

## 为什么"偶尔全坏"

触发条件是**同一条消息里既有 `<reply>` 又有 `<markdown>`**（模型常这么写）。
单独发 md 时…… 其实也会坏；但用户之前主要发"纯 md"，
而且这条路径只在**显式引用**时才走 `_quote_ref_for` 那段 ——
无论哪种，**只要 markdown 提取没传到 api 层就坏**。

## 实测前后对比

    修复前：msg_type=0  content='[Unsupported message element]'
    修复后：msg_type=2  content=None  markdown={'content': '## 标题\\n正文'}

## 修法

在 `_patch_send_path` 的 `_send_message` 里补齐 `PENDING_MD` / `PENDING_KB`
的设置与 reset，**与 3.0 路径完全一致**。
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", "/var/minis/workspace/qqbot_bridge_review/kira-core"))
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
    print("## v1.4.5 回归：reply + markdown ⇒ [Unsupported message element]")
    print("=" * 76)

    import main as B
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
    from core.chat import MessageChain
    from core.chat.message_elements import Reply, Text
    from rich_content import MarkdownText

    src = (ROOT / "main.py").read_text(encoding="utf-8")

    # ---------------- 1. 静态：2.x 路径也要设 PENDING_MD ----------------
    print("\n[1] 静态检查：两条发送路径都要提取 markdown")
    i = src.find("def _patch_send_path")
    seg = src[i:i + 3000] if i > 0 else ""
    check("★ 2.x 的 _send_message 包装里设了 PENDING_MD",
          "PENDING_MD.set(" in seg, seg[:300])
    check("★ 也设了 PENDING_KB", "PENDING_KB.set(" in seg)
    check("★ 有 reset（不污染后续调用）",
          "PENDING_MD.reset(" in seg and "PENDING_KB.reset(" in seg)
    check("★ 提取用的是 split_markdown_and_keyboard",
          "split_markdown_and_keyboard(send_message_obj)" in seg)

    # ---------------- 2. 决定性实测：reply + markdown ----------------
    print("\n[2] 实测：<reply> + <markdown> 同一条消息，实际发出什么")
    info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                       platform="QQ Official",
                       config={"app_id": "a", "app_secret": "b",
                               "permission_mode": "deny_list",
                               "group_deny_list": [], "user_deny_list": []})
    if os.environ.get("KIRA_CORE_GEN") == "3":
        print("      （3.0 走的是核心自己的发送链，本回归只影响 2.x 的补丁路径）")
        return 0

    ad = QQOfficialAdapter(info, asyncio.Queue())
    sent = []

    class API:
        async def post_group_message(self, **kw):
            sent.append(kw)
            return {"id": f"ROBOT1.0_REAL{len(sent)}"}
    ad.client = type("C", (), {})()
    ad.client.api = API()
    ad._client_task = type("T", (), {"done": lambda s: False})()
    ad._group_reply_ids = {"G1": "RAW_INCOMING"}

    class M:
        def get_adapter(self, n):
            return ad

    class C:
        adapter_mgr = M()

    p = B.QQOfficialGroupBridge(C(), {})
    p._install_api_send(ad, "qqo", ad.client)
    p._patch_send_path(ad, "qqo", ad.client)
    loop = asyncio.new_event_loop()

    # 先让机器人发一条（登记别名），再用展示态 id 引用它 —— 与线上一致
    r1 = loop.run_until_complete(
        ad.send_group_message("G1", MessageChain([Text("我是第一条")])))
    mid1 = getattr(r1, "message_id", None)
    check("★ 先发的消息有展示态 id", bool(mid1) and str(mid1).startswith("qqo-"),
          repr(mid1))

    chain = MessageChain([Reply(str(mid1)), MarkdownText("## 标题\n正文")])
    loop.run_until_complete(ad.send_group_message("G1", chain))
    got = sent[-1]
    print(f"      实际报文：msg_type={got.get('msg_type')} "
          f"content={got.get('content')!r} "
          f"markdown={got.get('markdown')!r}")

    check("★★ 走的是 markdown 分支（msg_type=2）", got.get("msg_type") == 2,
          str(got))
    check("★★ content 不是 [Unsupported message element]",
          "[Unsupported message element]" not in str(got.get("content") or ""),
          str(got.get("content")))
    check("★★ markdown 正文被正确带上",
          isinstance(got.get("markdown"), dict)
          and "标题" in str(got["markdown"].get("content")), str(got.get("markdown")))
    check("★ 引用用的是真实 msg_id（不是展示态）",
          got.get("msg_id") != mid1, f"{got.get('msg_id')!r} vs {mid1!r}")

    # ---------------- 3. 单发 md（无 reply）也要正常 ----------------
    print("\n[3] 对照：单独发 markdown（无 reply）")
    sent.clear()
    loop.run_until_complete(ad.send_group_message(
        "G1", MessageChain([MarkdownText("## 只有md")])))
    got2 = sent[-1]
    check("★ 也走 markdown 分支", got2.get("msg_type") == 2, str(got2))
    check("★ 不带占位文本",
          "[Unsupported message element]" not in str(got2.get("content") or ""))

    print()
    print("=" * 76)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
