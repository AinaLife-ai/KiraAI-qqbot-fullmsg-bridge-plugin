import os
"""★ 已经是 silk 的文件也必须过腾讯系头校验（这是真 bug 的回归测试）。"""
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
os.makedirs("/tmp/alrtest",exist_ok=True)
import asyncio

print("═══ ★ 标准系 silk 必须被修成腾讯系（模拟用户的 jbf_v2.silk）═══")
p="/tmp/alrtest/std.silk"
orig = b"#!SILK_V3" + b"\x00"*200 + b"\xff\xff"     # silk-wasm 产的标准系
open(p,"wb").write(orig)
out = asyncio.run(A.to_silk_if_needed(p, logger_=L()))
ck("★ 返回了路径（不是 None）", bool(out), repr(out))
data = open(out,'rb').read() if out else b""
ck("★★ 头已被修成腾讯系 \\x02#!SILK_V3", data[:10]==b"\x02#!SILK_V3", data[:12].hex())
ck("★ 只是前面补了 1 个字节，正文一字不差",
   data == b"\x02" + orig, f"{len(data)} vs {len(orig)+1}")

print("\n═══ 已是腾讯系 ⇒ 不动 ═══")
p2="/tmp/alrtest/ten.silk"
open(p2,"wb").write(b"\x02#!SILK_V3" + b"\x00"*200 + b"\xff\xff")
sz_before=os.path.getsize(p2)
out2=asyncio.run(A.to_silk_if_needed(p2, logger_=L()))
ck("★ 腾讯系 silk 原样返回", out2==p2 and os.path.getsize(p2)==sz_before, repr(out2))

print("\n═══ 乱码 silk ⇒ 拒收（按文件发）═══")
p3="/tmp/alrtest/bad.silk"
open(p3,"wb").write(b"NOTASILK" + b"\x00"*100)
ck("★ 头部乱码 ⇒ 返回 None（不硬发）", asyncio.run(A.to_silk_if_needed(p3, logger_=L())) is None)

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
