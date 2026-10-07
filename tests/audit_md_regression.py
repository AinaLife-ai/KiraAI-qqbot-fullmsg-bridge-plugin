"""md 回归：`[Unsupported message element]` 的两条成因（都修了）。

用户报：原本 md 发得好好的，更新后群里只剩 `[Unsupported message element]`。
用户还提醒：截图里**有明显没带 reply 的 md** 也坏了 —— 促使我找到真凶。

## ★ 真凶（v1.4.6 修）：**主动兜底** `_proactive_send`

它直接把**原链**丢给 `adapter._text_content()`：

    content = text_content(send_message_obj)

而 `_text_content()` **不认识我们的自定义元素**（`MarkdownText` / `KeyboardMarker`），
遇到不认识的就填 `"[Unsupported message element]"` —— 这句被当成正文**真发到群里**。

**触发路径**：模型发 md → 被动回复失败（`msg_id` 过期等）
→ 走主动兜底 → 就坏了。实测复现：

    修复前：msg_type=0  content='[Unsupported message element]'
    修复后：msg_type=2  content=None  markdown={'content': '…'}

## 另一条（v1.4.5 修的，属**冗余防御**）

2.x 的 `_patch_send_path` 里 `_send_message` 包装只设 `QUOTE_REF`、漏了
`PENDING_MD` / `PENDING_KB`。实测**有 `_patch_send_entry` 兜着时不会坏**
（它在外层已经提取并设好），但补上更稳、也更一致。

## 共同教训

**适配器/核心遇到不认识的自定义元素 → 填 `[Unsupported message element]`。**
⇒ **每一条发送路径都必须先提取自定义元素**，不能把原链丢给适配器。
现有四条：2.x `_patch_send_path` / 3.0 `_patch_send_entry` /
**主动兜底 `_proactive_send`** / api 层。加自定义元素时要逐一检查。
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

    # ---------------- 2b. ★ 主动兜底：这才是线上那条的真凶 ----------------
    print("\n[2b] ★ 主动兜底路径（被动 id 过期后走这里）—— 必须也不出占位文本")
    sent.clear()
    chain2 = MessageChain([Reply("qqo-whatever"),
                           MarkdownText("## 香香给哥的 Markdown 大展览 w\n\n**哥最大**")])
    r2 = loop.run_until_complete(p._proactive_send(ad, "G1", chain2, True))
    g2 = sent[-1] if sent else {}
    print(f"      实际报文：msg_type={g2.get('msg_type')} "
          f"content={str(g2.get('content'))[:34]!r} markdown={str(g2.get('markdown'))[:40]!r}")
    check("★★ 主动兜底：走 markdown 分支（msg_type=2）",
          g2.get("msg_type") == 2, str(g2)[:200])
    check("★★ 主动兜底：content 不是 [Unsupported message element]",
          "[Unsupported message element]" not in str(g2.get("content") or ""),
          str(g2.get("content")))
    check("★★ 主动兜底：markdown 正文被正确带上",
          isinstance(g2.get("markdown"), dict)
          and "展览" in str(g2["markdown"].get("content")), str(g2.get("markdown")))

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
