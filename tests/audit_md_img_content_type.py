"""★★★ md 图片「加载失败」的根因回归：**分片 PUT 必须带图片 Content-Type**。

## 线上现象（2026-10-08 用户实测）

本地图和真公网图都换成了 QQ 自己签发的 `raw_url`（转存成功、`#宽px #高px` 尺寸
也对），但手机 QQ 里两张都显示 **「加载失败」**。

## 根因

官方「分片上传」四步：`upload_prepare` → **逐片 PUT 到预签名 URL** →
`upload_part_finish` → 带 `upload_id` 合并拿 `file_info` / `raw_url`。

**分片 PUT 的 `Content-Type` 决定平台把对象存成什么类型** ——
不带的话 COS 默认落成 `application/octet-stream`，
QQ 的 markdown 图片链路判定"这不是图片" ⇒ 前端「加载失败」。

佐证（三条独立证据）：

1. 用户日志里 QQ 自己转存出来的对象头就是 `Content-Type: application/octet-stream`；
   而官方文档那条**能正常渲染**的示例图是 `image/png`；
2. 参考实现 `KasumiYuku/Aurorix`（Go，`lib/api/files.go`）在分片 PUT 上显式带
   `Content-Type`，注释写得很直白：
   「预签名 URL 只签了 host，多带一个 Content-Type 不会破签，
     但它**决定平台存下来是什么类型** —— 官方直链要被 QQ 当图片渲染，就靠这个头」；
3. 同项目 `UploadOfficialImage` 在没有 MIME 时**按字节嗅探**（`images.ProbeMime`）。

本测试：真跑 `md_media._upload_bytes_to_qq`（只把 `ClientSession.put` 换成记录版），
断言每个分片都带**按字节嗅探出来的**图片 Content-Type。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR

import asyncio
import random
import sys

sys.path.insert(0, _BR())

import aiohttp as _ah

SEEN = []
_RealSession = _ah.ClientSession


class _FakeResp:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _PatchedSession(_RealSession):
    def put(self, url, **kw):
        data = kw.get("data", b"")
        SEEN.append({"url": url, "headers": dict(kw.get("headers") or {}),
                     "size": len(data), "data": data})
        return _FakeResp()


_ah.ClientSession = _PatchedSession

import md_media as M  # noqa: E402

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

    def _p(self, level, a):
        self.lines.append((level, (a[0] % tuple(a[1:])) if len(a) > 1 else str(a[0])))

    def info(self, *a):
        self._p("info", a)

    def warning(self, *a):
        self._p("warning", a)

    def debug(self, *a):
        pass


class _HTTP:
    def __init__(self, parts):
        self.parts = parts

    async def request(self, route, **kw):
        path = getattr(route, "path", "")
        if "upload_prepare" in path:
            return {"upload_id": "UP1", "parts": self.parts}
        if "/files" in path:
            return {"file_info": "FI", "raw_url": "https://cos.example.com/x.png?sig=1"}
        return {}


class _API:
    def __init__(self, parts):
        self._http = _HTTP(parts)


class _Client:
    def __init__(self, parts):
        self.api = _API(parts)


def _parts(total, bs):
    n = max(1, (total + bs - 1) // bs)
    return [{"index": i, "block_size": str(min(bs, total - i * bs)),
             "presigned_url": f"http://cos/put/{i}"} for i in range(n)]


PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 +
       (300).to_bytes(4, "big") + (400).to_bytes(4, "big") + b"\x00" * 64)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
GIF = b"GIF89a" + b"\x00" * 64
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"VP8 " + b"\x00" * 64


async def main():
    print("═══ md 图片转存：分片 Content-Type ═══")

    print("\n[0] 嗅探：按**字节**而不是扩展名")
    check("★ PNG 魔数 ⇒ image/png（哪怕文件名说是 jpg）",
          M.sniff_image_mime(PNG, "x.jpg") == "image/png")
    check("★ JPEG 魔数 ⇒ image/jpeg（哪怕文件名说是 png）",
          M.sniff_image_mime(JPEG, "x.png") == "image/jpeg")
    check("★ GIF ⇒ image/gif", M.sniff_image_mime(GIF, "x.gif") == "image/gif")
    check("★ WEBP ⇒ image/webp", M.sniff_image_mime(WEBP, "x.webp") == "image/webp")
    check("★ 认不出魔数时按扩展名兜底",
          M.sniff_image_mime(b"\x00\x01\x02", "a.png") == "image/png")
    check("★ 都认不出 ⇒ 保守给 image/jpeg",
          M.sniff_image_mime(b"\x00\x01", "") == "image/jpeg")

    print("\n[1] 真跑分片上传：每个分片都要带图片 Content-Type")
    random.seed(20261008)
    data = PNG + random.randbytes(900_000)
    parts = _parts(len(data), 400_000)
    SEEN.clear()
    log = _Log()
    raw = await M._upload_bytes_to_qq(_Client(parts), "G1", True, data, "loli_md.jpg",
                                      logger=log)
    check("★ 成功拿到 raw_url", raw == "https://cos.example.com/x.png?sig=1", repr(raw))
    check("★ 分片数正确", len(SEEN) == len(parts), f"{len(SEEN)}/{len(parts)}")
    check("★★★ 每个分片都带 Content-Type=image/png",
          all(s["headers"].get("Content-Type") == "image/png" for s in SEEN),
          str([s["headers"] for s in SEEN]))
    check("★ 字节总数不变（没有因为加头破坏分片）",
          sum(s["size"] for s in SEEN) == len(data),
          f"{sum(s['size'] for s in SEEN)} vs {len(data)}")

    print("\n[2] 反向验证：不带头的旧行为会被记出来")
    old = [{"headers": {}}]
    check("★★ 旧行为（无 Content-Type）≠ 新行为 —— 本测试能抓到该回归",
          not all(s["headers"].get("Content-Type") == "image/png" for s in old))

    print("\n[2b] ★★ md 里的 GIF：也要能内嵌 ⇒ 上传前转 APNG（PNG 魔数 + acTL）")
    try:
        import io as _io

        from PIL import Image as _PIL

        _fr = [_PIL.new("RGB", (6, 6), (255, 0, 0)), _PIL.new("RGB", (6, 6), (0, 128, 255))]
        _b = _io.BytesIO()
        _fr[0].save(_b, "GIF", save_all=True, append_images=_fr[1:], duration=80)
        gif = _b.getvalue()
    except Exception as exc:
        gif = None
        print(f"  skip  没有 Pillow，跳过（{exc}）")
    if gif:
        import random as _r

        _r.seed(7)
        gdata = gif + _r.randbytes(900_000)
        gparts = _parts(len(gdata), 400_000)
        SEEN.clear()
        graw = await M._upload_bytes_to_qq(_Client(gparts), "G1", True, gdata, "meme.gif",
                                          logger=_Log())
        check("★ md 里的 GIF 也能上传成功（拿到公网地址）", bool(graw), repr(graw))
        _up = b"".join(s["data"] for s in SEEN)
        check("★★★ 上传的是 **APNG**：头部是 PNG 魔数（不是 GIF）",
              _up[:8] == b"\x89PNG\r\n\x1a\n", _up[:12].hex())
        check("★★★ 带 acTL 动画块 ⇒ **md 里的动图有动画**（客户端支持时）",
              b"acTL" in _up)
        check("★★ 分片仍带图片 Content-Type（image/png）",
              all(s["headers"].get("Content-Type", "").startswith("image/") for s in SEEN),
              str([s["headers"] for s in SEEN]))
        check("★ 上传的字节 != 原始 GIF 字节（确实转换过）", _up != gdata)

    print("\n[3] 直链自检（观测）被调用")
    check("★ 自检真的跑了（假 URL ⇒ 记一条 warning，不影响发送）",
          any("直链自检" in msg for _lv, msg in log.lines),
          str(log.lines[-3:]))

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
