"""★ markdown 图片修复回归：本地图要变成公网地址，且**md 结构一字不动**。

## 背景（2026-10-07 线上）

用户让 bot 用 markdown 发图，群里显示成 `[香香]`（alt 文字）。查证官方文档后确认
三条硬约束：

1. **md 内图片只吃公网 URL**
   ：「请使用**可在公网访问的资源 url**，开放平台会下载转存该资源。」
2. **`msg_type` 互斥**：`0=纯文本 / 2=Markdown / 7=富媒体` ⇒ 图文无法同条。
3. **普通上传只返回 `file_info`**（不透明串），拿不到可引用的 URL。

**但官方留了口子**：走**分片上传合并**（`upload_id` 路径）时，
响应会多返回一个 **`raw_url`** ——「文件下载链接（COS 预签名 GET URL）」。

⇒ 当地图 → 分片上传 → 拿到公网 URL → **原地填回 md**。
**标题/列表/引用/链接/代码块全部保留，行数不变** —— 只换 `(...)` 里的 URL。

## 用户的关切（必须守住）

> 「你确认一下，你的这个方式**不会严重破坏 md 的格式**，你懂吧？
>   如果 md 格式被破坏，就很没意思」

本测试就是钉死这一点：**逐字比对**，除图片 URL 外不允许有任何差异。
"""
import asyncio
import os
import struct
import sys
import zlib

ROOT = "/var/minis/workspace/qqbot_bridge_review"
sys.path.insert(0, f"{ROOT}/bridge")


def _normalize(md: str) -> str:
    """把图片的**尺寸后缀**去掉，便于断言"除图片 URL 与尺寸外逐字相同"。

    ★ v1.5.6 起我们会给图片补 `#0 #0`（QQ 的 md 图片需要尺寸后缀才渲染），
      所以"逐字相同"的断言要允许这两个已知差异：URL 与尺寸。
    """
    import re as _re
    return _re.sub(r"(!\[[^\]]*?)\s*#[^#\]]*\s*#[^#\]]*\s*(\]\()", r"\1\2", md)


PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def _png(w=8, h=8) -> bytes:
    raw = b"".join(b"\x00" + bytes([(x * 7) % 256, (y * 5) % 256, 128])
                   for y in range(h) for x in range(w))

    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


MD_FULL = """# 🎤 标题

**加粗** ｜ _斜体_ ｜ ~~删除线~~

![本地图](data/temp/compressed_abc.jpg)

> 引用块第一行
> 引用块第二行

- 列表1
- 列表2
  - 嵌套

1. 有序1
2. 有序2

[链接](https://www.qq.com)

```python
print("code block")
```

| 表头A | 表头B |
|------|------|
| 1 | 2 |
"""


