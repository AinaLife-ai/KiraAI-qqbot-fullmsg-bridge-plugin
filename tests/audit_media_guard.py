"""★★★ 媒体安全网（HTTP 层）—— 2026-10-09 新增的"最后一道防线"。

## 它防的是什么（线上实况）

用户线上出现过：贴纸的 GIF **原样直传**被平台拒（850019）、语音按 file_type=4
降级成文件卡片，而日志里**一条媒体处理记录都没有** —— 上传层的包装像从来没装上过。
元素层（换壳）明明在跑；装不上的原因受现场限制没查死。

⇒ 直接包 **botpy 的 HTTP 出口**（`client.api._http.request`）：
只要请求体是 `/files` 上传（带 `file_data`），就在发出去之前体检：

* file_type=1 但不是 png/jpeg ⇒ 先规范化（GIF→APNG）；
  仍被"格式不支持"拒 ⇒ **原始字节改按 file_type=4 再发一次**（保投递）；
* file_type=3 但不是 silk ⇒ 就地转 silk（有编码器时）；
* 请求本身的异常**绝不吞**（吞=静默重发，一条失败请求打两次 —— 本套件抓的就是它）。

## 断言

1. 安装/幂等/还原；
2. GIF（file_type=1）被平台以 850019 拒 ⇒ 发出去的是 **APNG**（PNG 魔数 + acTL）；
3. 连 APNG 都被拒 ⇒ 用**原始 GIF 字节**改按文件发（file_type=4，带文件名）；
4. 语音（file_type=3）非 silk ⇒ 发出前转成 silk（魔数 \x02#!SILK_V3）；
5. **失败绝不重发**：非格式类错误 ⇒ 上游只看到一次请求（反向验证本次修复的 bug）；
6. 非上传请求 / 非 /files 路由 ⇒ 一个字节都不动；
7. 还原后行为回到原样。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, botpy_parent as _BOTPY_DIR

import asyncio
import base64
import io
import os
import sys
import types

ROOT = _BR()
sys.path.insert(0, ROOT)
sys.path.insert(0, _BOTPY_DIR())

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


# ---- 桩 pysilk：把任意 PCM 写成腾讯系 silk ----
_PS = types.ModuleType("pysilk")


def _ps_encode(pcm_fp, silk_fp, pcm_rate, bit_rate=None):
    data = pcm_fp.read()
    if not data:
        raise ValueError("empty pcm")
    silk_fp.write(b"\x02#!SILK_V3" + b"\x00" * 64 + b"\xff\xff")
    return 0.04


_PS.encode = _ps_encode
sys.modules["pysilk"] = _PS

import media_types as M  # noqa: E402


class _Log:
    def __init__(self):
        self.lines = []

    def _p(self, lv, a):
        self.lines.append((lv, (a[0] % tuple(a[1:])) if len(a) > 1 else str(a[0])))

    def info(self, *a):
        self._p("info", a)

    def warning(self, *a):
        self._p("warning", a)

    def debug(self, *a):
        pass


class _FakeHTTP:
    """模拟平台上传接口：能按 file_type + 字节内容做判断/拒收。"""

    def __init__(self, reject_gif=True, reject_apng=False, hard_fail=False):
        self.calls = []
        self.reject_gif = reject_gif
        self.reject_apng = reject_apng
        self.hard_fail = hard_fail

    async def request(self, route, **kw):
        body = dict(kw.get("json") or {})
        raw = base64.b64decode(body.get("file_data") or "") if body.get("file_data") else b""
        self.calls.append({"route": str(getattr(route, "path", "")), "body": body, "raw": raw})
        if self.hard_fail and body.get("file_type") == 1:
            raise RuntimeError("boom: 网络抖动")
        if body.get("file_type") == 1:
            if raw[:6] in (b"GIF87a", b"GIF89a") and self.reject_gif:
                raise RuntimeError(
                    "400, {'code': 850019, 'message': '富媒体文件格式不支持'}")
            if raw[:8] == b"\x89PNG\r\n\x1a\n" and self.reject_apng:
                raise RuntimeError(
                    "400, {'code': 850019, 'message': '富媒体文件格式不支持'}")
        if body.get("file_type") == 3:
            if not (raw[:10] == b"\x02#!SILK_V3" or raw[:9] == b"#!SILK_V3"):
                raise RuntimeError(
                    "400, {'code': 850019, 'message': '富媒体文件格式不支持'}")
        return {"file_info": "FI"}


def _client(http):
    c = types.SimpleNamespace()
    c.api = types.SimpleNamespace()
    c.api._http = http
    return c


def _gif_bytes(frames=3, size=(8, 8)):
    from PIL import Image as P

    fs = []
    for i in range(frames):
        fs.append(P.new("RGB", size, (i * 40 % 255, 60, 120)))
    b = io.BytesIO()
    fs[0].save(b, "GIF", save_all=True, append_images=fs[1:], duration=100, loop=0)
    return b.getvalue()


async def main():
    print("═══ 媒体安全网（HTTP 层）═══")
    from botpy.http import Route

    M._HTTP_GUARD_LOGGED.clear()
    try:
        gif = _gif_bytes()
    except Exception as exc:
        print(f"  skip  没有 Pillow（{exc}），只跑安装/还原节")
        gif = None

    print("\n[1] 安装 / 幂等 / 还原")
    http = _FakeHTTP()
    client = _client(http)
    ok1 = M.install_http_guard(client, _Log(), None)
    check("★ 安装成功", ok1 is True)
    fn1 = http.request
    ok2 = M.install_http_guard(client, _Log(), None)
    check("★ 幂等（重复安装不叠加）", ok2 is True and http.request is fn1)
    check("★ 还原成功", M.restore_http_guard(client) is True)
    check("★ 还原后是原函数", http.request is not fn1)
    check("★ 没有 _http 的 client ⇒ 安静返回 False",
          M.install_http_guard(types.SimpleNamespace(), None) is False)

    if gif is None:
        print(f"\n结果：{PASS} passed, {FAIL} failed")
        return 1 if FAIL else 0

    route = Route("POST", "/v2/users/{openid}/files", openid="U1")
    log = _Log()
    http = _FakeHTTP()
    client = _client(http)
    M.install_http_guard(client, log, None)

    print("\n[2] GIF 原图被 850019 拒 ⇒ 自动转档为 APNG")
    M._RAW_IMG_REJECTED.clear()
    result = await http.request(route, json={
        "file_type": 1,
        "file_data": base64.b64encode(gif).decode("ascii"),
        "srv_send_msg": False, "openid": "U1"})
    check("★ 调用没抛（上游拿到 file_info）", result.get("file_info") == "FI", str(result))
    last = http.calls[-1]
    check("★★★ 真正发出去的是 PNG 魔数（APNG）", last["raw"][:8] == b"\x89PNG\r\n\x1a\n",
          last["raw"][:12].hex())
    check("★★★ 带 acTL（动画块保住了）", b"acTL" in last["raw"])
    check("★★ 请求序列：① 原图被拒 ② 转档(APNG)成功（失败绝不重发）",
          len(http.calls) == 2
          and http.calls[0]["raw"][:6] in (b"GIF87a", b"GIF89a")
          and http.calls[-1]["raw"][:8] == b"\x89PNG\r\n\x1a\n",
          str(len(http.calls)))
    check("★ 有日志说明（安全网做了转换）",
          any("媒体安全网" in m for _lv, m in log.lines), str(log.lines[-2:]))

    print("\n[2b] 平台收 GIF ⇒ 原样发（保动画，零转换）")
    M._RAW_IMG_REJECTED.clear()
    log = _Log()
    http = _FakeHTTP(reject_gif=False)
    client = _client(http)
    M.install_http_guard(client, log, None)
    result = await http.request(route, json={
        "file_type": 1,
        "file_data": base64.b64encode(gif).decode("ascii"),
        "srv_send_msg": False, "openid": "U1"})
    check("★ 成功返回", result.get("file_info") == "FI")
    check("★★★ 只发了一次且就是**原始 GIF 字节**（没有转档）",
          len(http.calls) == 1 and http.calls[0]["raw"] == gif,
          f"calls={len(http.calls)} head={http.calls[-1]['raw'][:8].hex()}")

    print("\n[3] 连 APNG 都被拒 ⇒ 原始 GIF 字节改按文件发（保投递）")
    M._RAW_IMG_REJECTED.clear()
    log = _Log()
    http = _FakeHTTP(reject_apng=True)
    client = _client(http)
    M.install_http_guard(client, log, None)
    result = await http.request(route, json={
        "file_type": 1,
        "file_data": base64.b64encode(gif).decode("ascii"),
        "srv_send_msg": False, "openid": "U1"})
    check("★ 最终仍拿到 file_info（消息没丢）", result.get("file_info") == "FI")
    check("★★ 请求序列：① 原图被拒 ② APNG 被拒 ③ 文件发原始 GIF",
          [c["body"].get("file_type") for c in http.calls] == [1, 1, 4],
          str([c["body"].get("file_type") for c in http.calls]))
    check("★★ 文件通道发的是**原始 GIF 字节**",
          http.calls[-1]["raw"] == gif and http.calls[-1]["raw"][:6] in (b"GIF87a", b"GIF89a"))
    check("★ 文件通道带上了文件名", bool(http.calls[-1]["body"].get("file_name")),
          str(http.calls[-1]["body"].get("file_name")))

    print("\n[4] 语音（file_type=3）非 silk ⇒ 发出前转 silk")
    import subprocess
    ffmpeg = None
    import shutil
    if shutil.which("ffmpeg"):
        ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        import audio_silk as _as

        _as.reset_encoder_cache()
        ogg = "/tmp/guard_voice.ogg"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "sine=frequency=330:duration=0.5", "-c:a", "libvorbis",
                        ogg, "-y"], capture_output=True)
        with open(ogg, "rb") as f:
            ogg_bytes = f.read()
        log = _Log()
        http = _FakeHTTP()
        client = _client(http)
        M.install_http_guard(client, log, None)
        result = await http.request(route, json={
            "file_type": 3,
            "file_data": base64.b64encode(ogg_bytes).decode("ascii"),
            "srv_send_msg": False, "openid": "U1"})
        check("★ 调用成功", result.get("file_info") == "FI")
        last = http.calls[-1]
        check("★★ file_type 仍是 3（语音条）", last["body"].get("file_type") == 3)
        check("★★★ 发出去的是腾讯系 silk（\\x02#!SILK_V3）",
              last["raw"][:10] == b"\x02#!SILK_V3", last["raw"][:12].hex())
        check("★ 日志说明转码发生",
              any("媒体安全网" in m for _lv, m in log.lines), str(log.lines[-2:]))
    else:
        print("  skip  没有 ffmpeg，跳过语音节")

    print("\n[5] ★★ 失败绝不重发（本套件抓的第一个 bug：静默重发）")
    http = _FakeHTTP(hard_fail=True)
    client = _client(http)
    M.install_http_guard(client, _Log(), None)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    raised = False
    try:
        await http.request(route, json={
            "file_type": 1, "file_data": base64.b64encode(png).decode("ascii"),
            "srv_send_msg": False, "openid": "U1"})
    except RuntimeError:
        raised = True
    check("★ 异常原样上抛（没被安全网吞掉）", raised)
    check("★★ 只被调用一次（旧实现会静默重发 → 2 次）", len(http.calls) == 1,
          str(len(http.calls)))

    print("\n[6] 非上传请求 / 非 /files 路由 ⇒ 一个字节都不动")
    http = _FakeHTTP()
    client = _client(http)
    M.install_http_guard(client, _Log(), None)
    msg_route = Route("POST", "/v2/users/{openid}/messages", openid="U1")
    await http.request(msg_route, json={"content": "hi", "msg_type": 0})
    prep_route = Route("POST", "/v2/users/{user_id}/upload_prepare", user_id="U1")
    await http.request(prep_route, json={"file_type": 1, "file_data": "AAAA", "file_name": "a.gif"})
    check("★ 消息类请求原样通过", http.calls[0]["body"].get("content") == "hi")
    check("★ 非 /files 路由原样通过（没被当成上传处理）",
          http.calls[1]["body"].get("file_data") == "AAAA")

    print("\n[7] 还原后行为回到原样（GIF 直传照旧被拒、不再转换）")
    http = _FakeHTTP()
    client = _client(http)
    M.install_http_guard(client, _Log(), None)
    M.restore_http_guard(client)
    raised = False
    try:
        await http.request(route, json={
            "file_type": 1, "file_data": base64.b64encode(gif).decode("ascii"),
            "srv_send_msg": False, "openid": "U1"})
    except RuntimeError:
        raised = True
    check("★ 还原后：GIF 直传原样被拒（回到平台原行为）", raised and len(http.calls) == 1)
    check("★ 还原后请求里就是原始 GIF 字节",
          http.calls[0]["raw"] == gif, http.calls[0]["raw"][:12].hex())

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
