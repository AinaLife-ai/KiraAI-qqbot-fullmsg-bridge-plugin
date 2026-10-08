
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR
import os
"""验证 silk 转码的临时目录不会泄漏。"""
import asyncio, glob, os, sys, types, tempfile
sys.path.insert(0,_BR())
sys.path.insert(0,'_BOTPY()')

fake=types.ModuleType("pilk")
def encode(pcm,silk,pcm_rate=None,tencent=False):
    open(silk,"wb").write(b"#!SILK_V3"+b"\x00"*64+b"\xff\xff"); return 1.0
fake.encode=encode; sys.modules["pilk"]=fake
import audio_silk as A

P=F=0
def ck(n,c,e=""):
    global P,F
    if c: P+=1; print("  ok   "+n)
    else: F+=1; print(f"  FAIL {n}  {e}")

def dirs(): return set(glob.glob(os.path.join(tempfile.gettempdir(), "qqbot_silk_*")))

os.makedirs("/tmp/silktest",exist_ok=True)
import subprocess
mp3="/tmp/silktest/leak.mp3"
subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi",
                "-i","sine=frequency=440:duration=1","-b:a","64k",mp3,"-y"],capture_output=True)

print("═══ 临时目录泄漏测试 ═══")
A.clear_cache()
before = dirs()
print(f"  起始临时目录数: {len(before)}")

# 1) 正常转码
out = asyncio.run(A.to_silk_if_needed(mp3))
ck("转码成功", bool(out) and os.path.exists(out))
mid = dirs()
ck("★ 转码后确实有个临时目录（产物要留着给上传用）", len(mid) > len(before), f"{len(mid)}")

# 2) clear_cache 必须删掉它
A.clear_cache()
after = dirs()
ck("★★ clear_cache 后临时目录被删干净", after == before, f"残留 {after - before}")

# 3) 失败路径（源文件不存在）不留目录
n0 = len(dirs())
asyncio.run(A.to_silk_if_needed("/tmp/definitely_not_here.mp3"))
ck("★ 失败路径不留临时目录", len(dirs()) == n0, f"{len(dirs())} vs {n0}")

# 4) 缓存淘汰时也删目录（塞满缓存）
A.clear_cache()
A._CACHE_MAX = 4
made=[]
for i in range(6):
    f=f"/tmp/silktest/m{i}.mp3"
    subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-f","lavfi",
                    "-i",f"sine=frequency={300+i*50}:duration=1","-b:a","64k",f,"-y"],capture_output=True)
    r=asyncio.run(A.to_silk_if_needed(f))
    if r: made.append(r)
alive = [d for d in dirs() if any(os.path.exists(os.path.join(d, os.path.basename(m))) for m in made)]
ck("★★ 缓存淘汰后目录数受控（不无限堆积）", len(alive) <= 4, f"{len(alive)} 个")

A.clear_cache()
print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
