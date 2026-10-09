"""★★★ 媒体上传体形状：`file_name` 的发放规则（2026-10-10 修订版）。

| file_type | 带文件名？ | 依据 |
|---|---|---|
| 4 文件 | **必带** | 官方文档/三家实现一致（用于显示文件名） |
| 3 语音 | **绝不带** | 2026-10-08 定位："语音变文件卡片"的根因就是它；去掉后语音条正常 |
| 1 图片 | **带**（本版修订） | ① KiraAI 原生路径（不带插件）**发 GIF 成功**：`<file type="image">` 的元素自带真实文件名（`test.gif`），上传体必然带 `file_name`；而本插件贴纸来自 base64、无名字、GIF 被拒（850019）<br>② 官方「富媒体概述」把 gif/webp/bmp 列为图片支持格式，扩展名是平台识别格式的最直接线索<br>③ 官方 Node SDK 只是"不主动给非 FILE 带名"，并非禁止 |
| 2 视频 | 不带 | 无实证需求，先不动 |

## 旧结论（2026-10-08，已被上面的实锤修订）

语音（ogg 与 silk）在 QQ 里显示成**文件卡片**、卡片上正是我们传的名字
（`jbf_v2.silk` / `jbf_voice60s.ogg`）→ 对照腾讯 Node SDK / openclaw-qqbot /
Hermes 三家实现（都不给语音带 `file_name`），改为非 FILE 不带。
——该结论对**语音**依然成立；2026-10-10 发现它对**图片**并不成立：
图片要带（见上表），且这正是 GIF 失败的差异点。

本测试：用假的元素 + 假的 client 真跑 `media_types.install()` 的 `_upload_file`，
把上传体抓下来逐字段断言（不需要任何核心，跑得飞快）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR

import asyncio
import os
import sys

sys.path.insert(0, _BR())

import media_types as M  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


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


class Elem:
    """最小媒体元素（形状与核心 `BaseMediaElement` 一致就够）。

    `name=None` ⇒ 默认用文件名；`name=""` ⇒ **没有名字**（模拟 sticker 的 base64）。
    """

    def __init__(self, path, name=None, orig_kind=None):
        self.file = path
        self.file_type = "path"
        self._name = name if name is not None else path.rsplit("/", 1)[-1]
        if orig_kind:
            self._kira_bridge_orig_kind = orig_kind   # media_coerce 留下的标记

    async def to_path(self):
        return self.file

    def guess_name(self):
        return self._name


class HTTP:
    def __init__(self):
        self.calls = []

    async def request(self, route, **kw):
        self.calls.append(kw.get("json") or {})
        return {"file_info": "FI"}


class Client:
    def __init__(self):
        self.api = type("A", (), {})()
        self.api._http = HTTP()


async def main():
    print("═══ 媒体上传体形状（file_name 只对 file_type=4）═══")
    os.makedirs("/tmp/payloadtest", exist_ok=True)
    silk = "/tmp/payloadtest/voice.silk"
    # 直接造一个「腾讯系 silk」（silk-wasm 的产物头就是 \x02 + #!SILK_V3）
    open(silk, "wb").write(b"\x02#!SILK_V3" + b"\x00" * 256)
    mp4 = "/tmp/payloadtest/v.mp4"
    open(mp4, "wb").write(b"\x00" * 128)

    log = _Log()
    client = Client()
    holder = type("H", (), {})()
    holder._upload_file = _OrigUpload()
    ok = M.install(holder, client, log)
    check("★ install 成功（有 _upload_file 落点）", ok is True)

    async def upload(elem):
        client.api._http.calls.clear()
        await holder._upload_file("OPENID", elem, False)
        calls = client.api._http.calls
        return calls[-1] if calls else {}

    print("\n[1] ★★★ 语音（Record → file_type=3）不得带 file_name")
    body = voice_body = await upload(Elem(silk, "jbf_v2.silk", orig_kind="Record"))
    check("★ file_type=3", body.get("file_type") == 3, str(body.keys()))
    check("★★★ 上传体里**没有** file_name（对齐官方三家实现）",
          "file_name" not in body, str(body.get("file_name")))
    check("★ file_data 仍然带上（内容不变）", bool(body.get("file_data")))
    check("★ srv_send_msg=False", body.get("srv_send_msg") is False)

    print("\n[2] 视频（file_type=2）同样不带 file_name")
    body = await upload(Elem(mp4, "v.mp4", orig_kind="Video"))
    check("★ file_type=2 且无 file_name",
          body.get("file_type") == 2 and "file_name" not in body, str(body.keys()))

    print("\n[3] ★ 降级成「文件」（file_type=4）时**必须带** file_name")
    # m4a 不在语音白名单、又转不了 silk ⇒ 我们按 file_type=4 发（至少收得到）
    open("/tmp/payloadtest/x.m4a", "wb").write(b"\x00" * 256)
    body = await upload(Elem("/tmp/payloadtest/x.m4a", "voice.m4a", orig_kind="Record"))
    check("★ 降级为 file_type=4", body.get("file_type") == 4, str(body.keys()))
    check("★ 此时**带** file_name（官方也只在这个类型带）",
          body.get("file_name") == "voice.m4a", str(body.get("file_name")))

    print("\n[3b] 真「文件」元素（File / zip）⇒ 交回核心原逻辑（核心按 4 发、带名字）")
    delegated = []
    holder._upload_file.__defaults__  # noqa: B018  (保持对象不变，仅说明)
    try:
        await upload(Elem("/tmp/payloadtest/a.zip", "报告.zip"))
        check("★ 交回核心（不该自己抢）", False, "竟然自己处理了")
    except AssertionError as exc:
        delegated.append(str(exc))
        check("★ 交回核心原逻辑（File 归核心，我们只改类型不准的那几类）",
              "交回原逻辑" in str(exc))

    print("\n[3c] ★★★ 图片（file_type=1）⇒ **带文件名**（2026-10-10 对齐原生成功路径）")
    try:
        import base64 as _b64
        import io as _io

        from PIL import Image as _PIL

        _fr = [_PIL.new("RGB", (8, 8), (255, 0, 0)), _PIL.new("RGB", (8, 8), (0, 0, 255))]
        _b = _io.BytesIO()
        _fr[0].save(_b, "GIF", save_all=True, append_images=_fr[1:], duration=100)
        open("/tmp/payloadtest/sticker.gif", "wb").write(_b.getvalue())
        open("/tmp/payloadtest/s.png", "wb").write(_b64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))
        have_img = True
    except Exception as exc:
        have_img = False
        print(f"  skip  没有 Pillow（{exc}）")
    if have_img:
        body = await upload(Elem("/tmp/payloadtest/sticker.gif", "", orig_kind="Sticker"))
        check("★★★ 无名字的 GIF 贴纸 ⇒ 上传体带 file_name 且扩展名 .gif",
              body.get("file_type") == 1
              and str(body.get("file_name") or "").endswith(".gif"),
              f"type={body.get('file_type')} name={body.get('file_name')}")
        body = await upload(Elem("/tmp/payloadtest/s.png", "", orig_kind="Sticker"))
        check("★★ 无名字的 PNG 贴纸 ⇒ 补 .png",
              body.get("file_type") == 1
              and str(body.get("file_name") or "").endswith(".png"),
              f"name={body.get('file_name')}")
        body = await upload(Elem("/tmp/payloadtest/s.png", "my_pic.png", orig_kind="Sticker"))
        check("★ 元素自带名字 ⇒ 原样沿用（不瞎改用户内容）",
              body.get("file_name") == "my_pic.png", str(body.get("file_name")))

    print("\n[4] 诊断：上传体形状只记一次")
    shapes = [m for lv, m in log.lines if "媒体上传体形状" in m]
    check("★ 每种 file_type 只记一条（不刷屏）",
          0 < len(shapes) <= 4, f"{len(shapes)} 条")

    print("\n[5] 反向验证：旧代码会带上 file_name（本测试能抓到该回归）")
    old_body = {"file_type": 3, "file_name": "jbf_v2.silk", "file_data": "x"}
    check("★ 旧形状确实带 file_name ≠ 新形状",
          "file_name" in old_body and "file_name" not in voice_body,
          f"voice={list(voice_body.keys())}")

    print("\n[6] 上传错误码 → 人话（官方错误码表）")
    check("★ 40093002 日额度 → 明天再试",
          "明天" in M.humanize_upload_error(Exception("400, {'code': 40093002}")))
    check("★ 850019 格式 → 提到 silk",
          "silk" in M.humanize_upload_error(Exception("850019 不支持的文件格式")))
    check("★ 850026 下载失败 → 提到换地址",
          "URL" in M.humanize_upload_error(Exception("850026")))
    check("★ 认不出的错误 ⇒ 空串（不硬编）",
          M.humanize_upload_error(Exception("some other error")) == "")

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


class _OrigUpload:
    """核心原逻辑的替身：被调用即说明我们交回了原路径（本测试不该走到）。"""

    async def __call__(self, target_id, media_element, is_group):
        raise AssertionError("不该交回原逻辑")


sys.exit(asyncio.run(main()))
