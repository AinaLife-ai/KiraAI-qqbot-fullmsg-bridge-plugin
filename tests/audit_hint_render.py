"""hint 文案的「渲染安全」验证。

为什么需要这个套件
----------------
配置项的提示文案（hint）会经过三条链路到达用户眼睛，任一条出问题都会显示错乱：

  A. **JSON 层**：schema.json 里字符串必须用 ASCII 双引号分隔，
     内容里若混入 ASCII 双引号/反斜杠就要转义，容易写坏。
  B. **核心层**：`build_fields()` → `to_dict()` → JSON 给前端，不能被破坏。
  C. **前端层**：两版前端**用的是不同渲染方式** ——
     * `ConfigForm` / `ConfigFieldInput`：`{{ hint }}` 纯文本插值（Vue 自动转义）；
     * `ConfigView`：`v-html="highlightSearch(...)"` —— **会解析 HTML**，
       但 `highlightSearch` 内部先 `escapeHtml()`（转义 & < > "），所以安全。

本套件把前端那两个函数**按原样复刻**，用真实 hint 数据跑一遍，
确保引号/尖括号在任何路径下都不会破坏渲染。

约定（写文案时必须遵守）
----------------------
* hint / name 里**只用中文引号**（“ ”），避免 JSON 转义风险；
* **不要写裸尖括号标签**（如 <markdown>）—— 会被转义成可见的 `&lt;markdown&gt;`；
* **不要写 markdown 标记**（**加粗**、`代码`）—— 前端不解析，会原样显示成星号/反引号。
"""
from __future__ import annotations

import html
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
P = HERE.parent / "schema.json"

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


# --------------------------------------------------------------------------- #
# A. JSON 层
# --------------------------------------------------------------------------- #
def part_a(raw: str) -> dict:
    print("=" * 72)
    print("## A. JSON 层（引号是否破坏 schema 解析）")
    print("=" * 72)
    d = json.loads(raw)
    check("schema.json 是合法 JSON（能解析）", isinstance(d, dict))

    bad_roundtrip, quotes = [], set()
    for sec in d.values():
        for _k, f in (sec.get("fields") or {}).items():
            h = f.get("hint", "")
            if json.loads(json.dumps(h, ensure_ascii=False)) != h:
                bad_roundtrip.append(_k)
            for ch in h + f.get("name", ""):
                if ch in "\"'“”":
                    quotes.add(ch)
    check("全部 hint 经过 JSON 序列化→反序列化后一字不差",
          not bad_roundtrip, str(bad_roundtrip))
    check("hint/name 只用中文引号（不需要 JSON 转义）",
          '"' not in quotes and "'" not in quotes,
          f"出现了 {quotes}")
    return d


# --------------------------------------------------------------------------- #
# B. 核心层
# --------------------------------------------------------------------------- #
def part_b(raw: str) -> None:
    print("\n## B. 核心层：build_fields → to_dict → JSON 输出")
    print("=" * 72)
    core = os.environ.get("KIRA_CORE", "")
    if not core:
        print("  skip  需要 KIRA_CORE 指向真实核心源码")
        return
    sys.path.insert(0, str(pathlib.Path(core).parent))
    try:
        from core.config.config_field import build_fields
    except Exception as exc:
        print(f"  skip  导入核心失败（{type(exc).__name__}: {exc}）")
        return

    fields = build_fields(json.loads(raw))
    check("核心 build_fields 能吃下这份 schema", len(fields) > 0, str(len(fields)))

    def leaves(items):
        out = []
        for item in items:
            td = item.to_dict() if hasattr(item, "to_dict") else item
            subs = getattr(item, "fields", None) or td.get("fields")
            if subs:
                out.extend(leaves(list(subs.values()) if isinstance(subs, dict) else subs))
            else:
                out.append(td)
        return out

    flat = leaves(fields)
    check("能取到全部叶子字段", len(flat) >= 20, str(len(flat)))
    back = json.loads(json.dumps(flat, ensure_ascii=False))
    check("核心序列化后再解析仍是合法 JSON", isinstance(back, list))

    with_hint = [f for f in back if str(f.get("hint", "")).strip()]
    check("叶子字段都带着 hint", len(with_hint) >= 20, str(len(with_hint)))
    if with_hint:
        richest = max(with_hint, key=lambda f: str(f.get("hint", "")).count("“"))
        check("含中文引号的 hint 内容完整无损",
              "“" in richest["hint"] and "”" in richest["hint"],
              repr(richest["hint"][:80]))


# --------------------------------------------------------------------------- #
# C. 前端层（复刻 ConfigView.vue 的实现）
# --------------------------------------------------------------------------- #
def escape_html(s: str) -> str:
    """与 ConfigView.vue 的 escapeHtml 完全一致（转义 & < > "）。"""
    if not s:
        return ""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def highlight_search(text: str, term: str = "") -> str:
    """与 ConfigView.vue 的 highlightSearch 完全一致。"""
    if not term or not text:
        return escape_html(text)
    t = escape_html(term)
    esc = escape_html(text)
    return re.sub(f"({re.escape(t)})",
                  '<mark class="bg-yellow-200 dark:bg-yellow-700 px-0.5 rounded">\\1</mark>',
                  esc, flags=re.I)


def part_c(d: dict) -> None:
    print("\n## C. 前端层（复刻两版前端的实现）")
    print("=" * 72)
    samples = [(k, f["hint"]) for sec in d.values()
               for k, f in (sec.get("fields") or {}).items()
               if any(ch in f.get("hint", "") for ch in "“”")]
    check("存在使用中文引号的 hint（有样本可测）", len(samples) > 0, str(len(samples)))

    check("★ v-html 路径：尖括号全部被转义（无标签注入）",
          not [k for k, h in samples if re.search(r"<(?!/?mark)", highlight_search(h))])
    check("★ v-html + 搜索高亮路径：仍无标签注入",
          not [k for k, h in samples
               if re.search(r"<(?!/?mark)", highlight_search(h, "机器人"))])
    check("★ placeholder 属性上下文：双引号已被转义为 &quot;",
          not [k for k, h in samples if '"' in escape_html(h)])

    if samples:
        k, h = samples[0]
        print(f"\n    真实样本（{k}）：")
        print(f"      原始：{h[:70]}…")
        print(f"      v-html 渲染后：{highlight_search(h)[:88]}…")


# --------------------------------------------------------------------------- #
# D. 极端输入
# --------------------------------------------------------------------------- #
def part_d() -> None:
    print("\n## D. 极端输入（假设文案里放入真恶意字符）")
    print("=" * 72)
    nasty = '试一下 "双引号" <script>alert(1)</script> & <img src=x onerror=1> “中文引号”'
    out = highlight_search(nasty)
    check("script 标签被转义（不会执行）", "<script" not in out and "&lt;script" in out)
    check("img onerror 被转义", "<img" not in out and "&lt;img" in out)
    check("& 被转义", "&amp;" in out)
    check("双引号被转义为 &quot;", "&quot;" in out)


def main() -> int:
    raw = P.read_text(encoding="utf-8")
    d = part_a(raw)
    part_b(raw)
    part_c(d)
    part_d()
    print()
    print("=" * 72)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
