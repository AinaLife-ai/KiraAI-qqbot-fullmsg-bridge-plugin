import os
"""验证：真实尺寸读取 + 补全。"""
import io, os, sys, zlib, struct
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import md_media as M
P=F=0
def ck(n,c,e=""):
    global P,F
    if c: P+=1; print("  ok   "+n)
    else: F+=1; print(f"  FAIL {n}  {e}")

def png(w,h):
    raw=b''.join(b'\x00'+bytes([(x*7)%256,(y*5)%256,128]) for y in range(h) for x in range(w))
    def chunk(t,d):
        c=t+d; return struct.pack('>I',len(d))+c+struct.pack('>I',zlib.crc32(c)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')

print("═══ ① 字节读尺寸 ═══")
for w,h in [(208,320),(618,249),(1,1),(1600,900)]:
    got = M._image_size_from_bytes(png(w,h))
    ck(f"PNG {w}x{h} → {got}", got==(w,h), str(got))

print("\n═══ ② JPEG（用 ffmpeg 造真图）═══")
import subprocess
os.makedirs("/tmp/silktest",exist_ok=True)
jp="/tmp/silktest/sz.jpg"
subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi","-i","color=c=red:s=320x240:d=1","-frames:v","1",jp,"-y"],capture_output=True)
got = M._image_size_from_bytes(open(jp,'rb').read())
ck(f"JPEG 320x240 → {got}", got==(320,240), str(got))

print("\n═══ ③ 本地文件读尺寸 ═══")
lf="/tmp/silktest/sz.png"; open(lf,"wb").write(png(100,50))
ck(f"本地 PNG 100x50 → {M._local_image_size(lf)}", M._local_image_size(lf)==(100,50))

print("\n═══ ④ 补尺寸（关键）═══")
cases = [
 (("香香立绘",208,320), "香香立绘 #208px #320px", "真实尺寸 ⇒ 补 #Wpx #Hpx"),
 (("香香立绘",None,None), "香香立绘",              "★ 拿不到尺寸 ⇒ 不加（别塞假值）"),
 (("img#208px #320px",208,320), "img#208px #320px", "已有尺寸 ⇒ 不动"),
 (("图",0,0), "图",                              "0 尺寸 ⇒ 不加"),
]
for (aid,w,h),want,label in cases:
    got=M._ensure_size(aid,w,h)
    ck(f"{label}: {aid!r} → {got!r}", got==want, f"期望 {want!r}")

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
