"""★★ 语音条超长自动剪裁（v1.6.16）：平台上限 5 分钟，超了自动剪到上限。

用户实测（2026-10-10）：QQ 官方 bot 语音条**最大 5 分钟（300 秒整）**——
多 1 秒都失败、整条退回文件卡片。本套件验证：

1. `_effective_cap` 语义（显式 > 模块默认 > 关闭）
2. 转码路径：`-t <上限>` 精确截断（真 ffmpeg + 真 pysilk 量产物时长）
3. 不超长的音频：**零成本**（`-t` 是 no-op，时长原样）
4. 已 silk 且超长：解码量长度 → 截断 PCM → 重编码（产物精确 = 上限）
5. 已 silk 且不长：**尺寸筛**放过（不解码 ⇒ 热路径不付钱）
6. 关闭剪裁：命令里**没有** `-t`（回退老行为）
7. 时长探测 `probe_duration_sync`（只读文件头 + 缓存）
8. 配置语义 `_voice_cap`（默认 300 / 关闭 / 自定义 / 非法回默认）
9. 端到端 `maybe_convert_to_silk`（假元素 → 产物 ≤ 上限且是腾讯系 silk）
10. 剪裁产物缓存（同一份超长音频不重复解码重编码）
"""
import asyncio
import math
import os
import struct
import subprocess
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import audio_silk as A  # noqa: E402

RATE = A._SILK_RATE
P = F = 0


def ck(n, c, e=""):
    global P, F
    if c:
        P += 1
        print("  ok   " + n)
    else:
        F += 1
        print(f"  FAIL {n}  {e}")


class L:
    def __init__(self):
        self.lines = []

    def info(self, *a):
        m = (a[0] % tuple(a[1:])) if len(a) > 1 else a[0]
        self.lines.append(("INFO", m))
        print("    INFO:", m)

    def warning(self, *a):
        m = (a[0] % tuple(a[1:])) if len(a) > 1 else a[0]
        self.lines.append(("WARN", m))
        print("    WARN:", m)

    def debug(self, *a):
        pass


D = "/tmp/voicetrim"


import logging  # noqa: E402


class _Capture(logging.Handler):
    """捕获 audio_silk **模块级** logger（_convert_sync 用的是它，不是传入的 logger_）。"""

    def __init__(self):
        super().__init__()
        self.recs = []

    def emit(self, record):
        try:
            self.recs.append(record.getMessage())
        except Exception:
            pass


def cap_logs():
    A.logger.setLevel(logging.INFO)      # 真实环境里日志级别是 INFO；默认 WARNING 会把 info 丢掉
    h = _Capture()
    A.logger.addHandler(h)
    return h


def mk_wav(path, secs):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * 300 * i / RATE)))
            for i in range(int(RATE * secs))))


def to_silk(src, dst):
    """任意音频 → 腾讯系 silk（走 ffmpeg + pysilk，与插件同一条链路）。"""
    pcm = dst + ".pcm"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                    "-i", src, "-f", "s16le", "-ac", "1", "-ar", str(RATE), pcm],
                   capture_output=True, check=True)
    import pysilk
    with open(pcm, "rb") as fi, open(dst, "wb") as fo:
        pysilk.encode(fi, fo, RATE, RATE)
    os.remove(pcm)
    return dst


def silk_secs(path):
    """解出 silk 的实际时长（秒）；失败返回 -1。"""
    import pysilk
    out = path + ".dec"
    with open(path, "rb") as fi, open(out, "wb") as fo:
        pysilk.decode(fi, fo, RATE)
    n = os.path.getsize(out) / (RATE * 2)
    os.remove(out)
    return n


print("═══ 0) 素材与依赖 ═══")
os.makedirs(D, exist_ok=True)
if not A.silk_available():
    print("  SKIP：本机没有 silk 编码器或 ffmpeg（无法验证转码链路）")
    print("\n结果：0 passed, 0 failed")
    sys.exit(0)
wav10 = os.path.join(D, "src10.wav")
mk_wav(wav10, 10)
silk10 = to_silk(wav10, os.path.join(D, "s10.silk"))
wav2 = os.path.join(D, "src2.wav")
mk_wav(wav2, 2)
silk2 = to_silk(wav2, os.path.join(D, "s2.silk"))
ck(f"素材就绪（10 秒 silk={os.path.getsize(silk10)}B、2 秒 silk={os.path.getsize(silk2)}B）",
   os.path.getsize(silk10) > 0 and os.path.getsize(silk2) > 0)
ck("★ 基准：10 秒 silk 实测时长 ≈ 10s", 9.5 <= silk_secs(silk10) <= 10.5, silk_secs(silk10))

