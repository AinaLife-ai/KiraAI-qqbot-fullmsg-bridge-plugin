
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR
import os
"""验证依赖可分离：有系统 ffmpeg + pilk 就够（不需要 imageio-ffmpeg）。"""
import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0,_BOTPY_DIR())
sys.path.insert(0,_BOTPY_DIR())
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
_prog = f"""
import sys, types
sys.path.insert(0, {_BR()!r})
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

_prog2 = f"""
import asyncio, sys, types, os
sys.path.insert(0, {_BR()!r})
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

print("\n═══ ④ 命令形状加固：-nostdin / stdin=DEVNULL / 120s 超时 / Win 无窗口 ═══")
A.clear_cache()
# 把编码器固定到桩 pilk（开发机真装 pysilk 时不再误判 —— 项目测试约定）
A._ENCODER_CACHE, A._ENCODER_TRIED = "pilk", True
real_run = A.subprocess.run
calls = []
class _P:
    returncode = 0
    stderr = b""
def _fake_run(cmd, **kw):
    calls.append((list(cmd), dict(kw)))
    open(cmd[-1], "wb").write(b"\x00" * 200)      # 产出 PCM（模拟解码成功）
    return _P()
A.subprocess.run = _fake_run
os.makedirs("/tmp/silktest/ff_shape", exist_ok=True)
_res = A._convert_sync(mp3, "/tmp/silktest/ff_shape")
ck("★ 转换成功（桩 run）", bool(_res), repr(_res))
ck("★★ 命令行带 -nostdin（经典卡死来源之一）",
   bool(calls) and "-nostdin" in calls[0][0], str(calls[0][0] if calls else None))
ck("★★ stdin=DEVNULL（不让 ffmpeg 碰 stdin）",
   bool(calls) and calls[0][1].get("stdin") == _sp.DEVNULL,
   str(calls[0][1] if calls else None))
ck("★ 单次超时=120s（原来是 300s，弹窗卡住时能更快退守）",
   bool(calls) and calls[0][1].get("timeout") == A.FFMPEG_TIMEOUT == 120,
   str(calls[0][1].get("timeout") if calls else None))
if os.name == "nt":
    ck("★ Windows 带 CREATE_NO_WINDOW（不弹控制台）",
       bool(calls) and calls[0][1].get("creationflags") == 0x08000000)
else:
    print("  skip  CREATE_NO_WINDOW 是 Windows 专属（本机非 Windows）")
A.subprocess.run = real_run

print("\n═══ ⑤ 启动类错误（0xC0000142）⇒ 自动换下一个候选 ═══")
A.clear_cache()
A._ENCODER_CACHE, A._ENCODER_TRIED = "pilk", True
_orig_cands = A._ffmpeg_candidates
A._ffmpeg_candidates = lambda: ["/fake/bad_ffmpeg.exe", "/fake/good_ffmpeg.exe"]
tries = []
def _fake_run2(cmd, **kw):
    tries.append(cmd[0])
    if cmd[0].endswith("bad_ffmpeg.exe"):
        return type("P", (), {"returncode": -1073741502, "stderr": b""})()   # 0xC0000142
    open(cmd[-1], "wb").write(b"\x00" * 200)
    return type("P", (), {"returncode": 0, "stderr": b""})()
A.subprocess.run = _fake_run2
os.makedirs("/tmp/silktest/ff_fb", exist_ok=True)
_res2 = A._convert_sync(mp3, "/tmp/silktest/ff_fb")
ck("★★★ 坏二进制(0xC0000142)被跳过、好二进制完成转换",
   bool(_res2) and tries[:2] == ["/fake/bad_ffmpeg.exe", "/fake/good_ffmpeg.exe"],
   str(tries))
ck("★★ 记住可用的二进制（后续优先复用）",
   A._FFMPEG_CACHE == "/fake/good_ffmpeg.exe", str(A._FFMPEG_CACHE))
A.subprocess.run = real_run
A._ffmpeg_candidates = _orig_cands

print("\n═══ ⑥ 插件配置 ffmpeg_path 优先级最高 ═══")
import tempfile as _tf
_fd, _tmp_ff = _tf.mkstemp(prefix="fake_ffmpeg_")
os.close(_fd)
A.set_ffmpeg_path(_tmp_ff)
_c = A._ffmpeg_candidates()
ck("★ 配置存在 ⇒ 候选第一位", bool(_c) and _c[0] == _tmp_ff, str(_c[:3]))
A.set_ffmpeg_path("")
_c2 = A._ffmpeg_candidates()
ck("★ 清空配置 ⇒ 回到自动查找", not _c2 or _c2[0] != _tmp_ff, str(_c2[:2]))
try:
    os.remove(_tmp_ff)
except Exception:
    pass

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
