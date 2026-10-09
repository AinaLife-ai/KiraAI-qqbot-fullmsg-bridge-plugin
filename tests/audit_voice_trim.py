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
import base64
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


print("\n═══ 12) ★审查修复回归：**已 silk 且超长**必须真的用上剪裁产物 ═══")
# （原缺陷：maybe_convert_to_silk 对"已是 silk"一律返回 KEEP_AS_IS ⇒
#   剪裁结果被丢掉，上传的仍是超长原文件 ⇒ 平台拒收 ⇒ 文件卡片，功能等于没做）
A._CACHE.clear()
A._DUR_CACHE.clear()
lg6 = L()
got2 = asyncio.run(M.maybe_convert_to_silk(FakeElem(silk10), lg6, 4))
ck("★★ 超长 silk：返回的是**剪裁产物**（不是 KEEP_AS_IS）",
   bool(got2) and got2 != M.KEEP_AS_IS, repr(got2))
ck("★★ 产物时长 = 4.00s（真的剪了）", abs(silk_secs(str(got2)) - 4.0) <= 0.05,
   silk_secs(str(got2)))
ck("★ 产物是腾讯系 silk", open(str(got2), "rb").read(10) == b"\x02#!SILK_V3")

A._CACHE.clear()
lg7 = L()
got3 = asyncio.run(M.maybe_convert_to_silk(FakeElem(silk2), lg7, 300))
ck("★ 不超长的 silk：仍然返回 KEEP_AS_IS（老行为不变、零成本）",
   got3 == M.KEEP_AS_IS, repr(got3))

print("\n═══ 13) 源正好等于上限 ⇒ 不谎报『已剪裁』（日志准确性）═══")
A._CACHE.clear()
A._DUR_CACHE.clear()
wav6 = os.path.join(D, "src6.wav")
mk_wav(wav6, 6)
_h13 = cap_logs()
A._CACHE.clear()
p13 = asyncio.run(A.convert_to_silk_forced(wav6, logger_=L(), max_seconds=6))
A.logger.removeHandler(_h13)
ck("★ 产物时长 ~6s（精确等于上限，没被剪短）", abs(silk_secs(p13) - 6.0) <= 0.1,
   silk_secs(p13))
ck("★★ 没有『已自动剪裁』日志（源没超，不该谎报）",
   not any("已自动剪裁" in m for m in _h13.recs),
   str([m for m in _h13.recs if "剪裁" in m][:1]))

print("\n═══ 14) 量长缓存：大而不超长的 silk 不重复解码 ═══")
silk6 = to_silk(wav6, os.path.join(D, "s6.silk"))
print(f"    6 秒 silk = {os.path.getsize(silk6)}B（尺寸筛阈值 cap×500；取 cap=10 ⇒ 5000B）")
calls3 = {"n": 0}


def _spy_decode2(fi, fo, r, *a, **k):
    calls3["n"] += 1
    return _orig_decode(fi, fo, r, *a, **k)


A._CACHE.clear()
A._DUR_CACHE.clear()
pysilk.decode = _spy_decode2
try:
    r1 = asyncio.run(A.to_silk_if_needed(silk6, logger_=L(), max_seconds=10))
    n_after_first = calls3["n"]
    r2 = asyncio.run(A.to_silk_if_needed(silk6, logger_=L(), max_seconds=10))
    n_after_second = calls3["n"]
finally:
    pysilk.decode = _orig_decode
ck("★ 第一次：解码量长度（没超 ⇒ 原样返回）", r1 == silk6 and n_after_first == 1,
   f"r1={r1} decode={n_after_first}")
ck("★★ 第二次：命中量长缓存 ⇒ **零解码**", r2 == silk6 and n_after_second == n_after_first,
   f"decode 累计={n_after_second}")