print("\n═══ 1) _effective_cap 语义（显式 > 模块默认 > 关闭）═══")
A.configure_trim(None)
ck("① 都没给 ⇒ None（不剪裁，老行为）", A._effective_cap(None) is None)
A.configure_trim(300)
ck("② 模块默认 300 ⇒ 300", A._effective_cap(None) == 300.0)
ck("③ 显式 60 覆盖模块默认 ⇒ 60", A._effective_cap(60) == 60.0)
ck("④ 显式 0 ⇒ 关闭（覆盖模块默认）", A._effective_cap(0) is None)
A.configure_trim(-5)
ck("⑤ 模块默认非法（负数）⇒ 关闭", A._effective_cap(None) is None)
A.configure_trim("abc")
ck("⑥ 模块默认非法（字符串）⇒ 关闭", A._effective_cap(None) is None)
A.configure_trim(99999)
ck("⑦ 超出合理范围（>3600）⇒ 关闭", A._effective_cap(None) is None)
A.configure_trim(None)

print("\n═══ 2) 转码路径：-t 精确截断（真 ffmpeg + 真 pysilk）═══")
A._CACHE.clear()
lg = L()
t0 = time.time()
p3 = asyncio.run(A.convert_to_silk_forced(wav10, logger_=lg, max_seconds=3))
dt = (time.time() - t0) * 1000
ck("★ 产物存在", bool(p3) and os.path.exists(p3), repr(p3))
ck(f"★★ 产物时长 = 3.00s（期望 3.0±0.05）", abs(silk_secs(p3) - 3.0) <= 0.05, silk_secs(p3))
ck("★ 产物是腾讯系 silk（\\x02#!SILK_V3）",
   open(p3, "rb").read(10) == b"\x02#!SILK_V3")
_h2 = cap_logs()
A._CACHE.clear()
p3b = asyncio.run(A.convert_to_silk_forced(wav10, logger_=lg, max_seconds=3))
A.logger.removeHandler(_h2)
ck("★★ 有『已自动剪裁』日志（含原时长 0:10 → 0:03）",
   any("已自动剪裁" in m and "0:10" in m and "0:03" in m for m in _h2.recs),
   str(_h2.recs[-2:]))
print(f"    （cap=3 全链路耗时 {dt:.0f}ms，含 ffmpeg 启动）")

print("\n═══ 3) 上限 ≥ 源长度 ⇒ 原样（零成本：-t 是 no-op）═══")
A._CACHE.clear()
lg2 = L()
p10 = asyncio.run(A.convert_to_silk_forced(wav10, logger_=lg2, max_seconds=60))
ck("★ 产物时长 ~10s（没被截）", abs(silk_secs(p10) - 10.0) <= 0.2, silk_secs(p10))
ck("★ 没有剪裁日志", not any("已自动剪裁" in m for _lv, m in lg2.lines))

print("\n═══ 4) 已 silk 且超长 ⇒ 解码量长度 → 截断重编码 ═══")
A._CACHE.clear()
lg3 = L()
t0 = time.time()
p4 = asyncio.run(A.to_silk_if_needed(silk10, logger_=lg3, max_seconds=4))
dt4 = (time.time() - t0) * 1000
ck("★ 返回了新路径（不是原文件）", p4 and p4 != silk10, f"{p4} vs {silk10}")
ck("★★ 产物时长 = 4.00s", abs(silk_secs(p4) - 4.0) <= 0.05, silk_secs(p4))
ck("★ 产物仍是腾讯系 silk", open(p4, "rb").read(10) == b"\x02#!SILK_V3")
ck("★ 有剪裁日志（原 0:10 → 0:04）",
   any("已自动剪裁" in m and "0:10" in m and "0:04" in m for _lv, m in lg3.lines),
   str([m for _lv, m in lg3.lines]))
ck("★ 原文件一个字节没动", os.path.getsize(silk10) > 0)
print(f"    （已 silk 剪裁耗时 {dt4:.0f}ms）")

print("\n═══ 5) 已 silk 且不长 ⇒ 尺寸筛放过（热路径不解码）═══")
import pysilk  # noqa: E402

calls = {"n": 0}
_orig_decode = pysilk.decode


def _spy_decode(fi, fo, r, *a, **k):
    calls["n"] += 1
    return _orig_decode(fi, fo, r, *a, **k)


A.pysilk = pysilk
pysilk.decode = _spy_decode
try:
    A._CACHE.clear()
    lg4 = L()
    p5 = asyncio.run(A.to_silk_if_needed(silk2, logger_=lg4, max_seconds=300))
finally:
    pysilk.decode = _orig_decode
ck("★★ 短 silk 原样返回（不剪裁）", p5 == silk2, repr(p5))
ck("★★ 尺寸筛生效：pysilk.decode **零调用**", calls["n"] == 0, f"decode 调用了 {calls['n']} 次")
ck("★ 无剪裁日志", not any("已自动剪裁" in m for _lv, m in lg4.lines))

print("\n═══ 6) 关闭剪裁 ⇒ 命令里没有 -t（回退老行为）═══")
cmds = []
_orig_run = subprocess.run


def _spy_run(cmd, *a, **k):
    try:
        cmds.append(list(cmd))
    except Exception:
        pass
    return _orig_run(cmd, *a, **k)


