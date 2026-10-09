"""修正 QQ 官方适配器上传时的 `file_type` —— 让**视频/语音**以原生形态发出。

## 问题（2026-10-07 查证官方文档）

框架（2.x 与 3.0 都一样）在 `_upload_file` 里写死：

```python
file_type = 1 if isinstance(media_element, Image) else 4
```

而官方的 `file_type` 是四类：

| 值 | 类型 | 格式 | 软限制 | 发出后的形态 |
|----|------|------|--------|--------------|
| 1 | 图片 | png / jpg | 20 MB | 直接展示图片 |
| 2 | 视频 | mp4 | 30 MB | 视频封面、可播放 |
| 3 | 语音 | silk | 20 MB | 语音条 |
| 4 | 文件 | 任意 | 200 MB | 文件卡片、需下载 |

⇒ 框架把 `Video` / `Record` **一律当 4=文件** 上传，
于是视频/语音在 QQ 里显示成**文件卡片**（要点开下载），
而**不是**「可播放的视频」/「语音条」。

## 怎么做（纯插件侧，不动核心）

`_upload_file` 是**类方法**，插件把它包一层，只改 `file_type` 的计算，
其余（URL 直传 / 本地 base64 / 路由）**逐行复用核心的实现**，
行为上只多一件事：类型判断更准。

核心这段逻辑很短且多年未变（2.x / 3.0 逐行一致），
所以这里直接照搬，并在开头做**能力探测**，核心若改了形状就自动放弃接管
（返回 False，行为完全退回原样，绝不半吊子）。

位置差异（两代不同，都要处理）：

* **3.0**：`QQOfficialIMCapability._upload_file`（`im.py`）
* **2.x**：`QQOfficialAdapter._upload_file`（`qq_official.py`）
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import os
from pathlib import Path
from typing import Any, Optional

#: 官方 file_type
FT_IMAGE, FT_VIDEO, FT_RECORD, FT_FILE = 1, 2, 3, 4

_VIDEO_EXT = {".mp4"}
_RECORD_EXT = {".silk", ".mp3", ".wav", ".ogg", ".m4a", ".amr", ".aac", ".flac"}

#: 文档里"语音"一节提到的格式 ——**转码不可用时**的兜底直传白名单。
#:
#: ⚠ 注意与"官方确证可直传"的差别：腾讯官方插件
#: `tencent-connect/openclaw-qqbot` 的 `voiceDirectUploadFormats`
#: （注释：「QQ 平台支持直传的音频格式（出站：跳过 →SILK 转换）」）
#: **默认只有 `['.wav','.mp3','.silk']`**，其余（ogg / m4a / amr …）它**一律先转 SILK**。
#: 而官方文档「富媒体概述」写的是 `silk/mp3/wav/ogg`。
#:
#: 实测（用户 2026-10-08 日志/截图）：**ogg 直传被平台降级成了文件卡片**。
#: ⇒ 所以正常路径是"**能转就转**"（只信 silk）；这个白名单**只在编码器不可用时**
#: 用来决定"要不要再试一次直传"（含 ogg，赌平台版本差异），
#: 不在白名单里的（m4a/amr/…）**一开始就按文件发**，绝不赌。
_DOC_AUDIO_EXT = {".silk", ".mp3", ".wav", ".ogg"}


#: 哨兵：源文件本来就是 silk，**无需改路径、file_type 保持 3**
KEEP_AS_IS = "\x00KEEP"


def _record_ext(path: str) -> str:
    return os.path.splitext(str(path or "").split("?")[0])[1].lower()


async def maybe_convert_to_silk(media_element: Any, logger_: Any) -> Optional[str]:
    """决定 `Record` 音频该怎么发（**只信 silk**，其余尽量转）。

    ## 三种线上形态（2026-10-08 用户实测，都必须能变成语音条）

    | 源文件 | 旧行为（都失败） | 现在的行为 |
    |--------|------------------|------------|
    | `.ogg` 真音频 | 按官方文档"原样发" | **转 silk** 再发 |
    | `.m4a` 真音频 | 原样发被拒 → 重试 → 可能整条发不出去 | **转 silk** 再发 |
    | 内容是 silk（腾讯/标准系） | 原样发 | 校验+补腾讯系头后发 |
    | 扩展名 `.silk` 但内容不是 silk（只是改名） | 原样发 ⇒ **文件卡片** | **当普通音频转 silk** |

    ⇒ 判据收敛成一句话：**内容不是真 silk 就转**；
    只有**编码器不可用**时才退回直传（且只对文档列出的格式）。

    返回值语义（调用方必须区分）：

    * ``KEEP_AS_IS`` —— 源文件本身就是（合法的）silk，**路径不用改**，类型保持 3；
    * ``<路径>``     —— 已转码成 silk，改用这个路径；
    * ``None``       —— 转不了（缺依赖 / 解码失败）⇒ 调用方按"能不能直传"决定，
      见 :func:`direct_upload_ok`。
    """
    try:
        if getattr(media_element, "file_type", None) == "url":
            return None                       # 远程地址：交给平台自己处理
        from audio_silk import silk_magic_ok, to_silk_if_needed
        path = await media_element.to_path()
        if not path or not os.path.isfile(path):
            return None
        # ① 内容**确实是** silk ⇒ 只做腾讯系头校验（是则原路径复用）
        if silk_magic_ok(path):
            return KEEP_AS_IS if await to_silk_if_needed(path, logger_=logger_) else None
        # ② 其它一律尝试转 silk（含"改名 silk"与 ogg/m4a/amr…）
        silk = await to_silk_if_needed(path, logger_=logger_)
        if not silk:
            return None
        if os.path.realpath(silk) == os.path.realpath(path):
            return KEEP_AS_IS
        return silk
    except Exception as exc:
        if logger_ is not None:
            logger_.debug("[QQBOT-BRIDGE] silk 判断失败: %s", exc)
        return None


def direct_upload_ok(media_element: Any) -> bool:
    """转码不可用时，这个音频**能不能按 file_type=3 直接上传**？

    * 真 silk ⇒ 能（但真 silk 走不到这里）；
    * 扩展名在官方文档的语音格式白名单里（silk/mp3/wav/ogg）⇒ 试一次；
    * 其余（m4a / amr / aac / flac / 无扩展名）⇒ **不行** ——
      直接发只会换来 850019 / 静默降级，不如一开始就按文件发（至少看得见）。
    """
    try:
        path = getattr(media_element, "file", None) or getattr(media_element, "record", None)
    except Exception:
        path = None
    return _record_ext(str(path or _guess_name(media_element))) in _DOC_AUDIO_EXT


def _guess_name(element: Any) -> str:
    g = getattr(element, "guess_name", None)
    if callable(g):
        try:
            return str(g() or "")
        except Exception:
            pass
    return str(getattr(element, "file", "") or "")


# --------------------------------------------------------------------------- #
# 图片格式规范化（★ 2026-10-09：GIF 被平台拒，实测 850019）
# --------------------------------------------------------------------------- #
#: 官方「文件类型与限制」表里 `file_type=1 图片` 只列 **png / jpg**；
#: 概览页虽然写"支持 jpg/png/gif/webp/bmp"，但**实测 GIF 直传会被拒**：
#:     400 {'code': 850019, 'message': '富媒体文件格式不支持'}
#: ⇒ 上传前把平台不认的格式（gif/webp/bmp/tiff/…）**转成 PNG** 再传。
_QQ_OK_FORMATS = ("png", "jpeg")

_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)


def sniff_image_format(data: bytes) -> str:
    """按**字节魔数**判断图片格式（认不出返回空串）。"""
    head = data[:16] if data else b""
    for magic, fmt in _MAGIC:
        if head.startswith(magic):
            return fmt
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def normalize_image_data(data: bytes, name: str = "", logger_: Any = None, *,
                         allow_anim: bool = True,
                         anim_max_bytes: int = 4 * 1024 * 1024):
    """把平台不认的图片格式转成 **PNG**。返回 ``(data, name, note)``。

    * 已经是 png/jpeg ⇒ **原样返回**（零开销，绝不动用户内容）；
    * **动图（GIF 多帧）** ⇒ 先试 **APNG**（动图版 PNG：魔数还是 `\x89PNG`，
      平台按 png 收 ✓，客户端支持就能继续动）；APNG 太大（> `anim_max_bytes`）或失败
      ⇒ 退回**静态 PNG（第一帧）**；
    * 其它（webp/bmp/…） ⇒ 静态 PNG。

    ⚠ 全过程都在 `asyncio.to_thread` 里跑（调用方负责），**不阻塞事件循环**；
      失败一律**原样返回**，上层照旧会失败但在日志里说清楚。
    """
    fmt = sniff_image_format(data)
    if fmt in _QQ_OK_FORMATS:
        return data, name, ""
    if not data:
        return data, name, ""
    base = (os.path.splitext(name or "image")[0] or "image") + ".png"
    try:
        import io

        from PIL import Image as _PILImage

        with _PILImage.open(io.BytesIO(data)) as im:
            frames = int(getattr(im, "n_frames", 1) or 1)
            duration = 0
            try:
                duration = int((im.info or {}).get("duration") or 0)
            except Exception:
                duration = 0
            if frames > 1 and allow_anim:
                # ★ 超大动图**先别试** APNG（2026-10-09 实测补）：
                #   Pillow 会实打实把每一帧转成 RGBA 再打包 —— 60 帧 720×720
                #   要白等 22 秒，最后还因超过体积上限被**整个丢掉**。
                #   先按"帧数×像素"估个量，超了直接走静态，别浪费这 20 秒。
                _pixels = frames * int(getattr(im, "width", 0) or 0) \
                    * int(getattr(im, "height", 0) or 0)
                if _pixels > _APNG_SKIP_PIXELS:
                    if logger_ is not None:
                        logger_.info(
                            "[QQBOT-BRIDGE] 动图较大（%s，%d 帧、%.1f 万像素帧）——"
                            "跳过 APNG 直接转静态 PNG（避免几十秒的无效转换）",
                            fmt, frames, _pixels / 10000.0,
                        )
                else:
                    try:
                        im.seek(0)
                        ims = []
                        for idx in range(frames):
                            im.seek(idx)
                            ims.append(im.convert("RGBA").copy())
                        buf = io.BytesIO()
                        ims[0].save(buf, "PNG", save_all=True, append_images=ims[1:],
                                    duration=duration or 100, loop=0, optimize=True)
                        apng = buf.getvalue()
                        if 0 < len(apng) <= anim_max_bytes:
                            if logger_ is not None:
                                logger_.info(
                                    "[QQBOT-BRIDGE] 动图已转成 APNG（%s，%d 帧，%d 字节 → %d 字节）"
                                    "—— 魔数仍是 PNG，平台按图片收；客户端支持就继续动",
                                    fmt, frames, len(data), len(apng),
                                )
                            return apng, base, f"{fmt} → apng（{frames} 帧保动画）"
                        if logger_ is not None:
                            # ★ 这条原来**完全静默** —— 线上"图只有一帧"的现场，
                            #   很可能就是走到了这里（体积换不回来，只能静态）。
                            logger_.info(
                                "[QQBOT-BRIDGE] 动图转出的 APNG 超过体积上限"
                                "（%.1f MB > %.1f MB）—— 退回静态 PNG（只发第一帧）",
                                len(apng) / 1048576.0, anim_max_bytes / 1048576.0,
                            )
                    except Exception as exc:
                        if logger_ is not None:
                            logger_.info(
                                "[QQBOT-BRIDGE] 动图转 APNG 失败（%s）—— 退回静态 PNG（只发第一帧）",
                                str(exc)[:100],
                            )
            im.seek(0)
            still = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im
            buf2 = io.BytesIO()
            still.save(buf2, "PNG", optimize=True)
            out = buf2.getvalue()
        note = f"{fmt or '未知'} → png" + ("（动图只发第一帧）" if frames > 1 else "")
        if logger_ is not None:
            logger_.info(
                "[QQBOT-BRIDGE] 图片格式已规范化：%s（%s，%d 字节 → %d 字节）"
                "—— 平台的上传接口只接受 png/jpg，GIF/WEBP 直传会被拒（850019）",
                note, os.path.basename(name or "?"), len(data), len(out),
            )
        return out, base, note
    except Exception as exc:
        if logger_ is not None:
            logger_.warning(
                "[QQBOT-BRIDGE] 图片格式 %s 平台可能不收，但转 PNG 失败（%s: %s）—— 按原样上传",
                fmt or "未知", type(exc).__name__, str(exc)[:100],
            )
        return data, name, ""


#: 平台"文件格式不支持"类错误（图片被拒时可据此改成按文件发）
_FORMAT_ERROR_CODES = ("850019", "850031")

#: 格式类错误的**文字特征**（★★★ 2026-10-09 实锤的坑）：
#: qq-botpy 抛出的 `ServerError` 里**只有 message（"富媒体文件格式不支持"），
#: 没有错误码**（码只出现在 botpy 自己的日志行里）⇒ 只按码匹配的话，
#: `is_format_error` 对真实现场**永远返回 False**：
#:   * md 候选链不降级（原图被拒 ⇒ 直接放弃，而不是换 APNG/PNG）；
#:   * 安全网的"原图被拒 ⇒ 转档"分支进不去；
#:   * 重试逻辑把格式拒收当网络故障反复重试。
#: 所以文字特征必须一起匹配。
_FORMAT_ERROR_TEXT = ("格式不支持", "format not support", "unsupported format")

#: 动图转 APNG 前的体量预判上限（帧数 × 单帧像素）：
#: 超过就不试 APNG 了（试也大概率超体积上限，还白等几十秒）。
#: 3000 万 ≈ 60 帧 720×720 / 120 帧 500×500。
_APNG_SKIP_PIXELS = 30_000_000

#: 「原图被平台拒」记忆（md5 -> 时间戳）：10 分钟内不再对同一张图试原图直传，
#: 免得每次都白撞一次 850019。有界（最多 64 条）。
_RAW_IMG_REJECTED: dict = {}
_RAW_IMG_REJECT_TTL = 600.0


def _animated_format(data: bytes) -> str:
    """多帧动图返回 ``"gif"`` / ``"webp"``，否则空串。

    只认这两种"有动画意义"的格式 —— 它们值得"原图优先"（保动画）；
    静图/其它格式直接走转档，没必要多撞一次。
    """
    fmt = sniff_image_format(data)
    if fmt not in ("gif", "webp"):
        return ""
    try:
        import io

        from PIL import Image as _PILImage

        with _PILImage.open(io.BytesIO(data)) as im:
            return fmt if int(getattr(im, "n_frames", 1) or 1) > 1 else ""
    except Exception:
        return ""


#: 图片格式 → 上传用文件名的扩展名
_IMG_EXT = {"gif": ".gif", "webp": ".webp", "png": ".png", "jpeg": ".jpg", "bmp": ".bmp"}


def _image_upload_name(data: bytes, name: Any) -> str:
    """给图片上传挑一个带扩展名的文件名（★★★ 2026-10-10 对齐"原生成功路径"）。

    依据（用户实测对照）：**KiraAI 原生路径（不带本插件）发 GIF 成功** ——
    它走 `<file type="image">`，元素自带真实文件名（如 `test.gif`），上传体里
    **必然带 `file_name`**。而本插件的贴纸路径来自 base64、元素没有名字，
    上传体里**没有 `file_name`** —— 现场 GIF 被拒（850019）、原生路径却成功。
    ⇒ 与原生路径精确对齐：图片上传一律带一个"体面文件名"：
      * 元素自带名字且有扩展名 ⇒ 原样用；
      * 否则按**字节魔数**补扩展名（sticker 的 base64 走这里）。
    """
    n = ""
    try:
        if name:
            n = os.path.basename(str(name).split("?")[0])
    except Exception:
        n = ""
    if n and os.path.splitext(n)[1]:
        return n
    stem = os.path.splitext(n)[0] if n else "image"
    fmt = sniff_image_format(data)
    return stem + _IMG_EXT.get(fmt, ".png")


def is_format_error(exc: BaseException) -> bool:
    text = str(exc)
    if any(code in text for code in _FORMAT_ERROR_CODES):
        return True
    low = text.lower()
    return any(marker in text or marker in low for marker in _FORMAT_ERROR_TEXT)


#: 只提示一次：模型把音频用 `<file>`（而非 `<file type="record">`）发出来
_AUDIO_AS_FILE_LOGGED = False

#: 只提示一次：每种 file_type 首次上传的"字节头部"自检
_HEAD_LOGGED: set = set()

#: 只提示一次：每种 file_type 实际发出的上传体形状（排查"平台为什么不认"的关键信息）
_SHAPE_LOGGED: set = set()

#: 官方上传/发送错误码 → 人话（来源：官方「单聊/群聊富媒体上传」错误码表）
_UPLOAD_HINT = {
    "850018": "群被禁言或机器人被禁言 —— 解禁后再发",
    "850019": "平台不支持这个文件格式（语音只认 silk，图片只认 png/jpg）",
    "850026": "平台下载不到这个 URL（转存失败），换一个可直接访问的地址",
    "850027": "平台发送数据超时 —— 稍后重试即可",
    "850031": "文件超过平台大小限制",
    "304080": "文件信息无效（file_info 已过期或损坏 —— 重新上传即可）",
    "40093001": "上传通道（BDH）异常 —— 重试即可",
    "40093002": "★ **今天的分片上传日额度已用完** —— 只能等明天；"
                "但小文件（<5MB、走 base64 直传）不受此额度限制",
}


def humanize_upload_error(exc: Any) -> str:
    """把平台的上传/发送错误翻译成"用户看了知道该做什么"的一句话。

    为什么要做：官方错误码表里 `40093002`（日额度）与 `850019`（格式不支持）
    这两类**根本不是 bug**，但日志里只有一串英文/数字，
    用户只会看到"图/语音又发不出去"，然后来问为什么。
    """
    text = str(exc)
    for code, hint in _UPLOAD_HINT.items():
        if code in text:
            return f"[{code}] {hint}"
    return ""


def _log_upload_shape_once(file_type: int, payload: dict, logger_: Any) -> None:
    """把**实际发出的上传体形状**记一条 INFO（去掉 file_data，只留字段名与大小）。

    为什么值得单独记（2026-10-08）：排查"平台为什么把它当文件"时，
    **"我们到底发了什么"是唯一能对齐官方实现的东西**，而日志里原来完全没有 ——
    只能靠猜（已经因此浪费过好几轮）。
    """
    if logger_ is None or file_type in _SHAPE_LOGGED:
        return
    _SHAPE_LOGGED.add(file_type)
    fields = {k: (f"<base64 {len(v) // 1024}KB>" if k == "file_data" else v)
              for k, v in payload.items() if k not in ("group_openid", "openid")}
    logger_.info(
        "[QQBOT-BRIDGE] 媒体上传体形状（file_type=%s）：%s —— "
        "官方口径：file_name 只对 file_type=4 发（腾讯 Node SDK / openclaw-qqbot / Hermes 三家一致）",
        file_type, fields,
    )


def _maybe_log_audio_as_file(media_element: Any, logger_: Any) -> None:
    """音频被**当普通文件**发（`<file>` 而不是 `<file type="record">`）时提示一次。

    两种写法都是合法的（框架 `FileTag` 文档写得很清楚：`type=record` ⇒ 语音条，
    `type=file` ⇒ 音频文件），但**日志里必须能分辨** ——
    否则用户说"语音条发不出来"时，我们连"是不是模型就没按语音发"都判断不了
    （2026-10-08 排查卡了整整两轮，就因为缺这一行）。
    """
    global _AUDIO_AS_FILE_LOGGED
    if _AUDIO_AS_FILE_LOGGED or logger_ is None:
        return
    try:
        if type(media_element).__name__ != "File":
            return
        ext = _record_ext(_guess_name(media_element))
        if ext not in _RECORD_EXT:
            return
        _AUDIO_AS_FILE_LOGGED = True
        logger_.info(
            "[QQBOT-BRIDGE] 本条音频是按「文件」类型发出的（%s）—— 模型写的是 "
            "<file>（type 缺省=file），不是 <file type=\"record\">；"
            "所以它在 QQ 里是文件卡片而不是语音条。想发语音条请让模型用 type=\"record\"",
            ext,
        )
    except Exception:
        pass


def _probe_recently_rejected(data: bytes) -> bool:
    """这张图（md5）10 分钟内被平台以格式为由拒过？"""
    try:
        import hashlib
        import time

        key = hashlib.md5(data).hexdigest()
    except Exception:
        return False
    ts = _RAW_IMG_REJECTED.get(key, 0.0)
    return (time.time() - ts) < _RAW_IMG_REJECT_TTL


def _probe_mark(data: bytes, rejected: bool) -> None:
    """记录「原图直传」结果（拒=记时间戳；成功=清记录）。有界。"""
    try:
        import hashlib
        import time

        key = hashlib.md5(data).hexdigest()
    except Exception:
        return
    if rejected:
        _RAW_IMG_REJECTED[key] = time.time()
        if len(_RAW_IMG_REJECTED) > 64:
            _old = sorted(_RAW_IMG_REJECTED, key=lambda x: _RAW_IMG_REJECTED[x])[:16]
            for k in _old:
                _RAW_IMG_REJECTED.pop(k, None)
    else:
        _RAW_IMG_REJECTED.pop(key, None)


async def _try_send_original(api: Any, target_id: str, is_group: bool,
                             data: bytes, elem_name: Any, logger: Any):
    """元素层「原图优先」探测：把动图（gif/webp）**原样**按 file_type=1 试传一次。

    背景（2026-10-09 用户情报）：QQ 官方 bot 的图片格式**已支持 gif/webp**，
    所以表情包/动图应当**原样直传**以保住动画；被平台以格式为由拒（850019）
    才退转档（APNG→PNG）。被拒过的图记 10 分钟，避免每次发送都白撞。

    返回上传结果；返回 None = "没通过，走转档路径"（被拒/网络错误/被记忆跳过）。

    ★ 走**安全网内层**（如果挂了）：探测意图就是"原样发"，不需要安全网再加工
      （否则安全网会把原图转档，元素层会误以为"原图直传成功"——日志和缓存全乱）。
    """
    if api is None or not data:
        return None
    if _probe_recently_rejected(data):
        if logger is not None:
            logger.info(
                "[QQBOT-BRIDGE] 这张动图 10 分钟内被平台拒过原图 ⇒ 跳过原图直传，直接转档")
        return None
    try:
        from botpy.http import Route
    except Exception:
        return None
    import base64 as _b

    payload = {
        "file_type": FT_IMAGE,
        "file_data": _b.b64encode(data).decode("ascii"),
        "srv_send_msg": False,
        # ★ 与原生路径一致：图片带文件名（无名字时按魔数补扩展名）
        "file_name": _image_upload_name(data, elem_name),
    }
    if is_group:
        payload["group_openid"] = target_id
        route = Route("POST", "/v2/groups/{group_openid}/files",
                      group_openid=target_id)
    else:
        payload["openid"] = target_id
        route = Route("POST", "/v2/users/{openid}/files", openid=target_id)
    _log_upload_shape_once(FT_IMAGE, payload, logger)
    http = getattr(api, "_http", None)
    sender = getattr(http, "request", None)
    inner = getattr(sender, "_kira_bridge_guard_orig", None)
    if callable(inner):
        sender = inner                    # 绕过安全网：探测=原样发
    if not callable(sender):
        return None
    try:
        result = await sender(route, json=payload)
    except Exception as exc:
        if is_format_error(exc):
            _probe_mark(data, True)
            if logger is not None:
                logger.info(
                    "[QQBOT-BRIDGE] 动图**原图直传被平台拒**（%s）⇒ 转成 APNG/PNG 保显示；"
                    "同一张图 10 分钟内不再试原图",
                    str(exc)[:80])
        else:
            if logger is not None:
                logger.warning(
                    "[QQBOT-BRIDGE] 动图原图直传失败（非格式错误：%s: %s）⇒ 继续走转档路径",
                    type(exc).__name__, str(exc)[:120])
        return None
    _probe_mark(data, False)
    if logger is not None:
        fmt = _animated_format(data) or "动图"
        logger.info(
            "[QQBOT-BRIDGE] ★ 动图**原图直传成功**（%s，保动画）—— 平台现在收 gif/webp；"
            "若客户端里仍显示不动，把 gif_sticker_mode 设为 image 可回退（转静态）",
            fmt)
        _fi = result.get("file_info") if isinstance(result, dict) \
            else getattr(result, "file_info", None)
        logger.info(
            "[QQBOT-BRIDGE] 媒体上传完成：file_type=1 文件=%s → %s",
            os.path.basename(str(elem_name or "?")),
            "拿到 file_info" if _fi else f"响应异常 {str(result)[:120]}")
    return result


def classify(element: Any) -> Optional[int]:
    """按元素类型 + 扩展名给出官方 `file_type`。

    返回 None 表示「交回核心原逻辑」（例如 `File`/`Sticker`，核心按 4 处理是对的）。

    ★ 也认 `media_coerce` 留下的 `_kira_bridge_orig_kind` 标记：
    为了让核心的 `media_elements` 白名单认得 ` Record`/`Video`，
    我们会把它们**临时换成 `File`** 再发；换的时候把原始类型记在标记里，
    这里按**原始类型**给值，保证 `视频→2 / 语音→3` 不会因为"变成 File"而丢。
    """
    # ★ 先看"换壳"标记（见 media_coerce）—— 标记里存的是**原始类名**，
    #   第三方表情包插件的元素类名可能是 `StickerPlus` 之类，统一按名字判类型。
    orig = getattr(element, "_kira_bridge_orig_kind", None)
    if orig:
        low = str(orig).lower()
        if "record" in low:
            return FT_RECORD
        if "video" in low:
            return FT_VIDEO
        # 其余"换壳"过来的（Sticker / StickerPlus / …）：就是一张图
        # ⇒ file_type=1，QQ 直接当图片展示
        return FT_IMAGE

    name = type(element).__name__
    if name == "Video":
        return FT_VIDEO
    if name == "Record":
        return FT_RECORD
    if name in ("Image", "File", "Sticker"):
        return None
    # 兜底：按扩展名（有些插件用自定义元素类）
    ext = os.path.splitext(_guess_name(element).split("?")[0])[1].lower()
    if ext in _VIDEO_EXT:
        return FT_VIDEO
    if ext in _RECORD_EXT:
        return FT_RECORD
    return None


async def _retry_as_silk(api, target_id, media_element, is_group, exc, logger_):
    """原样发被拒 ⇒ 转成 silk 再上传一次。返回结果或 None（转不了就交回）。

    ⚠ **全程不阻塞**：转码本身在 `to_silk_if_needed` 里走 `asyncio.to_thread`，
      读文件同样 `to_thread` —— 事件循环不会卡。
    """
    try:
        from audio_silk import convert_to_silk_forced
        src = await media_element.to_path()
        silk = await convert_to_silk_forced(src, logger_= logger_)
        if not silk:
            if logger_ is not None:
                logger_.warning(
                    "[QQBOT-BRIDGE] 该音频被平台拒了，且无法转成 silk"
                    "（缺 pilk/ffmpeg 或解码失败）—— 本条按「文件」发送",
                )
            return None
        data = await asyncio.to_thread(Path(silk).read_bytes)
        # ⚠ 同样**不带 file_name**（见 `_upload` 里那段依据）：语音带文件名会被当文件渲染。
        payload = {
            "file_type": FT_RECORD,
            "file_data": base64.b64encode(data).decode("ascii"),
            "srv_send_msg": False,
        }
        if is_group:
            payload["group_openid"] = target_id
            route = Route("POST", "/v2/groups/{group_openid}/files",
                          group_openid=target_id)
        else:
            payload["openid"] = target_id
            route = Route("POST", "/v2/users/{openid}/files", openid=target_id)
        if logger_ is not None:
            logger_.info(
                "[QQBOT-BRIDGE] 已把音频转成 silk 并重试上传（file_type=3，文件=%s）",
                os.path.basename(silk),
            )
        return await api._http.request(route, json=payload)
    except Exception as exc2:
        if logger_ is not None:
            logger_.warning("[QQBOT-BRIDGE] 转 silk 重试也失败：%s", exc2)
        return None


# --------------------------------------------------------------------------- #
# ★★★ HTTP 层「媒体安全网」（最后一道防线，2026-10-09 新增）
# --------------------------------------------------------------------------- #
#
# ## 为什么还要这一层
#
# 正规修法有两层：元素层（media_coerce 换壳）+ 上传层（包 `_upload_file`）。
# 但 2026-10-09 线上出现了一种现象：元素层明明在跑（贴纸被换成了图片），
# 上传层却像**从来没装上过** ——
#   * GIF 贴纸被原样直传 ⇒ 平台拒收 850019（本该先转 APNG/PNG）；
#   * 语音走 file_type=4 ⇒ 文件卡片（本该是 3）；
# 而日志里**一条媒体处理记录都没有**（连"发送媒体：…"这条每条都该有的 INFO 也没有）。
# 安装失败的原因受现场限制没查死。
#
# ⇒ 这一层直接包 **botpy 的 HTTP 出口**（`client.api._http.request`）：
# 只要请求体里带 `file_data` 且目标是 `/files` 上传接口，就在**发出去之前**
# 做最后一轮体检 —— 它不依赖上面任何一层是否装上：
#
#   * file_type=1 但不是 png/jpeg（GIF/WebP/BMP…）⇒ 先规范化（APNG/PNG）再传；
#     若仍被"格式不支持"拒收 ⇒ 用**原始字节**改按 file_type=4 再发一次（保投递）；
#   * file_type=3 但内容不是 silk ⇒ 就地转 silk 再传（有编码器时）；
#   * file_type=4 且内容是音频 ⇒ 提示一次（多半是上层 file_type 修正没生效）。
#
# 幂等、可还原；热重载时**接管**旧实例留下的网（用新配置的闭包重新包）。

#: 安全网提示只打一次的键
_HTTP_GUARD_LOGGED: set = set()

#: ★ 发送路径与安全网之间的「这条消息含语音条（<record>）」信号（2026-10-09）。
#:
#: 用途：线上出现过"媒体包装没装上"的最坏情况 —— 此时 `<record>` 语音会被
#: 核心按 file_type=4 发成文件卡片，而安全网单看请求体**分不清**"用户故意发文件"
#: 和"本该是语音条"。发送路径（换壳处，实测一定会跑到）在调用前把这个
#: contextvar 置 True，安全网看到「file_type=4 + 音频内容 + 本信号」就知道
#: 这条本该是语音条 ⇒ 就地转 silk 并按 file_type=3 重发（救援）。
#: asyncio 任务内 contextvar 自动隔离，跨会话并发互不影响。
PENDING_RECORD = contextvars.ContextVar("qqbot_bridge_pending_record", default=False)

#: silk 魔数（与 audio_silk.SILK_MAGIC 同一事实；本模块内自用一份，避免循环依赖）
_SILK_MAGIC = b"#!SILK_V3"

#: 音频走文件通道的提示（只打一次）
_HTTP_AUDIO_FILE_HINTED = False


def _b64decode_local(s: str) -> bytes:
    import base64 as _b

    return _b.b64decode(s or "")


def _sniff_audio_format(data: bytes) -> str:
    """按魔数粗判音频格式（只用于诊断/决定"要不要转 silk"）。"""
    head = data[:16] if data else b""
    if head[:1] == b"\x02" and head[1:10] == _SILK_MAGIC:
        return "silk(tencent)"
    if head[:9] == _SILK_MAGIC:
        return "silk"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"#!AMR" or head[:6] == b"#!AMR\n":
        return "amr"
    if head[4:8] == b"ftyp":
        return "m4a/mp4"
    if head[:3] == b"ID3" or (head[:1] == b"\xff" and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[:4] == b"fLaC":
        return "flac"
    return ""


def _is_silk_bytes(data: bytes) -> bool:
    head = data[:16] if data else b""
    return (head[:1] == b"\x02" and head[1:10] == _SILK_MAGIC) or head[:9] == _SILK_MAGIC


#: 「媒体层安装失败」提示去重（每个原因打一次 WARNING）
_INSTALL_FAIL_LOGGED: set = set()

#: ★★★ 媒体包装的「世代戳」——每次插件升级都应修改它。
#: 用途（2026-10-09 实锤）：旧版本装上的包装没有世代信息，而 install() 原来
#: 看到 `_kira_bridge_ftype` 标记就当作"已安装"⇒ **每次更新都被短路**，
#: 包装永远停在旧代码（没有日志、行为过时）——即"僵尸包装"。
#: 有了世代戳，install() 就能识别"标记在、但世代不符"并自动接管换新。
_WRAPPER_BUILD = "1.6.10"


def installed_current(holder: Any) -> bool:
    """这个 holder 上的媒体包装是不是**本世代**的？（给自愈层做快速检查用）"""
    cur = getattr(holder, "_upload_file", None)
    return bool(getattr(cur, "_kira_bridge_ftype", False)
                and getattr(cur, "_kira_bridge_build", "") == _WRAPPER_BUILD)


def _install_fail(reason: str, logger: Any) -> None:
    """媒体层安装失败 ⇒ **响亮**记录（每个原因只打一次）。

    2026-10-09 线上教训：原来所有失败路径都静默 return False —— 用户明明遇到了
    "GIF 直传被拒 / 语音变文件卡片"，日志里却**一条线索都没有**。
    安装不上的原因必须能被看见。
    """
    if reason in _INSTALL_FAIL_LOGGED or logger is None:
        return
    _INSTALL_FAIL_LOGGED.add(reason)
    try:
        logger.warning(
            "[QQBOT-BRIDGE] ⚠ 媒体层（file_type 修正）**没有装上**：%s。"
            "影响：GIF/动图会被平台拒收（850019）、语音/视频会退化成文件卡片。"
            "HTTP 层安全网（另一路）若在，仍会兜住图片与语音；请把这条日志反馈",
            reason,
        )
    except Exception:
        pass


def install_http_guard(client: Any, logger: Any = None, plugin: Any = None) -> bool:
    """挂「媒体安全网」到 `client.api._http.request`。幂等、可还原。

    返回是否已就位。任何异常都不向外抛（安全网本身绝不影响正常请求）。
    """
    try:
        api = getattr(client, "api", None)
        http = getattr(api, "_http", None)
        current = getattr(http, "request", None)
        if not callable(current):
            return False
    except Exception:
        return False
    try:
        from botpy.http import Route  # noqa: F401
    except Exception:
        return False

    if getattr(current, "_kira_bridge_guard", False):
        # 已有网（可能是热重载残留的旧实例）⇒ **接管**：包住它的原始实现，
        # 这样新配置（gif_sticker_mode 等）立即生效，不会一直用旧闭包。
        # 同一个插件实例重复调用（每 15s 巡检）⇒ 视作已就位，不重复包。
        if getattr(current, "_kira_bridge_guard_plugin", None) is plugin:
            return True
        orig = getattr(current, "_kira_bridge_guard_orig", None) or current
    else:
        orig = current

    async def _guarded(route, *args, _orig=orig, **kwargs):
        """安全网入口。

        ⚠⚠ 结构契约（2026-10-09 血的教训）：**真实请求的异常绝不能被吞** ——
        吞掉后再次调用 `_orig` 等于「每条失败请求都静默重发一次」
        （audit_sticker [5e-3] 当场抓到：图片被拒 1 次被记成 2 次）。
        这里用 `tried` 标记区分两种情况：
          * 请求**还没发过**就出错（安全网自身逻辑 bug）⇒ 静默放行原请求；
          * 请求**已经发过**再出错（真实的网络/平台异常）⇒ 原样上抛。
        """
        body = kwargs.get("json")
        if not (isinstance(body, dict) and isinstance(body.get("file_data"), str)
                and "file_type" in body
                and str(getattr(route, "path", "") or "").endswith("/files")):
            return await _orig(route, *args, **kwargs)
        state = {"tried": False}

        async def _tracked(*a, **k):
            state["tried"] = True
            return await _orig(*a, **k)

        try:
            return await _guard_upload(route, args, body, kwargs,
                                       _tracked, logger, plugin)
        except Exception:
            if state["tried"]:
                raise          # 请求已发过：这是真实异常，原样上抛（绝不重发）
            if logger is not None:
                logger.debug("[QQBOT-BRIDGE] 媒体安全网自身出错（放行原请求）")
            return await _orig(route, *args, **kwargs)

    setattr(_guarded, "_kira_bridge_guard", True)
    setattr(_guarded, "_kira_bridge_guard_orig", orig)
    setattr(_guarded, "_kira_bridge_guard_plugin", plugin)
    try:
        http.request = _guarded
    except Exception:
        return False
    return True


def restore_http_guard(client: Any) -> bool:
    """还原 `client.api._http.request`（幂等）。"""
    try:
        http = getattr(getattr(client, "api", None), "_http", None)
        current = getattr(http, "request", None)
        orig = getattr(current, "_kira_bridge_guard_orig", None)
        if callable(orig):
            http.request = orig
            return True
    except Exception:
        pass
    return False


async def _guard_upload(route, args, body, kwargs, orig, logger, plugin):
    """安全网的实际处理：图片规范化 / 语音转 silk / 兜底按文件重发。"""
    try:
        data = _b64decode_local(body.get("file_data") or "")
    except Exception:
        return await orig(route, *args, **kwargs)
    file_type = body.get("file_type")

    def _once(key: str, level: str, text: str, *fmt_args) -> None:
        if key in _HTTP_GUARD_LOGGED or logger is None:
            return
        _HTTP_GUARD_LOGGED.add(key)
        try:
            getattr(logger, level)("[QQBOT-BRIDGE] " + text, *fmt_args)
        except Exception:
            pass

    # ---- 图片：不是 png/jpg ⇒ 原图优先，被拒再规范化（GIF/WEBP/BMP…）----
    if file_type == FT_IMAGE:
        fmt = sniff_image_format(data)
        if fmt and fmt not in _QQ_OK_FORMATS:
            mode = str(getattr(plugin, "gif_sticker_mode", "auto") or "auto").lower()
            # ★ 原图优先（2026-10-09：平台图片格式已支持 gif/webp）：
            #   先**原样发一次**（保动画）；被格式拒再转档（APNG/PNG）；
            #   再被拒按文件发。被拒过的图 10 分钟内跳过此步（与元素层共用记忆）。
            #   ★★ 2026-10-10：与"原生成功路径"对齐 —— 原样发时**带文件名**
            #   （无名字按魔数补扩展名；原生路径的图片上传永远带 file_name）。
            if fmt in ("gif", "webp") and not _probe_recently_rejected(data):
                body_raw = dict(body)
                if not body_raw.get("file_name"):
                    body_raw["file_name"] = _image_upload_name(data, "")
                kwargs_raw = dict(kwargs)
                kwargs_raw["json"] = body_raw
                try:
                    result = await orig(route, *args, **kwargs_raw)
                    _probe_mark(data, False)
                    _once("img_raw_ok", "info",
                          "媒体安全网：%s 原样直传成功（带文件名，与原生路径一致）—— 保动画", fmt)
                    return result
                except Exception as exc:
                    if not is_format_error(exc):
                        raise
                    _probe_mark(data, True)
                    _once("img_raw_rej", "info",
                          "媒体安全网：%s 原样直传被平台拒（%s）—— 转档后再试",
                          fmt, str(exc)[:60])
            try:
                new_data, new_name, _note = await asyncio.to_thread(
                    normalize_image_data, data, "image." + (fmt or "bin"), logger,
                    allow_anim=(mode == "auto"))
            except Exception:
                new_data, new_name = data, ""
            if new_data is not data:
                import base64 as _b

                body2 = dict(body)
                body2["file_data"] = _b.b64encode(new_data).decode("ascii")
                if not body2.get("file_name"):
                    body2["file_name"] = _image_upload_name(new_data, "")
                kwargs2 = dict(kwargs)
                kwargs2["json"] = body2
                try:
                    result = await orig(route, *args, **kwargs2)
                    _once("img_ok", "info",
                          "媒体安全网：这张 %s 图在发出前被规范化为 PNG（APNG）——"
                          "平台只收 png/jpg，直传会被 850019 拒", fmt)
                    return result
                except Exception as exc:
                    if not is_format_error(exc):
                        raise
                    # 还是被拒（保险中的保险）⇒ 原始字节按「文件」发，保投递
                    fb = dict(body)
                    fb["file_type"] = FT_FILE
                    fb["file_data"] = body.get("file_data")
                    if not fb.get("file_name"):
                        fb["file_name"] = _image_upload_name(data, "") or "sticker.bin"
                    _once("img_fb", "warning",
                          "媒体安全网：图片规范化后仍被平台拒收（%s）——"
                          "已用**原始动图**改按文件发送（动图下载后仍是动的）",
                          str(exc)[:80])
                    kwargs3 = dict(kwargs)
                    kwargs3["json"] = fb
                    return await orig(route, *args, **kwargs3)

    # ---- 语音：file_type=3 但内容不是 silk ⇒ 就地转 silk ----
    if file_type == FT_RECORD:
        if data and not _is_silk_bytes(data):
            fmt = _sniff_audio_format(data)
            try:
                from audio_silk import convert_to_silk_forced
                import tempfile
                import os as _os

                def _write_tmp() -> str:
                    fd, p = tempfile.mkstemp(prefix="qqbot_guard_", suffix=".bin")
                    with _os.fdopen(fd, "wb") as f:
                        f.write(data)
                    return p

                tmp = await asyncio.to_thread(_write_tmp)
                silk = await convert_to_silk_forced(tmp, logger_=logger)
                if silk:
                    with open(silk, "rb") as f:
                        silk_data = await asyncio.to_thread(f.read)
                    import base64 as _b

                    body2 = dict(body)
                    body2["file_data"] = _b.b64encode(silk_data).decode("ascii")
                    kwargs2 = dict(kwargs)
                    kwargs2["json"] = body2
                    result = await orig(route, *args, **kwargs2)
                    _once("voice_ok", "info",
                          "媒体安全网：这段音频（%s）在发出前被转成了 silk ——"
                          "语音条（file_type=3）必须是腾讯系 silk，其余格式会降级成文件卡片",
                          fmt or "未知格式")
                    return result
                _once("voice_fail", "warning",
                      "媒体安全网：音频（%s）不是 silk 且转码失败（缺 silk 编码器/ffmpeg？）——"
                      "本条大概率会以**文件卡片**发出。修复：pip install silk-python imageio-ffmpeg",
                      fmt or "未知格式")
            except Exception as exc:
                _once("voice_exc", "warning",
                      "媒体安全网：音频转 silk 出错（%s）—— 原样发送", str(exc)[:120])

    # ---- 语音救援：核心把它当「文件」发了，但发送路径标了"这条含 <record>" ----
    if file_type == FT_FILE and data:
        global _HTTP_AUDIO_FILE_HINTED
        fmt = _sniff_audio_format(data)
        if fmt and PENDING_RECORD.get(False):
            try:
                if fmt.startswith("silk"):
                    body2 = dict(body)
                    body2["file_type"] = FT_RECORD
                    body2.pop("file_name", None)
                    kwargs2 = dict(kwargs)
                    kwargs2["json"] = body2
                    result = await orig(route, *args, **kwargs2)
                    _once("voice_rescue_silk", "info",
                          "媒体安全网：本条是 <record> 语音（内容已是 silk）却走了文件通道 ——"
                          "已改按语音条发送（file_type=3）")
                    return result
                from audio_silk import convert_to_silk_forced
                import tempfile
                import os as _os

                def _write_tmp2() -> str:
                    fd, p = tempfile.mkstemp(prefix="qqbot_guard_", suffix=".bin")
                    with _os.fdopen(fd, "wb") as f:
                        f.write(data)
                    return p

                tmp2 = await asyncio.to_thread(_write_tmp2)
                silk2 = await convert_to_silk_forced(tmp2, logger_=logger)
                if silk2:
                    with open(silk2, "rb") as f:
                        silk_data2 = await asyncio.to_thread(f.read)
                    import base64 as _b2

                    body2 = dict(body)
                    body2["file_type"] = FT_RECORD
                    body2["file_data"] = _b2.b64encode(silk_data2).decode("ascii")
                    body2.pop("file_name", None)
                    kwargs2 = dict(kwargs)
                    kwargs2["json"] = body2
                    result = await orig(route, *args, **kwargs2)
                    _once("voice_rescue", "info",
                          "媒体安全网：本条 <record> 语音走了文件通道（file_type=4，%s）——"
                          "已就地转 silk 并按语音条发送（file_type=3）；"
                          "若经常看到本行，请把启动日志里「媒体层」相关行一起反馈", fmt)
                    return result
                _once("voice_rescue_fail", "warning",
                      "媒体安全网：<record> 语音走了文件通道、且转 silk 失败（缺编码器/ffmpeg？）——"
                      "本条只能按文件发。修复：pip install silk-python imageio-ffmpeg")
            except Exception as exc:
                _once("voice_rescue_exc", "warning",
                      "媒体安全网：语音救援出错（%s）—— 原样发送", str(exc)[:120])
        if fmt and not _HTTP_AUDIO_FILE_HINTED:
            _HTTP_AUDIO_FILE_HINTED = True
            if logger is not None:
                logger.info(
                    "[QQBOT-BRIDGE] 提示：有一条**音频**在按「文件」通道上传（file_type=4，%s）——"
                    "本次没有触发「语音救援」（没有收到 <record> 信号，或救援转码失败）。"
                    "如果这条本该是语音条，请把本条前后的日志一起反馈 ——"
                    "没有信号时安全网不会把文件通道的音频改成语音（分辨不出「故意要发文件」的情况）",
                    fmt,
                )
    return await orig(route, *args, **kwargs)


def install(holder: Any, client: Any, logger: Any = None, plugin: Any = None) -> bool:
    """把 `holder._upload_file` 换成「类型更准」的版本。幂等。

    :param holder: 真正带 `_upload_file` 的对象
                   （3.0 = 能力对象；2.x = 适配器实例）
    :param client: botpy 客户端（拿 `api` / `_http`）

    ★ 2026-10-09：**每一条失败路径都要响亮**（原来全部静默 return False ——
      线上"媒体层像没装上"时日志里一点线索都没有）。失败原因按 key 去重，
      每个原因只打一次 WARNING。

    ★★★ 2026-10-09 当日晚间实锤的**「僵尸包装」**（一整天排查的最终根因）：
      旧版本（v1.5.0 起）装上的包装**没有世代标记**，而这里原来看到
      `_kira_bridge_ftype` 标记就 return True（"已安装"）——
      ⇒ 从此**每一次更新都被短路**，那层永远停在旧代码：
        没有逐条日志、不认识后来的换壳标记、GIF 原样传（850019）、
        语音永远按 file_type=4 发（文件卡片）。
      修法：给包装盖**世代戳**（`_WRAPPER_BUILD`）；标记存在但世代不符 ⇒
      剥到它最初的核函数、**重新包一层新的**（自动"接管"，留一条 INFO）。
    """
    if holder is None or client is None:
        _install_fail(f"holder/client 为空（holder={holder!r}）", logger)
        return False
    current = getattr(holder, "_upload_file", None)
    if not callable(current):
        _install_fail(
            f"holder（{type(holder).__name__}）上没有可调用的 `_upload_file` —— "
            f"能力对象可能没解析到（3.0 在能力对象上、2.x 在适配器实例上）",
            logger)
        return False
    _stale = False
    if getattr(current, "_kira_bridge_ftype", False):
        _old_build = getattr(current, "_kira_bridge_build", "")
        if _old_build == _WRAPPER_BUILD:
            return True
        # —— 僵尸包装：剥到核函数，重包新版 ——
        _stale = True
        _base = getattr(current, "_kira_bridge_orig", None)
        if callable(_base):
            current = _base
        if logger is not None:
            logger.info(
                "[QQBOT-BRIDGE] ★ 检测到**旧版本留下的媒体包装**（世代=%s，本版=%s）——"
                "它从装上那天起就再没被更新过（旧 install 的「已安装」短路缺陷），"
                "现场表现就是：GIF 直传被拒 / 语音按文件发 / 一条媒体日志都没有。"
                "已自动剥壳并用本版重新包装",
                _old_build or "无（v1.6.9 之前）",
                _WRAPPER_BUILD,
            )

    api = getattr(client, "api", None)
    if api is None:
        _install_fail("client.api 为空（botpy 客户端还没就绪？）", logger)
        return False
    try:
        from botpy.http import Route                      # noqa: F401
    except Exception as exc:
        _install_fail(f"import botpy.http 失败：{type(exc).__name__}: {exc}", logger)
        return False
    #: 运行时读配置（gif_sticker_mode 等），热改配置立即生效
    plugin = plugin

    async def _upload_file(target_id, media_element, is_group, _orig=current):
        want = classify(media_element)
        if want is None:
            _maybe_log_audio_as_file(media_element, logger)
            return await _orig(target_id, media_element, is_group)
        try:
            return await _upload(target_id, media_element, is_group, want)
        except Exception as exc:
            # ★★★ 这一条**必须是 WARNING**（原来在 debug，等于没有）：
            #   "媒体按语音上传失败 ⇒ 交回核心按文件发" 正是"语音变成文件卡片"的现场，
            #   而日志里看不到它时，用户只能看到一张文件卡片、查无可查（2026-10-09 踩到）。
            if logger is not None:
                try:
                    _hint = humanize_upload_error(exc)
                except Exception:
                    _hint = ""
                logger.warning(
                    "[QQBOT-BRIDGE] 按 file_type=%s 上传失败（%s: %s）%s"
                    " —— 已交回核心逻辑（多半会降级成**文件卡片**，不是语音条）。"
                    "请把这条连同上面的错误码一起反馈",
                    want, type(exc).__name__, str(exc)[:180],
                    ("\n    → " + _hint) if _hint else "",
                )
            return await _orig(target_id, media_element, is_group)

    async def _upload(target_id, media_element, is_group, file_type):
        # ★★★ 语音条（file_type=3）要求 **silk** 格式 —— 官方限制表只列 silk，
        #   第三方实测也确认「MP3 直接上传会降级成文件」。
        #   ⇒ 在真正上传前，把非 silk 的音频转成 silk。
        #   转不了（缺依赖 / 解码失败）就**退回 file_type=4 按文件发** ——
        #   宁可降级成文件，也绝不失败。
        silk_path = None
        #: 原样发的音频被平台拒时，是否值得转 silk 重试（见下面 FT_RECORD 分支）
        _pending_silk_retry = False
        _orig_kind = getattr(media_element, "_kira_bridge_orig_kind", None)
        _elem_name = _guess_name(media_element)
        _elem_size = None
        try:
            _p = await media_element.to_path()
            if _p and os.path.isfile(_p):
                _elem_size = os.path.getsize(_p)
        except Exception:
            pass
        # ★ 诊断：让用户（和排查的人）能一眼看到"到底按什么类型、发哪个文件"。
        #   之前线上出问题完全看不到这些，只能猜（2026-10-08 教训）。
        if logger is not None:
            logger.info(
                "[QQBOT-BRIDGE] 发送媒体：原始类型=%s 文件=%s 大小=%s 计划 file_type=%s",
                _orig_kind or type(media_element).__name__,
                _elem_name or "?", f"{_elem_size}B" if _elem_size else "?",
                file_type,
            )
        if file_type == FT_RECORD:
            got = await maybe_convert_to_silk(media_element, logger)
            if got == KEEP_AS_IS:
                pass                          # 本来就是（合法腾讯系）silk，全部不动
            elif got:
                silk_path = got               # 已转成 silk（ogg/m4a/改名 silk …）
            elif direct_upload_ok(media_element):
                # ★ 转不了（没装编码器 / 解码失败），但格式在官方文档的语音白名单里
                #   ⇒ 先直传试一次；被平台拒了下面会自动再试一次转码（见 _retry_as_silk）。
                _pending_silk_retry = True
                if logger is not None:
                    logger.warning(
                        "[QQBOT-BRIDGE] 该音频（%s）不是有效 silk，且**没有可用的 silk 编码器** "
                        "⇒ 只能原样直传，平台大概率把它降级成**文件卡片**（不是语音条）。"
                        "修复：pip install silk-python imageio-ffmpeg",
                        os.path.splitext(_elem_name or "")[1] or "?",
                    )
            else:
                # ★ 既转不了、又不在白名单里（m4a / amr / aac / flac / 无扩展名…）
                #   ⇒ **一开始就按「文件」发**。
                #
                #   踩过的坑（2026-10-08 用户实测）：m4a 直传 file_type=3 被平台拒，
                #   重试也转不了 ⇒ 最后**整个消息都没发出去**（用户："QQ 聊天里都看不到消息"）。
                #   宁可降级成文件卡片（用户看得见、可下载），也绝不发不出去。
                file_type = FT_FILE
                if logger is not None:
                    logger.warning(
                        "[QQBOT-BRIDGE] 该音频（%s）既不是 silk、也转不了 silk，"
                        "且不在官方语音白名单里 —— 本条按「文件」发送（至少能收到）；"
                        "装上 silk 依赖后会以语音条发出",
                        os.path.splitext(_elem_name or "")[1] or "?",
                    )

        # ---- 与核心逐行一致，唯一区别是 file_type 由上面算出来 ----
        if getattr(media_element, "file_type", None) == "url":
            if is_group:
                return await api.post_group_file(
                    group_openid=target_id, file_type=file_type,
                    url=media_element.file, srv_send_msg=False)
            return await api.post_c2c_file(
                openid=target_id, file_type=file_type,
                url=media_element.file, srv_send_msg=False)

        from botpy.http import Route
        # 有转码产物就用它；否则用元素自己的路径
        file_path = silk_path or await media_element.to_path()
        data = await asyncio.to_thread(Path(file_path).read_bytes)
        _up_name = os.path.basename(silk_path) if silk_path else _guess_name(media_element)
        #: 图片被平台以格式为由拒绝时，用来"按文件再发一次"的**原始字节**
        _file_fallback = None
        _img_mode = "auto"
        if file_type == FT_IMAGE:
            _img_mode = str(getattr(plugin, "gif_sticker_mode", "auto") or "auto").lower()
            if _img_mode not in ("auto", "image", "file"):
                _img_mode = "auto"
            _fmt = sniff_image_format(data)
            if _fmt not in _QQ_OK_FORMATS:
                # ★★★ 2026-10-09（用户情报）：平台图片格式**已支持 gif/webp** ——
                #   "原图优先"：auto 模式下，动图先**原样直传**（保动画）；
                #   被平台以格式为由拒（850019）才退转档（APNG→PNG）→ 再不行按文件发。
                if _img_mode == "auto" and _fmt in ("gif", "webp"):
                    _anim = await asyncio.to_thread(_animated_format, data)
                    if _anim:
                        _probe_res = await _try_send_original(
                            api, target_id, is_group, data, _elem_name, logger)
                        if _probe_res is not None:
                            return _probe_res
                # 非动图 / image 模式：走转档（平台历史上只收 png/jpg，GIF 直传=850019）
                if _img_mode == "file":
                    # 用户明确要求：这类图**原样按文件发**（动图下载后还能动）
                    file_type = FT_FILE
                    if not os.path.splitext(_up_name or "")[1]:
                        # base64 进来的元素常常没有文件名 ⇒ 按魔数补一个（不然 QQ 显示"未命名"）
                        _up_name = "sticker." + (_fmt or "bin")
                    if logger is not None:
                        logger.info(
                            "[QQBOT-BRIDGE] %s 不在平台图片白名单（png/jpg）里 ⇒ 按配置"
                            "（gif_sticker_mode=file）**原样按文件发送**（保留动图）",
                            _fmt or "该格式",
                        )
                else:
                    _orig_bytes, _orig_name = data, _up_name
                    data, _new_name, _note = await asyncio.to_thread(
                        normalize_image_data, data, _up_name, logger,
                        allow_anim=(_img_mode == "auto"))
                    if _note:
                        _up_name = _new_name
                    if data is not _orig_bytes:
                        _file_fallback = (_orig_bytes, _orig_name)
                    # 规范化后仍超过图片软限制（20MB）⇒ 平台也会降级成文件，直接按文件发
                    if len(data) > 20 * 1024 * 1024:
                        file_type = FT_FILE
                        data, _up_name = _orig_bytes, _orig_name
                        _file_fallback = None
                        if logger is not None:
                            logger.info(
                                "[QQBOT-BRIDGE] 该图片超过平台图片软限制（20MB）⇒ 直接按文件发送")
        # ★★★ 图片补一个"体面文件名"（2026-10-10 对齐原生成功路径）：
        #   KiraAI 原生（不带本插件）发 GIF 成功 —— 它走 `<file type="image">`，
        #   上传体**必然带 file_name（test.gif）**；本插件贴纸来自 base64、
        #   没有名字、上传体里没有 file_name → 现场 GIF 被拒（850019）。
        if file_type == FT_IMAGE:
            _up_name = _image_upload_name(data, _up_name)
        _ = _img_mode
        payload: dict = {
            "file_type": file_type,
            "file_data": base64.b64encode(data).decode("ascii"),
            "srv_send_msg": False,
        }
        # ★★★ `file_name` 的发放规则（2026-10-10 修订版）：
        #
        #   * `file_type=4`（文件）⇒ 必带（官方文档/三家实现一致，用于显示文件名）；
        #   * `file_type=3`（语音）⇒ **绝不带**（2026-10-08 定位到的"语音变文件卡片"
        #     根因就是它；去名后语音条才正常）；
        #   * `file_type=1`（图片）⇒ **带**（2026-10-10 修订）：
        #     - 依据一（实锤对照）：KiraAI 原生路径上传图片永远带真实文件名
        #       （`<file type="image">test.gif` ⇒ `file_name=test.gif`），
        #       且现场**发 GIF 成功**；本插件贴纸（无文件名）GIF 被拒；
        #     - 依据二（官方文档口径）：富媒体概述把 gif/webp/bmp 列为图片
        #       支持格式，文件名扩展名是平台识别格式的最直接线索；
        #     - 依据三：官方 Node SDK 只是"不主动给非 FILE 带名"（缺少此字段
        #       时的保守行为），并没有"禁止"——原生路径与官方文档都不冲突。
        #   * `file_type=2`（视频）⇒ 保持不带（无实证需求，先不动）。
        name = _up_name
        if file_type in (FT_FILE, FT_IMAGE) and name:
            payload["file_name"] = os.path.basename(name.split("?")[0])
        # 一次性诊断：把"我们到底发了什么形状的体"写进日志（不含 file_data）
        _log_upload_shape_once(file_type, payload, logger)
        if is_group:
            payload["group_openid"] = target_id
            route = Route("POST", "/v2/groups/{group_openid}/files",
                          group_openid=target_id)
        else:
            payload["openid"] = target_id
            route = Route("POST", "/v2/users/{openid}/files", openid=target_id)
        # ★ 上传结果也记一条：成功/失败都要看得见（否则线上只能靠猜）。
        #   失败时把平台的原始响应也带出来 —— 像 `500 call inner proxy error`
        #   这种是**平台侧**的问题，有日志才能一眼分清是谁的锅。
        try:
            result = await api._http.request(route, json=payload)
        except Exception as exc:
            if logger is not None:
                _hint = humanize_upload_error(exc)
                logger.warning(
                    "[QQBOT-BRIDGE] 媒体上传失败（file_type=%s，文件=%s）：%s: %s%s",
                    file_type, os.path.basename(str(file_path)),
                    type(exc).__name__, str(exc)[:200],
                    ("\n    → " + _hint) if _hint else "",
                )
            # ★ 「原样发的 mp3/ogg 被平台拒了」⇒ **转 silk 再试一次**（只试一次）。
            #   这样既保留了「官方支持就直接发」的快路径，
            #   又保证最终一定能发成语音条。
            if _pending_silk_retry and silk_path is None:
                retried = await _retry_as_silk(
                    api, target_id, media_element, is_group, exc, logger)
                if retried is not None:
                    return retried
            # ★★ 图片被平台以"格式不支持"拒（850019/850031）⇒ **按文件再发一次**（只一次）。
            #    折中：内嵌显示做不到，至少让表情包/图片**发得出去**（用户点开可看原图，
            #    动图也还是动的）。仅对"图片且确实转换过"的情形生效 —— **不误伤别的类型**。
            if _file_fallback is not None and is_format_error(exc):
                _fb_data, _fb_name = _file_fallback
                if not os.path.splitext(_fb_name or "")[1]:
                    _fb_name = "sticker." + (sniff_image_format(_fb_data) or "bin")
                payload_fb: dict = {
                    "file_type": FT_FILE,
                    "file_data": base64.b64encode(_fb_data).decode("ascii"),
                    "srv_send_msg": False,
                    "file_name": _fb_name,
                }
                if is_group:
                    payload_fb["group_openid"] = target_id
                else:
                    payload_fb["openid"] = target_id
                try:
                    if logger is not None:
                        logger.warning(
                            "[QQBOT-BRIDGE] 平台拒收该图片格式（%s）⇒ 已自动**改按文件发送**"
                            "（原图/动图都在，点开可看）；想强制内嵌可把 gif_sticker_mode 设为 image",
                            str(exc)[:80],
                        )
                    return await api._http.request(route, json=payload_fb)
                except Exception as exc2:
                    if logger is not None:
                        logger.warning(
                            "[QQBOT-BRIDGE] 按文件发送也失败：%s —— 交回核心逻辑",
                            str(exc2)[:120],
                        )
                    raise
            raise
        if logger is not None and file_type not in _HEAD_LOGGED:
            _HEAD_LOGGED.add(file_type)
            logger.info(
                "[QQBOT-BRIDGE] 媒体上传自检：file_type=%s 字节=%s 头部=%s 文件名=%s"
                "（语音条要求 file_type=3 且内容为 silk：腾讯系头 \\x02 或标准头 #!SILK_V3）",
                file_type, len(data), data[:12].hex(),
                payload.get("file_name") or "（未发文件名）",
            )
        if logger is not None:
            _fi = result.get("file_info") if isinstance(result, dict) else getattr(result, "file_info", None)
            logger.info(
                "[QQBOT-BRIDGE] 媒体上传完成：file_type=%s 文件=%s → %s",
                file_type, os.path.basename(str(file_path)),
                "拿到 file_info" if _fi else f"响应异常 {str(result)[:120]}",
            )
        return result


    setattr(_upload_file, "_kira_bridge_ftype", True)
    setattr(_upload_file, "_kira_bridge_build", _WRAPPER_BUILD)
    setattr(_upload_file, "_kira_bridge_orig", current)
    try:
        holder._upload_file = _upload_file
    except Exception as exc:
        _install_fail(f"写入 holder._upload_file 失败（{type(holder).__name__}）: {exc}", logger)
        return False

    if not getattr(holder, "_kira_bridge_ftype_logged", False):
        try:
            holder._kira_bridge_ftype_logged = True
        except Exception:
            pass
        if logger is not None:
            logger.info(
                "[QQBOT-BRIDGE] 已修正媒体类型（挂在 %s 上）：视频按 2、语音按 3 上传"
                "（官方定义 1图/2视频/3语音/4文件；框架原先一律按 4 发 ⇒ "
                "视频与语音会退化成「文件卡片」，不能内嵌播放）",
                type(holder).__name__,
            )
    return True


def restore(holder: Any) -> bool:
    """还原（幂等）。"""
    current = getattr(holder, "_upload_file", None)
    orig = getattr(current, "_kira_bridge_orig", None)
    if callable(orig):
        try:
            holder._upload_file = orig
            return True
        except Exception:
            return False
    return False
