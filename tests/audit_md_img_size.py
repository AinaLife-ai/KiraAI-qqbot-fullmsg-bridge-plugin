"""验证尺寸后缀的补全逻辑。"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from md_media import _ensure_size
P=F=0
def ck(n,c,e=""):
    global P,F
    if c: P+=1; print("  ok   "+n)
    else: F+=1; print(f"  FAIL {n}  {e}")

print("═══ 图片 alt 原样保留（不再擅自补尺寸）═══")
cases = [
  ("香香立绘",            "香香立绘",              "★ 无尺寸 ⇒ **不擅自加**（v1.5.7 回退）"),
  ("香香立绘 #100 #100",  "香香立绘 #100 #100",    "模型写了尺寸 ⇒ 原样保留"),
  ("img#208px #320px",   "img#208px #320px",     "官方 px 写法 ⇒ 原样保留"),
  ("img#618px #249px",   "img#618px #249px",     "官方模板写法 ⇒ 原样保留"),
  ("图 #0 #0",            "图 #0 #0",              "模型自己写 #0 #0 ⇒ 也不动（可能是它有意为之）"),
  ("",                    "",                      "空 alt ⇒ 保持空"),
]
for inp, want, label in cases:
    got = _ensure_size(inp)
    ck(f"{label}: {inp!r} → {got!r}", got == want, f"期望 {want!r}")

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