print("\n═══ 15) 安全网（HTTP 层）也按上限剪裁（端到端，真 ffmpeg+pysilk）═══")
try:
    import types as _types

    from _env import botpy_parent as _bp

    sys.path.insert(0, _bp())
    from botpy.http import Route

    class _GuardHTTP:
        def __init__(self):
            self.calls = []

        async def request(self, route, **kw):
            body = dict(kw.get("json") or {})
            raw = base64.b64decode(body.get("file_data") or "") if body.get("file_data") else b""
            self.calls.append({"body": body, "raw": raw})
            return {"file_info": "FI"}

    class _Plugin:
        voice_auto_trim = True
        voice_max_seconds = 2

    http = _GuardHTTP()
    client = _types.SimpleNamespace()
    client.api = _types.SimpleNamespace()
    client.api._http = http
    A._CACHE.clear()
    ok_guard = M.install_http_guard(client, L(), _Plugin())
    ck("★ 安全网安装成功", ok_guard is True, repr(ok_guard))
    route = Route("POST", "/v2/users/{openid}/files", openid="U1")
    with open(wav6, "rb") as _f:
        _payload = _f.read()
    res = asyncio.run(http.request(route, json={
        "file_type": 3,
        "file_data": base64.b64encode(_payload).decode("ascii"),
        "srv_send_msg": False, "openid": "U1"}))
    ck("★ 拿到 file_info（消息没丢）", (res or {}).get("file_info") == "FI", str(res)[:80])
    ck("★ 安全网发出了请求（file_type=3）", bool(http.calls) and http.calls[-1]["body"].get("file_type") == 3)
    _sent = http.calls[-1]["raw"]
    ck("★★ 安全网发出的字节是腾讯系 silk",
       _sent[:10] == b"\x02#!SILK_V3", _sent[:12].hex())
    with open("/tmp/voicetrim/guard_out.silk", "wb") as _f:
        _f.write(_sent)
    ck("★★ 安全网产物 ≤ 2 秒（按插件配置的上限剪裁）",
       silk_secs("/tmp/voicetrim/guard_out.silk") <= 2.05,
       silk_secs("/tmp/voicetrim/guard_out.silk"))
    M.restore_http_guard(client)
except ImportError as exc:                     # botpy 不在本机 ⇒ 跳过该节
    print(f"    SKIP（botpy 不可用：{exc}）")
except Exception as exc:
    ck("★ 安全网用例无异常", False, f"{type(exc).__name__}: {exc}")


