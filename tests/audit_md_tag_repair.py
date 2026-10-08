
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR
import os
import sys
ROOT=_BR()
sys.path.insert(0, str(_CORE_ROOT(os.environ.get("KIRA_CORE_GEN","3"))))
sys.path.insert(0, ROOT)
import os
os.makedirs(ROOT+"/data",exist_ok=True); open(ROOT+"/data/log.log","a").close()
from rich_content import unwrap_markdown_tags, looks_like_markdown, split_markdown_and_keyboard, MarkdownText
from core.chat import MessageChain
from core.chat.message_elements import Text
P=F=0
def ck(n,c,e=""):
    global P,F
    if c: P+=1; print("  ok   "+n)
    else: F+=1; print(f"  FAIL {n}  {e}")

print("[1] 转义标签剥壳")
raw = "&lt;markdown&gt;\n# 标题\n![图](http://x/a.png)\n&lt;/markdown&gt;"
out = unwrap_markdown_tags(raw)
ck("★ &lt;markdown&gt; 被剥掉", "&lt;" not in out and "# 标题" in out, repr(out[:40]))
ck("裸标签也剥", unwrap_markdown_tags("<markdown>\n# T\n</markdown>") .strip()=="# T")

print("\n[2] 是不是 markdown（用于补救）")
for txt, want in [
  ("# 标题\n正文", True),
  ("- 列表1\n- 列表2", True),
  ("![图](http://x/a.png)", True),
  ("**加粗**", True),
  ("> 引用", True),
  ("1. 有序", True),
  ("哥ww 香香reply好啦", False),
  ("摸摸腿", False),
  ("好的，我知道了", False),
]:
    ck(f"looks_like_markdown({txt[:16]!r}) = {want}", looks_like_markdown(txt)==want)

print("\n[3] ★ 端到端：标签写在 <text> 里 ⇒ 仍按 markdown 发")
chain = MessageChain([Text("&lt;markdown&gt;\n# 🎧 标题\n\n[▶ 播放](http://x/a.mp3)\n\n&gt; 引用\n&lt;/markdown&gt;")])
md, kb, changed = split_markdown_and_keyboard(chain)
ck("★ changed=True（会被当成 markdown 处理）", changed, f"md={md!r}")
ck("★ 内容里没有残留的转义标签", md is not None and "&lt;" not in md, repr(md))
ck("★ 标题与链接都在", md is not None and "# 🎧 标题" in md and "[▶ 播放]" in md)

print("\n[4] 普通聊天不能被误判成 md")
chain2 = MessageChain([Text("哥ww 香香reply好啦")])
md2, kb2, ch2 = split_markdown_and_keyboard(chain2)
ck("普通文本 changed=False（不误伤）", not ch2, f"md={md2!r}")

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
