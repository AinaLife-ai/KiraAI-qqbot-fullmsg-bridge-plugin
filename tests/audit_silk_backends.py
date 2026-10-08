import os
"""验证多后端：pysilk / pilk / silk_v3_encoder 都能被选中并正确调用。"""
import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0,'/tmp/botpy_src/botpy-master')
P=F=0
def ck(n,c,e=""):
    global P,F
    if c: P+=1; print("  ok   "+n)
    else: F+=1; print(f"  FAIL {n}  {e}")

CALLS=[]
# ── 桩 pysilk（file-like API）──
ps=types.ModuleType("pysilk")
def ps_encode(pcm_fp, silk_fp, pcm_rate, bit_rate):
    CALLS.append(("pysilk", pcm_rate, bit_rate, pcm_fp.read(4)))
    silk_fp.write(b"#!SILK_V3"+b"\x00"*32+b"\xff\xff")
ps.encode=ps_encode

# ── 桩 pilk（路径 API）──
pk=types.ModuleType("pilk")
def pk_encode(pcm, silk, pcm_rate=None, tencent=False):
    CALLS.append(("pilk", pcm_rate, tencent))
    open(silk,"wb").write(b"#!SILK_V3"+b"\x00"*32+b"\xff\xff")
pk.encode=pk_encode

os.makedirs("/tmp/silktest",exist_ok=True)
import subprocess
mp3="/tmp/silktest/multi.mp3"
subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi",
                "-i","sine=frequency=440:duration=1","-b:a","64k",mp3,"-y"],capture_output=True)

def fresh(mods):
    """按给定的可用模块重新加载 audio_silk。"""
    for k in ("pysilk","pilk"): sys.modules.pop(k, None)
    for m in mods: sys.modules[m[0]] = m[1]
    sys.modules.pop("audio_silk", None)
    import importlib
    return importlib.import_module("audio_silk")

print("═══ ① 有 pysilk 时的选择 ═══")
CALLS.clear()
A = fresh([("pysilk", ps)])
ck("★ 选中 pysilk（首选）", A._silk_encoder()=="pysilk", A._silk_encoder())
out = asyncio.run(A.to_silk_if_needed(mp3))
ck("★ 转码成功", bool(out) and os.path.exists(out), repr(out))
ck("★ 调用参数正确（pcm_rate=24000）",
   bool(CALLS) and CALLS[0][0]=="pysilk" and CALLS[0][1]==24000, str(CALLS))
ck("★ 产物带 silk 魔数", bool(out) and open(out,'rb').read(9)==b"#!SILK_V3")

print("\n═══ ② 只有 pilk 时退回它 ═══")
CALLS.clear()
A2 = fresh([("pilk", pk)])
ck("★ 选中 pilk", A2._silk_encoder()=="pilk", A2._silk_encoder())
out2 = asyncio.run(A2.to_silk_if_needed(mp3))
ck("★ 转码成功", bool(out2) and os.path.exists(out2), repr(out2))
ck("★ 调用带 tencent=True",
   bool(CALLS) and CALLS[0][0]=="pilk" and CALLS[0][2] is True, str(CALLS))

print("\n═══ ③ 两个都没有 ⇒ 优雅降级 ═══")
A3 = fresh([])
ck("★ silk_available()=False", not A3.silk_available())
ck("★ 返回 None（调用方退回按文件发）",
   asyncio.run(A3.to_silk_if_needed(mp3)) is None)

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
