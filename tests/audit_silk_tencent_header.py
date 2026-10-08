import os
"""验证腾讯系 silk 头校验/修正。"""
import os, sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio_silk as A
class L:
    def info(self,*a): print("    INFO:", (a[0]%tuple(a[1:])) if len(a)>1 else a[0])
    def warning(self,*a): print("    WARN:", (a[0]%tuple(a[1:])) if len(a)>1 else a[0])
    def debug(self,*a): pass
P=F=0
def ck(n,c,e=""):
    global P,F
    if c: P+=1; print("  ok   "+n)
    else: F+=1; print(f"  FAIL {n}  {e}")

os.makedirs("/tmp/hdrtest",exist_ok=True)
def W(name, data):
    p=f"/tmp/hdrtest/{name}"; open(p,"wb").write(data); return p

print("═══ 腾讯系头校验 ═══")
# ① 已是腾讯系
p1=W("a.silk", b"\x02#!SILK_V3" + b"\x00"*64)
ck("腾讯系 ⇒ 判 True 且不改动", A._fix_tencent_header(p1, L()) and open(p1,'rb').read(3)==b"\x02#!")

# ② 标准系 ⇒ 应自动补 \x02
p2=W("b.silk", b"#!SILK_V3" + b"\x00"*64)
before=open(p2,'rb').read()
ok=A._fix_tencent_header(p2, L())
after=open(p2,'rb').read()
ck("★ 标准系 ⇒ 自动补 \\x02 头", ok and after[:1]==b"\x02" and after[1:]==before, after[:12].hex())
ck("★ 修完就是腾讯系", after[:10]==b"\x02#!SILK_V3")

# ③ 乱码 ⇒ 判 False（不硬改）
p3=W("c.silk", b"NOTASILK\xff\xff")
ck("乱码 ⇒ 判 False（不硬改）", not A._fix_tencent_header(p3, L()))

# ④ 不存在的文件 ⇒ 不抛
ck("文件不存在 ⇒ False 不抛异常", not A._fix_tencent_header("/tmp/nope.silk", L()))

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