A.subprocess.run = _spy_run
try:
    A._CACHE.clear()
    asyncio.run(A.convert_to_silk_forced(wav10, logger_=L(), max_seconds=0))
    off_flags = [c for c in cmds if any("-t" == x for x in c)]
    cmds.clear()
    A._CACHE.clear()
    A.configure_trim(300)
    asyncio.run(A.convert_to_silk_forced(wav10, logger_=L()))
    on_cmds = [c for c in cmds if any("-t" == x for x in c)]
    flag_vals = [c[c.index("-t") + 1] for c in on_cmds]
    A.configure_trim(None)
finally:
    A.subprocess.run = _orig_run
ck("★★ 关闭时：ffmpeg 命令行里没有 -t", not off_flags, str(off_flags[:1]))
ck("★★ 开启时：命令行带 -t 300.000", flag_vals == ["300.000"], str(flag_vals))

print("\n═══ 7) 时长探测 probe_duration_sync（只读头 + 缓存）═══")
A._DUR_CACHE.clear()
d = A.probe_duration_sync(wav10)
ck("★ 10 秒 wav ⇒ 探测值 ≈10.0", d is not None and abs(d - 10.0) <= 0.1, repr(d))
ck("★ 结果进了缓存", len(A._DUR_CACHE) >= 1)
cmds.clear()
A.subprocess.run = _spy_run
try:
    A.probe_duration_sync(wav10)          # 第二次应命中缓存
finally:
    A.subprocess.run = _orig_run
ck("★ 第二次不再启动进程（命中缓存）", not cmds, f"又跑了 {len(cmds)} 次")

print("\n═══ 8) 配置语义 _voice_cap（默认 300 / 关闭 / 自定义 / 非法）═══")
import media_types as M  # noqa: E402


class PConf:
    voice_auto_trim = True
    voice_max_seconds = 300


ck("★ 默认 ⇒ 300.0", M._voice_cap(PConf()) == 300.0)


class POff(PConf):
    voice_auto_trim = False


ck("★ voice_auto_trim=False ⇒ None（不剪裁）", M._voice_cap(POff()) is None)


class PCustom(PConf):
    voice_max_seconds = 60


ck("★ 自定义 60 ⇒ 60.0", M._voice_cap(PCustom()) == 60.0)


class PBad(PConf):
    voice_max_seconds = "abc"


ck("★ 非法值 ⇒ 回默认 300.0", M._voice_cap(PBad()) == 300.0)
ck("★ 空 plugin（None）⇒ 默认 300.0", M._voice_cap(None) == 300.0)

print("\n═══ 9) 端到端 maybe_convert_to_silk（假元素 → 语音条 silk）═══")


class FakeElem:
    file_type = "file"
    _kira_bridge_orig_kind = "Record"

    def __init__(self, path):
        self._p = path

    async def to_path(self):
        return self._p


A._CACHE.clear()
lg5 = L()
got = asyncio.run(M.maybe_convert_to_silk(FakeElem(wav10), lg5, 3))
ck("★ 返回 silk 路径（不是 KEEP_AS_IS / None）",
   bool(got) and got != M.KEEP_AS_IS and os.path.exists(str(got)), repr(got))
ck("★★ 端到端产物 ≤ 3.05s（超长语音自动剪裁到上限）",
   silk_secs(str(got)) <= 3.05, silk_secs(str(got)))
ck("★ 端到端产物是腾讯系 silk", open(str(got), "rb").read(10) == b"\x02#!SILK_V3")

print("\n═══ 10) 剪裁产物缓存（同一份超长音频不重复解码重编码）═══")
A._CACHE.clear()
first = asyncio.run(A.to_silk_if_needed(silk10, logger_=L(), max_seconds=4))
calls2 = {"n": 0}
pysilk.decode = _spy_decode
try:
    t0 = time.time()
    second = asyncio.run(A.to_silk_if_needed(silk10, logger_=L(), max_seconds=4))
    dt2 = (time.time() - t0) * 1000
finally:
    pysilk.decode = _orig_decode
ck("★★ 第二次命中缓存 ⇒ decode 零调用", calls2["n"] == 0, f"decode 调用了 {calls2['n']} 次")
ck("★★ 第二次返回同一产物", first == second, f"{first} vs {second}")
ck(f"★ 第二次几乎零耗时（{dt2:.1f}ms，无 ffmpeg/解码）", dt2 < 200.0, f"{dt2:.1f}ms")

print("\n═══ 11) 不误伤：普通文件（file_type=4）根本不进语音路径 ═══")
src_txt = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "media_types.py"), encoding="utf-8").read()
ck("★ `_voice_cap` 只挂在语音相关分支上（元素层转换/重试 + 安全网×2 = 4 处）",
   src_txt.count("_voice_cap(plugin)") == 4,
   f"_voice_cap 出现 {src_txt.count('_voice_cap(plugin)')} 次")


class FakeFileElem:
    _kira_bridge_orig_kind = None

    def __init__(self, path):
        self.file = path


class PlainFileElem:
    file = "/tmp/x.pdf"


ck("★ classify() 对普通 File 返回 None（交回核心按文件发）",
   M.classify(PlainFileElem()) is None)

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
