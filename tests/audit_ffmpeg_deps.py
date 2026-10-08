import os
"""验证依赖可分离：有系统 ffmpeg + pilk 就够（不需要 imageio-ffmpeg）。"""
import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0,'/tmp/botpy_src/botpy-master')
sys.path.insert(0,'/tmp/botpy_src/botpy-master')
P=F=0
def ck(n,c,e=""):
    global P,F
    if c: P+=1; print("  ok   "+n)
    else: F+=1; print(f"  FAIL {n}  {e}")

# 桩 pilk
fake=types.ModuleType("pilk")
def encode(pcm,silk,pcm_rate=None,tencent=False):
    open(silk,"wb").write(b"#!SILK_V3"+b"\x00"*64+b"\xff\xff"); return 1.0
fake.encode=encode; sys.modules["pilk"]=fake

import audio_silk as A
A.clear_cache()

print("═══ ffmpeg 查找顺序 ═══")
exe = A._ffmpeg_exe()
print(f"  找到: {exe}")
ck("★ 找到 ffmpeg", bool(exe))
ck("★ 优先用系统 PATH 上的（不是 imageio 的）",
   exe == __import__("shutil").which("ffmpeg"), f"{exe}")

print("\n═══ 只有 pilk + 系统 ffmpeg（模拟没装 imageio-ffmpeg）═══")
sys.modules["imageio_ffmpeg"] = None   # 让它 import 失败
A.clear_cache()
ck("★★ silk_available() 仍为 True（不把 imageio 当硬依赖）", A.silk_available())

os.makedirs("/tmp/silktest",exist_ok=True)
import subprocess
mp3="/tmp/silktest/dep.mp3"
subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi",
                "-i","sine=frequency=440:duration=1","-b:a","64k",mp3,"-y"],capture_output=True)
out = asyncio.run(A.to_silk_if_needed(mp3))
ck("★★ 没装 imageio-ffmpeg 也能转出 silk", bool(out) and os.path.exists(out), repr(out))

print("\n═══ 完全没 ffmpeg ⇒ 优雅返回 None（子进程隔离）═══")
import subprocess as _sp
_prog = """
import sys, types
sys.path.insert(0, '/var/minis/workspace/qqbot_bridge_review/bridge')
f = types.ModuleType('pilk'); f.encode = lambda *a, **k: 1.0
sys.modules['pilk'] = f
sys.modules['imageio_ffmpeg'] = None          # 让 import 直接失败
import audio_silk as A
A.shutil.which = lambda x: None               # 系统 PATH 也没有
A.reset_ffmpeg_cache()
print('AVAIL', A.silk_available())
"""
_r = _sp.run([sys.executable, "-c", _prog], capture_output=True, text=True, timeout=120)
ok_no_ff = "AVAIL False" in (_r.stdout or "")
ck("★★ 没 ffmpeg 时 silk_available()=False", ok_no_ff, (_r.stdout or _r.stderr or "")[-160:])

_prog2 = """
import asyncio, sys, types, os
sys.path.insert(0, '/var/minis/workspace/qqbot_bridge_review/bridge')
f = types.ModuleType('pilk'); f.encode = lambda *a, **k: 1.0
sys.modules['pilk'] = f
sys.modules['imageio_ffmpeg'] = None
import audio_silk as A
A.shutil.which = lambda x: None
A.reset_ffmpeg_cache()
r = asyncio.run(A.to_silk_if_needed('/tmp/silktest/dep.mp3'))
print('RESULT', r)
"""
_r2 = _sp.run([sys.executable, "-c", _prog2], capture_output=True, text=True, timeout=180)
ck("★ 返回 None 而非抛异常（调用方会退回按文件发）",
   "RESULT None" in (_r2.stdout or ""), (_r2.stdout or _r2.stderr or "")[-160:])

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
