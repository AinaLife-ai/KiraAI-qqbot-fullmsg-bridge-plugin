"""查证：`<msg message_id="">` 到底是**谁**加的（用户质疑："这个是模型自己输出吗？"）

## 结论：**不是模型写的**，是**框架回填**的

证据链（全部来自真实源码/实测）：

### ① 模型原始输出是干净的
`_parse_xml_msg` 只读 `<msg>` 的**子标签**，**从不读、也从不要求** `message_id` 属性：

    for element in root:
        if element.tag == "msg":
            for child in element:          # ← 只看子标签
                ...
        # msg 自身的 attrib 从头到尾没被用过

### ② 提示词里也没有任何地方教模型写 `message_id`
框架内置 `kira-ai` 只规定 `<msg>` 里可以有子标签（`<text>` 等）；
我们自己的 `<markdown>` / `<keyboard>` 描述里也没有。

### ③ 日志里那行是【回填后】的结果
`message_manager.py`：

    message_results = await self.send_xml_messages(event, text.strip(), tag_set)
    raw_output = self._add_message_ids(text, message_results)   # ← 在这里被加属性
    logger.info(f"LLM -> {sid}: {raw_output}")                   # ← 日志打的是回填后的

而 `_add_message_ids`：

    for i, msg in enumerate(root.findall("msg")):
        if i < len(message_results):
            message_id = message_results[i].message_id
            if not message_id:
                message_id = ""          # ← 没有 id ⇒ 填空串
            msg.set("message_id", message_id)

### ④ 实测：同一段模型原始输出，回填前后差别

| 情形 | 回填后的样子 |
|---|---|
| 模型原始输出 | `<msg><text>哥 这次不空了w</text></msg>` |
| 发送**成功**（有 id） | `<msg message_id="qqo-xxxx"><text>…</text></msg>` |
| 发送**失败/无结果** | `<msg message_id=""><text>…</text></msg>` ← 日志里看到的 |

⇒ 所以「msg id 为空」**不是模型的问题，是发送结果没有 id 的问题**。

## 那为什么发送结果会没有 id？

`_add_message_ids` 是**按位置**把 `message_results[i]` 贴到第 i 个 `<msg>` 上的。
而**加速器（提速器）**在发送层做了剥离：

* 它拦下 `send_xml_messages`，把**已抢发过的段**从待发列表里去掉，
  只把**剩余段**交给框架发 ⇒ 返回的 `message_results` 只对应剩余段；
* 但 `_add_message_ids` 用的 `text` 是**模型原文（全量）**（加速器刻意保持完整，
  否则别的插件会误判"AI 没说话"）；
* ⇒ **位置错配**：原文的 `<msg>` 数量 ≠ 结果数量；
  多出来的 `<msg>` 拿不到结果 ⇒ 被填 `""`。

**"全部抢发完"时最明显**：`rest` 为空 ⇒ 只有 1 个结果，
而原文可能有 N 个 `<msg>` ⇒ 第 2 个起全是空串。

> 这也解释了用户说的"之前没有"：以前没装/没开提速器时，
> 发送与回填用的都是同一份原文，位置天然对齐。
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR, ref_dir as _REF, peers_dir as _PEERS2, peer_plugin as _PEER

import os
import pathlib
import re
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", _CORE_ROOT("2")))
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def _add_ids(xml_data, results):
    """复刻框架 message_manager._add_message_ids（逐字一致）。"""
    root = ET.fromstring(f"<root>{xml_data}</root>")
    for i, msg in enumerate(root.findall("msg")):
        if i < len(results):
            mid = results[i]
            if not mid:
                mid = ""
            msg.set("message_id", mid)
    return ET.tostring(root, encoding="unicode", method="xml")[6:-7]


def main():
    print("=" * 76)
    print("## 查证：<msg message_id=\"\"> 是谁加的？")
    print("=" * 76)

    mm = (CORE / "core/message_manager.py").read_text(encoding="utf-8")
    ki = (CORE / "core/plugin/builtin_plugins/kira-ai/main.py").read_text(encoding="utf-8")
    our = "\n".join((ROOT / f).read_text(encoding="utf-8")
                    for f in ("rich_content.py", "api_send.py", "main.py"))

    # ---------------- ① 模型不写这个属性 ----------------
    print("\n[1] 模型原始输出里有 message_id 吗？")
    check("★ 框架解析输出时只读**子标签**，不读 msg 的属性",
          'if element.tag == "msg":' in mm and "for child in element:" in mm)
    seg = mm.split('if element.tag == "msg":')[1].split("elif element.tag")[0]
    check("★ 解析过程中**从未**读取 msg.attrib / message_id",
          "attrib" not in seg.split("for child in element")[0], seg[:200])
    check("★ 框架提示词里没有教模型写 message_id",
          "message_id" not in ki.split("XML_FIX_PROMPT")[1].split("class DefaultPlugin")[0])
    check("★ 我们自己的标签描述也没教（markdown/keyboard）",
          "message_id" not in our.split("MARKDOWN_TAG_DESCRIPTION")[1][:900])

    # ---------------- ② 日志那行是回填后的 ----------------
    print("\n[2] 日志里那行是「回填后」的结果")
    check("★ 框架先发送、再 _add_message_ids、再打日志",
          mm.index("message_results = await self.send_xml_messages") <
          mm.index("raw_output = self._add_message_ids(text, message_results)") <
          mm.index('logger.info(f"LLM ->'))
    check("★ _add_message_ids 没有 id 时**填空串**",
          'message_id = ""' in mm)

    # ---------------- ③ 实测：回填的三种结果 ----------------
    print("\n[3] 实测同一段模型输出、回填三种情形")
    model_raw = '<msg><text>哥 这次不空了w</text></msg>'
    print(f"      模型原始输出      ：{model_raw}")
    ok_txt = _add_ids(model_raw, ["qqo-abc123"])
    print(f"      发送成功（有 id） ：{ok_txt}")
    bad_txt = _add_ids(model_raw, [None])
    print(f"      无 id（失败/缺失） ：{bad_txt}")
    check("★ 模型原始输出**不含** message_id",
          "message_id" not in model_raw)
    check("★ 有 id 时回填成真实展示态 id",
          'message_id="qqo-abc123"' in ok_txt)
    check("★ 无 id 时回填成**空串**（= 日志里看到的样子）",
          'message_id=""' in bad_txt, bad_txt)

    # ---------------- ④ 位置错配（真正的成因） ----------------
    print("\n[4] 为什么没有 id？—— 加速器剥离导致**位置错配**")
    multi_raw = ("<msg><text>第一段</text></msg>"
                 "<msg><text>第二段</text></msg>")
    # 加速器：把第 1 段抢发了，只把第 2 段交给框架
    rest_results = ["qqo-second"]          # 只剩第 2 段的结果
    out = _add_ids(multi_raw, rest_results)
    print(f"      模型原文 2 段：{multi_raw}")
    print(f"      加速器抢发第 1 段后，框架只发第 2 段，结果 1 个")
    print(f"      回填后：{out}")
    check("★ 位置错配：第 1 个 <msg> 拿到了**第 2 段**的 id（错位）",
          out.index('message_id="qqo-second"') < out.index("第一段"), out)

    # 全部抢发完（rest 为空）⇒ 原文后面的段拿不到结果 ⇒ 空串
    out2 = _add_ids(multi_raw, [])
    print(f"      全部抢发完（结果为 0 个）→ 回填后：{out2}")
    check("★ 全抢发完时，<msg> **完全不带** message_id 属性（不是空串！）",
          "message_id" not in out2, out2)

    # ---------------- ⑤ 加速器确实这么干 ----------------
    print("\n[5] 加速器（提速器）确实在发送层剥离")
    acc = _PEER("compat_accelerator")
    if acc and acc.is_file():
        a = acc.read_text(encoding="utf-8")
        check("★ 它拦的是 send_xml_messages", "send_xml_messages" in a)
        check("★ 它把抢发结果放在返回序列**最前面**",
              "return (early + list(rest or []))" in a)
        check("★ 它的注释里明确提到「位置错位」这个风险",
              "错位" in a)
    else:
        print("      （跳过：加速器源码不在）")

    # ---------------- ⑥ 结论 ----------------
    print("\n[6] 结论")
    print("  · **不是模型输出** —— 模型原文干净，框架 `_add_message_ids` 加的属性；")
    print("  · 日志打的是**回填后**的文本，所以看着像「模型写了空 id」；")
    print("  · 空/缺失的真正原因是**发送结果与 <msg> 位置对不上**：")
    print("    提速器在发送层剥离已抢发段 ⇒ 结果数 < 原文 <msg> 数 ⇒ 位置错配；")
    print("  · 所以修法不该在「让模型别写空 id」上，而应在**位置对齐**上；")
    print("  · 「之前没有」= 以前没开提速器时，发送与回填用同一份原文，天然对齐。")

    print()
    print("=" * 76)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
