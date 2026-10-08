"""★ 语音条 vs 音频文件：**两条路都要能走**（2026-10-08）。

官方限制：`file_type=3` 语音 **只认 silk**（mp3 直接上传会降级成文件）。
所以「发语音条」必须先把音频转 silk；而「发音频文件」应保持原样。

本测试断言：

1. `Record` + 非 silk 音频 ⇒ **会自动转 silk**，且 `file_type=3`；
2. `Record` + 已是 silk    ⇒ 不转码、路径不变、`file_type=3`；
3. **转码不可用时 ⇒ 退回 `file_type=4` 按文件发**（绝不丢消息）；
4. `File` 元素（模型写 `type="file"`）⇒ **一律不碰**，`file_type=4`
   ⇒ 「想发文件」这条路不被影响；
5. `Video` ⇒ `file_type=2`（不受本改动影响）。
"""
import asyncio
import os
import subprocess
import sys
import types

ROOT = "/var/minis/workspace/qqbot_bridge_review"
GEN = os.environ.get("KIRA_CORE_GEN", "3")
sys.path.insert(0, f"{ROOT}/kira-v3" if GEN == "3" else f"{ROOT}/kira-core")
sys.path.insert(0, f"{ROOT}/bridge")
sys.path.insert(0, "/tmp/botpy_src/botpy-master")

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

    print("\n[1] Record + mp3 ⇒ 自动转 silk 且 file_type=3（语音条）")
    up = await send(Record(MP3, name="voice.mp3", mime="audio/mpeg"))
    ft = [u.get("file_type") for u in up]
    check("★ file_type=3（语音条）", ft == [3], f"file_type={ft}")
    check("★ 真的调用了 silk 转码", len(SILK_CALLS) == 1, str(SILK_CALLS))
    check("★ 传的是转码后的 silk（文件名 .silk）",
          bool(up) and str(up[0].get("file_name", "")).endswith(".silk"),
          str(up[0].get("file_name")) if up else "")

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

    print("\n[5] ★ 转码不可用（缺依赖）⇒ 退回 file_type=4，绝不丢消息")
    import audio_silk as _asilk
    _saved = _asilk.silk_available
    _asilk.silk_available = lambda: False        # 模拟"没装 pilk/ffmpeg"
    _asilk.clear_cache()
    try:
        import importlib
        importlib.reload(_asilk)                # 让模块内引用也更新
        _asilk.silk_available = lambda: False
        import media_types as _mt
        importlib.reload(_mt)
        up = await send(Record(MP3, name="voice.mp3", mime="audio/mpeg"))
        ft = [u.get("file_type") for u in up]
        check("★★ 缺依赖时退回 file_type=4（按文件发）", ft == [4], f"file_type={ft}")
        check("★ 仍然发出去了（没丢消息）", len(up) == 1, f"up={len(up)}")
    finally:
        import importlib
        import audio_silk as _a2
        importlib.reload(_a2)
        import media_types as _m2
        importlib.reload(_m2)

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
