import os
"""验证 audio_silk 的代码路径（用桩 pilk 记录调用参数）。"""
import os, sys, types, asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0,'_BOTPY()')

CALLS=[]
fake=types.ModuleType("pilk")
def encode(pcm_path, silk_path, pcm_rate=None, tencent=False):
    CALLS.append({"pcm":pcm_path,"silk":silk_path,"rate":pcm_rate,"tencent":tencent,
                  "pcm_exists":os.path.exists(pcm_path),
                  "pcm_size":os.path.getsize(pcm_path) if os.path.exists(pcm_path) else 0})
    with open(silk_path,"wb") as f:
        f.write(b"#!SILK_V3" + b"\x00"*200 + b"\xff\xff")
    return 1.0
fake.encode=encode
sys.modules["pilk"]=fake

import audio_silk as A
class L:
    def info(self,*a): print("    INFO:", (a[0]%tuple(a[1:])) if len(a)>1 else a[0])
    def warning(self,*a): print("    WARN:", (a[0]%tuple(a[1:])) if len(a)>1 else a[0])

P=F=0
def ck(n,c,e=""):
    global P,F
    if c: P+=1; print("  ok   "+n)
    else: F+=1; print(f"  FAIL {n}  {e}")

os.makedirs("/tmp/silktest",exist_ok=True)
import subprocess
mp3="/tmp/silktest/src.mp3"
subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi",
                "-i","sine=frequency=440:duration=1","-b:a","64k",mp3,"-y"],capture_output=True)
print(f"  测试音频: {mp3} ({os.path.getsize(mp3)} 字节)")

print("\n[1] mp3 → silk（ffmpeg 解码 + pilk 编码）")
out = asyncio.run(A.to_silk_if_needed(mp3, logger_=L()))
print(f"      → {out}")
ck("★ 成功产出 silk 文件", bool(out) and os.path.exists(out), repr(out))
ck("★ 产物带 silk 魔数", bool(out) and open(out,'rb').read(10)==b"\x02#!SILK_V3")
ck("★ pilk.encode 参数正确（rate=24000, tencent=True）",
   len(CALLS)==1 and CALLS[0]["rate"]==24000 and CALLS[0]["tencent"] is True, str(CALLS))
ck("★ 传入的 PCM 是 ffmpeg 真解出来的（非空）",
   bool(CALLS) and CALLS[0]["pcm_exists"] and CALLS[0]["pcm_size"]>0, str(CALLS))

print("\n[2] 已是 silk ⇒ 原样返回、不再转码")
CALLS.clear()
out2 = asyncio.run(A.to_silk_if_needed(out, logger_=L()))
ck("★ 已 silk 不再转码", out2==out and len(CALLS)==0, f"{out2} calls={len(CALLS)}")

print("\n[3] 缓存：同文件第二次不再转码")
CALLS.clear()
out3 = asyncio.run(A.to_silk_if_needed(mp3, logger_=L()))
ck("★ 命中缓存", out3==out and len(CALLS)==0, f"calls={len(CALLS)}")

print("\n[4] 源文件不存在 ⇒ None（调用方按原样发）")
ck("★ 返回 None 而非抛异常", asyncio.run(A.to_silk_if_needed("/tmp/nope.mp3")) is None)

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