async def main():
    import md_media as M

    workdir = "/tmp/md_media_test"
    os.makedirs(f"{workdir}/data/temp", exist_ok=True)
    os.chdir(workdir)
    with open("data/temp/compressed_abc.jpg", "wb") as f:
        f.write(_png())
    M.clear_caches()

    print("[1] 本地图片 → 公网 URL，且 md 结构逐字保留")
    calls = {"upload": 0}

    async def fake_upload(client, tid, isg, path, logger=None):
        calls["upload"] += 1
        return "https://cos.example.com/real.png?sign=zzz"
    M.upload_local_to_public_url = fake_upload

    out = await M.fix_markdown_images(MD_FULL, client=None, target_id="G1",
                                      is_group=True)

    check("★ 图片 URL 已换成公网地址", "https://cos.example.com/real.png?sign=zzz" in out)
    check("本地路径已消失", "data/temp/" not in out)
    check("★★ 除图片 URL 与尺寸外**逐字相同**（md 正文零改动）",
          _normalize(out).replace("https://cos.example.com/real.png?sign=zzz",
                      "data/temp/compressed_abc.jpg") == MD_FULL)
    check("★★ 行数不变", len(MD_FULL.splitlines()) == len(out.splitlines()))
    for name, frag in [
        ("标题 #", "# 🎤 标题"), ("加粗", "**加粗**"), ("斜体", "_斜体_"),
        ("删除线", "~~删除线~~"), ("引用块", "> 引用块第二行"),
        ("无序列表", "- 列表1\n- 列表2"), ("嵌套列表", "  - 嵌套"),
        ("有序列表", "1. 有序1\n2. 有序2"), ("链接", "[链接](https://www.qq.com)"),
        ("代码块", '```python\nprint("code block")\n```'),
        ("表格", "| 表头A | 表头B |"),
        ("图片 alt 文字保留", "![本地图"),      # 后面会跟尺寸后缀，故不写闭括号
    ]:
        check(f"格式保留：{name}", frag in out)

    print("\n[2] 重复图片 / 重复发送不重复上传（缓存）")
    before = calls["upload"]
    await M.fix_markdown_images(MD_FULL, client=None, target_id="G1", is_group=True)
    check("★ 第二次不再重复上传（命中缓存）", calls["upload"] == before,
          f"上传次数 {before} → {calls['upload']}")

    print("\n[3] 公网 URL：验真（跳转/非图片 ⇒ 不硬塞）")

    async def fake_resolve_bad(client, url, logger=None, timeout=12.0):
        return None                      # 模拟"不是可直接下载的图片"
    M.resolve_image_url = fake_resolve_bad
    M.clear_caches()
    md_remote = "![香香](https://zh.wikipedia.org/wiki/Special:FilePath/Luka.png)\n"
    out2 = await M.fix_markdown_images(md_remote, client=None, target_id="G1",
                                       is_group=True)
    check("★ 验不出来的 URL **原样保留**（不静默改动用户内容；只补尺寸）",
          _normalize(out2) == md_remote and "zh.wikipedia.org" in out2, repr(out2))

    async def fake_resolve_ok(client, url, logger=None, timeout=12.0):
        return "https://upload.wikimedia.org/real.png"    # 模拟"跟随跳转后的真地址"
    M.resolve_image_url = fake_resolve_ok
    M.clear_caches()
    out3 = await M.fix_markdown_images(md_remote, client=None, target_id="G1",
                                       is_group=True)
    check("★ 能跟随跳转到真图 ⇒ 替换成真地址",
          "upload.wikimedia.org/real.png" in out3, repr(out3))
    check("结构仍未被破坏（除图片 URL/尺寸外逐字相同）",
          _normalize(out3).replace("https://upload.wikimedia.org/real.png",
                       "https://zh.wikipedia.org/wiki/Special:FilePath/Luka.png") == md_remote)

    print("\n[3b] ★ 远程图也转存到 QQ 的 COS（不再只验真）")
    M.resolve_image_url = lambda *a, **k: None      # 故意让验真失败
    M.clear_caches()
    remote_calls = {"n": 0}

    async def fake_remote_upload(client, tid, isg, url, logger=None, timeout=30.0,
                                 want_size=False):
        remote_calls["n"] += 1
        r = "https://cos.example.com/converted.png?sign=qqq"
        return (r, (300, 180)) if want_size else r
    M.upload_remote_to_public_url = fake_remote_upload
    md_r = "![香香](https://storage.moegirl.org.cn/moegirl/commons/3/39/Luka_v4x_final.png)\n"
    out_r = await M.fix_markdown_images(md_r, client=None, target_id="G1", is_group=True)
    check("★★ 远程图被转存成 QQ 自己的 COS 地址",
          "https://cos.example.com/converted.png?sign=qqq" in out_r, repr(out_r))
    check("原站地址已不在 md 里", "moegirl" not in out_r)
    check("结构仍未被破坏（除图片 URL/尺寸外逐字相同）",
          _normalize(out_r).replace("https://cos.example.com/converted.png?sign=qqq",
                        "https://storage.moegirl.org.cn/moegirl/commons/3/39/Luka_v4x_final.png") == md_r)

    print("\n[3c] 转存失败 ⇒ 退回验真，仍然不许丢内容")
    M.clear_caches()

    async def fail_remote(client, tid, isg, url, logger=None, timeout=30.0,
                          want_size=False):
        return (None, None) if want_size else None
    M.upload_remote_to_public_url = fail_remote

    async def ok_resolve(client, url, logger=None, timeout=12.0):
        return url
    M.resolve_image_url = ok_resolve
    out_r2 = await M.fix_markdown_images(md_r, client=None, target_id="G1", is_group=True)
    check("★ 转存失败但地址确是真图 ⇒ 保留原地址（不丢内容）",
          _normalize(out_r2) == md_r, repr(out_r2))

    print("\n[4] 没有图片的 md：一个字都不许动")
    plain = "# 只有文字\n\n- 列表\n"
    out4 = await M.fix_markdown_images(plain, client=None, target_id="G1", is_group=True)
    check("无图片 ⇒ 原样返回", out4 == plain)

    print("\n[5] 上传失败：必须原样发送，绝不丢消息")

    async def boom(client, tid, isg, path, logger=None):
        return None
    M.upload_local_to_public_url = boom
    M.clear_caches()
    out5 = await M.fix_markdown_images(MD_FULL, client=None, target_id="G1", is_group=True)
    check("★ 转存失败 ⇒ 原样返回（不抛异常、不删内容）", _normalize(out5) == MD_FULL)

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