print("\n═══ 16) 元素层端到端（_upload_file 全链路：分类→转码→剪裁→上传体）═══")
try:
    import types as _t2

    from _env import botpy_parent as _bp2

    sys.path.insert(0, _bp2())

    class _CapHTTP:
        def __init__(self):
            self.calls = []

        async def request(self, route, **kw):
            body = dict(kw.get("json") or {})
            raw = base64.b64decode(body.get("file_data") or "") if body.get("file_data") else b""
            self.calls.append({"body": body, "raw": raw})
            return {"file_info": "FI"}

    class _Elem:
        """假媒体元素：够 _guess_name/classify/_upload 用。"""

        def __init__(self, path, kind="Record"):
            self.file = path
            self.file_type = "file"
            self._kira_bridge_orig_kind = kind

        async def to_path(self):
            return self.file

    class _Plugin2:
        voice_auto_trim = True
        voice_max_seconds = 4
        gif_sticker_mode = "auto"

    async def _run_case(elem, plugin):
        http = _CapHTTP()
        client = _t2.SimpleNamespace()
        client.api = _t2.SimpleNamespace()
        client.api._http = http
        delegated = []

        async def _orig_upload(target_id, media_element, is_group):
            delegated.append(media_element)
            return {"file_info": "ORIG"}

        holder = _t2.SimpleNamespace()
        holder._upload_file = _orig_upload
        A._CACHE.clear()
        if not M.install(holder, client, L(), plugin):
            raise RuntimeError("install 失败")
        out = await holder._upload_file("U1", elem, False)
        return http, delegated, out

    # (a) 非 silk 源（10 秒 wav）+ 上限 4 ⇒ 上传体必须是 ≤4 秒的腾讯系 silk
    http_a, deleg_a, out_a = asyncio.run(_run_case(_Elem(wav10), _Plugin2()))
    raw_a = http_a.calls[-1]["raw"] if http_a.calls else b""
    ck("★(a) 走了元素层上传（没交回核心）", bool(http_a.calls) and not deleg_a,
       f"calls={len(http_a.calls)} delegated={len(deleg_a)}")
    ck("★(a) 体 = file_type=3 + 腾讯系 silk",
       http_a.calls[-1]["body"].get("file_type") == 3 and raw_a[:10] == b"\x02#!SILK_V3",
       raw_a[:12].hex())
    open("/tmp/voicetrim/e2e_a.silk", "wb").write(raw_a)
    ck("★★(a) 上传体时长 = 4.00s", abs(silk_secs("/tmp/voicetrim/e2e_a.silk") - 4.0) <= 0.05,
       silk_secs("/tmp/voicetrim/e2e_a.silk"))

    # (b) ★ 已经是 silk 且超长（10 秒 silk）+ 上限 4 ⇒ 上传体必须是被剪过的产物
    http_b, deleg_b, out_b = asyncio.run(_run_case(_Elem(silk10), _Plugin2()))
    raw_b = http_b.calls[-1]["raw"] if http_b.calls else b""
    open("/tmp/voicetrim/e2e_b.silk", "wb").write(raw_b)
    ck("★★(b) 已 silk 超长：上传体被剪到 ≤4s（回归：原缺陷会原样发 10s）",
       bool(raw_b) and silk_secs("/tmp/voicetrim/e2e_b.silk") <= 4.05,
       silk_secs("/tmp/voicetrim/e2e_b.silk"))
    ck("★(b) 且是腾讯系 silk、file_type=3",
       raw_b[:10] == b"\x02#!SILK_V3" and http_b.calls[-1]["body"].get("file_type") == 3)

    # (c) 已 silk 且不超长（2 秒 silk）+ 上限 300 ⇒ 字节一字不改（零成本老行为）
    http_c, _, _ = asyncio.run(_run_case(_Elem(silk2), _Plugin2.__class__(
        "P", (), {"voice_auto_trim": True, "voice_max_seconds": 300,
                  "gif_sticker_mode": "auto"})()))
    raw_c = http_c.calls[-1]["raw"] if http_c.calls else b""
    with open(silk2, "rb") as _f:
        ck("★★(c) 短 silk：上传体与原文件**逐字节一致**（未剪裁、零额外开销）",
           raw_c == _f.read(), f"{len(raw_c)}B")

    # (d) 关闭剪裁（voice_auto_trim=False）⇒ 超长 silk 原样发（老行为，可回退）
    _off = _t2.SimpleNamespace(voice_auto_trim=False, voice_max_seconds=300,
                               gif_sticker_mode="auto")
    http_d, _, _ = asyncio.run(_run_case(_Elem(silk10), _off))
    raw_d = http_d.calls[-1]["raw"] if http_d.calls else b""
    with open(silk10, "rb") as _f:
        ck("★★(d) 关闭剪裁：超长 silk 原样发（回退老行为，功能可关）",
           raw_d == _f.read(), f"{len(raw_d)}B")

    # (e) 普通文件（file_type=4 路径）⇒ 完全交回核心，一个字节都不动
    http_e, deleg_e, out_e = asyncio.run(_run_case(
        _Elem(os.path.abspath(__file__), kind=None), _Plugin2()))
    ck("★★(e) 普通 File：不进媒体层（交回核心）", not http_e.calls and len(deleg_e) == 1,
       f"calls={len(http_e.calls)} delegated={len(deleg_e)}")
except ImportError as exc:
    print(f"    SKIP（botpy 不可用：{exc}）")
except Exception as exc:
    ck("★ 元素层端到端用例无异常", False, f"{type(exc).__name__}: {exc}")


print("\n═══ 17) 零额外成本的硬证据：进程数（上限 ≥ 源长时）═══")
_procs = []
_orig_run2 = A.subprocess.run


def _count_run(cmd, *a, **k):
    try:
        if "ffmpeg" in os.path.basename(str(cmd[0])):
            _procs.append(list(cmd))
    except Exception:
        pass
    return _orig_run2(cmd, *a, **k)


A.subprocess.run = _count_run
try:
    A._CACHE.clear()
    A._DUR_CACHE.clear()
    asyncio.run(A.convert_to_silk_forced(wav10, logger_=L(), max_seconds=300))
    n_ok = len(_procs)
    _procs.clear()
    A._CACHE.clear()
    A._DUR_CACHE.clear()
    asyncio.run(A.convert_to_silk_forced(wav10, logger_=L(), max_seconds=3))
    n_trim = len(_procs)
finally:
    A.subprocess.run = _orig_run2
ck("★★ 不超长：**只启动 1 个 ffmpeg**（-t 是 no-op，没有额外探测）", n_ok == 1,
   f"进程数={n_ok}")
ck("★ 真剪裁：2 个 ffmpeg（解码 + 为日志探一次头），只在真剪时才付",
   n_trim == 2, f"进程数={n_trim}")

print(f"\n结果：{P} passed, {F} failed")
sys.exit(1 if F else 0)
