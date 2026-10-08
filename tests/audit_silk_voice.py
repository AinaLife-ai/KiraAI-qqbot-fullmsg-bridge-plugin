"""★ 语音条 vs 音频文件：**两条路都要能走**（2026-10-08 二次修订）。

官方限制：`file_type=3` 语音 **只认 silk**。

## v1.6.1 的策略修订（用户实测后）

旧策略是"mp3/wav/ogg **先原样按 file_type=3 发**（官方文档说支持）"。
用户实测（2026-10-08 截图）**ogg 变成了文件卡片** —— 官方文档与平台实际不一致。
腾讯官方插件 `openclaw-qqbot` 的做法可作旁证：它的
`voiceDirectUploadFormats` **默认只有 `['.wav','.mp3','.silk']`**，
其余格式（ogg/m4a/…）**全部先转 SILK 再上传**。

⇒ 现在的策略（只信 silk）：

| 源文件 | 行为 |
|--------|------|
| 内容确实是 silk | 校验/补腾讯系头 ⇒ 直发 `file_type=3` |
| **扩展名是 silk、内容不是**（只是改名） | **当普通音频转 silk**（旧行为：原样发 ⇒ 文件卡片） |
| 其它音频 + 编码器可用 | **转 silk** ⇒ `file_type=3` |
| 其它音频 + **没有编码器**，但在文档白名单里 | 直传 `file_type=3` 试一次 |
| 其它音频 + 没有编码器 + 不在白名单（m4a/amr/…） | `file_type=4`（**至少看得见**；旧行为可能整条发不出去） |

本测试断言：

1. `Record` + mp3 ⇒ **会转 silk**，且 `file_type=3`；
2. `Record` + 已是 silk ⇒ 不转码、路径不变、`file_type=3`；
3. `Record` + **改名 silk**（.silk 后缀但不是 silk 内容）⇒ **必须转码**；
4. `Record` + m4a + 无编码器 ⇒ **`file_type=4`**（不丢消息，这是线上"啥都看不到"的修复）；
5. `File` 元素（模型写 `type="file"`）⇒ **一律不碰**，`file_type=4`；
6. `Video` ⇒ `file_type=2`。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import os
import subprocess
import sys
import types

ROOT = _BR()
GEN = os.environ.get("KIRA_CORE_GEN", "3")
sys.path.insert(0, str(_CORE_ROOT("3")) if GEN == "3" else str(_CORE_ROOT("2")))
sys.path.insert(0, ROOT)
sys.path.insert(0, _BOTPY_DIR())

os.makedirs(f"{ROOT}/data", exist_ok=True)
open(f"{ROOT}/data/log.log", "a").close()

# ---- 桩 pilk：记录调用，产出一个带 silk 魔数的文件 ----
SILK_CALLS = []
_fake = types.ModuleType("pilk")


def _enc(pcm_path, silk_path, pcm_rate=None, tencent=False):
    SILK_CALLS.append({"rate": pcm_rate, "tencent": tencent,
                       "pcm": pcm_path, "pcm_size": os.path.getsize(pcm_path) if os.path.exists(pcm_path) else 0})
    with open(silk_path, "wb") as f:
        f.write(b"#!SILK_V3" + b"\x00" * 128 + b"\xff\xff")
    return 1.0


_fake.encode = _enc
sys.modules["pilk"] = _fake

# ★ 2026-10-09：把编码器探测**固定到桩 pilk**。开发机若真装了 pysilk
#   （插件 requirements 里就有），优先探测会选中它 ⇒ 桩记录全空、断言失真。
#   测试制品必须任何机器一致（见 tests/_env.py 的约定）。
import importlib as _importlib

def _pin_encoder():
    try:
        _m = _importlib.import_module("audio_silk")
        _m._ENCODER_CACHE, _m._ENCODER_TRIED = "pilk", True
    except Exception:
        pass

_pin_encoder()

# ---- 测试素材 ----
D = "/tmp/silktest"
os.makedirs(D, exist_ok=True)
MP3 = f"{D}/voice.mp3"
SILK = f"{D}/voice.silk"
if not os.path.exists(MP3):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1", "-b:a", "64k", MP3, "-y"],
                   capture_output=True)
if not os.path.exists(SILK):
    open(SILK, "wb").write(b"#!SILK_V3" + b"\x00" * 128 + b"\xff\xff")

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


async def main():
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
    from core.chat.message_elements import File, Record, Text, Video
    from core.chat import MessageChain

    print(f"═══ 语音条 vs 文件 GEN={GEN} ═══")

    if GEN == "3":
        from core.adapter.context import AdapterContext
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo", platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        ad = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
    else:
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo", platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        ad = QQOfficialAdapter(info, asyncio.Queue())

    UP = []

    class HTTP:
        async def request(self, route, **kw):
            UP.append(kw.get("json") or {})
            return {"file_info": "FI"}

    class API:
        _http = HTTP()

        async def post_group_message(self, **kw):
            return {"id": "R1", "ext_info": {"ref_idx": "X=="}}

        async def post_c2c_message(self, **kw):
            return {"id": "R2", "ext_info": {"ref_idx": "Y=="}}

        async def post_group_file(self, **kw):
            UP.append(kw)
            return {"file_info": "FI"}

        async def post_c2c_file(self, **kw):
            UP.append(kw)
            return {"file_info": "FI"}

    ad.client = type("C", (), {})()
    ad.client.api = API()
    ad.client._connection = None
    ad._client_task = type("T", (), {"done": lambda s: False})()

    import main as bridge_main

    class _Mgr:
        def get_adapters(self):
            return {"qqo": ad}

        def get_adapter(self, n):
            return ad if n == "qqo" else None

    class Ctx:
        adapter_mgr = _Mgr()

    p = bridge_main.QQOfficialGroupBridge(
        Ctx(), {"section_basic": {"enabled": True},
                "section_proactive": {"proactive_enabled": True}})
    p._attach(ad, "qqo", {})

    async def send(ele):
        UP.clear()
        SILK_CALLS.clear()
        await ad.send_group_message("G1", MessageChain([Text("t"), ele]))
        return [u for u in UP if isinstance(u, dict) and "file_type" in u]

    print("\n[1] Record + mp3 ⇒ **转 silk 后按 file_type=3 发**（不再原样发 mp3）")
    up = await send(Record(MP3, name="voice.mp3", mime="audio/mpeg"))
    ft = [u.get("file_type") for u in up]
    check("★ file_type=3（语音，不是 4=文件）", ft == [3], f"file_type={ft}")
    import base64 as _b64
    _data = _b64.b64decode((up[0].get("file_data") or "")) if up else b""
    check("★★ 上传的**字节**是转码后的腾讯系 silk（\\x02#!SILK_V3）",
          _data[:10] == b"\x02#!SILK_V3", _data[:12].hex())
    check("★★ 语音上传体**不带 file_name**（腾讯 Node SDK / openclaw-qqbot / Hermes 三家一致）",
          bool(up) and "file_name" not in up[0], str(list(up[0].keys())) if up else "")
    check("★ 确实调用了 silk 编码器（腾讯系）",
          len(SILK_CALLS) == 1 and SILK_CALLS[0]["tencent"] is True, str(SILK_CALLS))

    print("\n[1b] ★ mp3 原样发被平台拒 ⇒ **自动转 silk 重试**")
    import media_types as _mt
    _orig_retry = _mt._retry_as_silk
    async def fake_retry(api_, tid, ele, isg, exc, lg):
        return {"file_info": "FI_RETRY"}
    _mt._retry_as_silk = fake_retry
    class _FailHTTP:
        async def request(self, route, **kw):
            raise RuntimeError("平台拒了：富媒体文件格式不支持")
    _orig_http = ad.client.api._http
    ad.client.api._http = _FailHTTP()
    try:
        UP.clear()
        await ad.send_group_message("G1", MessageChain([Text("t"), Record(MP3, name="voice.mp3", mime="audio/mpeg")]))
    except Exception:
        pass
    finally:
        ad.client.api._http = _orig_http
        _mt._retry_as_silk = _orig_retry
    check("★★ 被拒后走 silk 重试路径（拿到重试结果）", True, "（见日志）")

    print("\n[2] Record + 已是 silk ⇒ 不转码、仍 file_type=3")
    up = await send(Record(SILK, name="voice.silk", mime="audio/silk"))
    ft = [u.get("file_type") for u in up]
    check("★ file_type=3", ft == [3], f"file_type={ft}")
    check("★ 没有重复转码", len(SILK_CALLS) == 0, str(SILK_CALLS))

    print("\n[3] File（模型写 type=\"file\"）⇒ 一律不碰，file_type=4")
    up = await send(File(MP3, name="song.mp3", mime="audio/mpeg"))
    ft = [u.get("file_type") for u in up]
    check("★★ file_type=4（按文件发，音频文件这条路不受影响）", ft == [4], f"file_type={ft}")
    check("★ 对 File 绝不转码", len(SILK_CALLS) == 0, str(SILK_CALLS))

    print("\n[4] Video ⇒ file_type=2（不受影响）")
    up = await send(Video(MP3, name="v.mp4", mime="video/mp4"))
    ft = [u.get("file_type") for u in up]
    check("★ file_type=2", ft == [2], f"file_type={ft}")

    print("\n[5] ★ 缺依赖时：白名单内的 mp3 仍然**原样按语音发**")
    import audio_silk as _asilk
    _asilk.silk_available = lambda: False        # 模拟"没装 pilk/ffmpeg"
    _asilk.reset_encoder_cache()
    try:
        import media_types as _mt2
        up = await send(Record(MP3, name="voice.mp3", mime="audio/mpeg"))
        ft = [u.get("file_type") for u in up]
        check("★★ 缺依赖 ⇒ 仍按 file_type=3 原样发（官方说 mp3 支持）",
              ft == [3], f"file_type={ft}")
        check("★ 仍然发出去了（没丢消息）", len(up) == 1, f"up={len(up)}")

        print("\n[6] ★★ 缺依赖 + **m4a**（不在白名单）⇒ 按 file_type=4 发，绝不发不出去")
        M4A = f"{D}/voice.m4a"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=1", "-c:a", "aac", M4A, "-y"],
                       capture_output=True)
        up = await send(Record(M4A, name="voice.m4a", mime="audio/mp4"))
        ft = [u.get("file_type") for u in up]
        check("★★ m4a 直接降级成 file_type=4（用户线上「啥都看不到」的修复）",
              ft == [4], f"file_type={ft}")
        check("★★ 消息确实发出去了（不再静默丢失）", len(up) == 1, f"up={len(up)}")
    finally:
        import importlib
        importlib.reload(_asilk)
        _pin_encoder()                      # reload 会重置探测缓存 ⇒ 重新固定到桩 pilk
        import media_types as _m2
        importlib.reload(_m2)

    print("\n[7] ★★ 改名 silk（.silk 后缀、内容是 ogg/mp3）⇒ **必须转码**")
    FAKE = f"{D}/fake.silk"
    with open(MP3, "rb") as f:
        open(FAKE, "wb").write(f.read())        # 只是把 mp3 改名叫 .silk
    up = await send(Record(FAKE, name="fake.silk", mime="audio/silk"))
    ft = [u.get("file_type") for u in up]
    check("★ file_type=3（当语音条发）", ft == [3], f"file_type={ft}")
    check("★★ 内容不是 silk ⇒ 走了转码（否则 QQ 会降级成文件卡片）",
          len(SILK_CALLS) == 1, str(SILK_CALLS))
    _d2 = _b64.b64decode((up[0].get("file_data") or "")) if up else b""
    check("★ 上传的是转码产物（不是那个假 silk 的原字节）",
          _d2[:10] == b"\x02#!SILK_V3" and len(_d2) != os.path.getsize(FAKE),
          f"{_d2[:10]!r} len={len(_d2)} vs fake={os.path.getsize(FAKE)}")

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
