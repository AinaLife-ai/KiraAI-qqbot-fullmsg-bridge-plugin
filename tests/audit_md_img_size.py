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

print("═══ 尺寸后缀补全 ═══")
cases = [
  ("香香立绘",            "香香立绘 #0 #0",        "无尺寸 ⇒ 补默认"),
  ("香香立绘 #100 #100",  "香香立绘 #100 #100",    "已有尺寸 ⇒ 不动"),
  ("img#208px #320px",   "img#208px #320px",     "官方 px 写法 ⇒ 不动"),
  ("img#618px #249px",   "img#618px #249px",     "官方模板写法 ⇒ 不动"),
  ("图 #0 #0",            "图 #0 #0",              "已是自动缩放 ⇒ 不动"),
  ("",                    "#0 #0",                 "空 alt ⇒ 只有尺寸"),
]
for inp, want, label in cases:
    got = _ensure_size(inp)
    ck(f"{label}: {inp!r} → {got!r}", got == want, f"期望 {want!r}")

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
