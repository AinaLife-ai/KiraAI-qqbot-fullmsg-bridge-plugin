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
                except Exception as exc:
                    if logger_ is not None:
                        logger_.debug("[QQBOT-BRIDGE] APNG 转换失败，退回静态图: %s", exc)
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


def is_format_error(exc: BaseException) -> bool:
    text = str(exc)
    return any(code in text for code in _FORMAT_ERROR_CODES)


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


def install(holder: Any, client: Any, logger: Any = None, plugin: Any = None) -> bool:
    """把 `holder._upload_file` 换成「类型更准」的版本。幂等。

    :param holder: 真正带 `_upload_file` 的对象
                   （3.0 = 能力对象；2.x = 适配器实例）
    :param client: botpy 客户端（拿 `api` / `_http`）
    """
    if holder is None or client is None:
        return False
    current = getattr(holder, "_upload_file", None)
    if not callable(current):
        return False
    if getattr(current, "_kira_bridge_ftype", False):
        return True

    api = getattr(client, "api", None)
    if api is None:
        return False
    try:
        from botpy.http import Route                      # noqa: F401
    except Exception:
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
                # 平台的上传接口只收 png/jpg（实测 GIF 直传 = 850019）
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
        _ = _img_mode
        payload: dict = {
            "file_type": file_type,
            "file_data": base64.b64encode(data).decode("ascii"),
            "srv_send_msg": False,
        }
        # ★★★ `file_name` **只对 file_type=4（文件）发** —— 2026-10-08 定案。
        #
        #   三家官方/官方推荐实现完全一致（都不给语音带文件名）：
        #
        #   ① 腾讯官方 Node SDK（`@tencent-connect/qqbot-nodejs`）
        #      `src/protocol/api/media.ts`：
        #          if (fileType === MediaFileType.FILE && opts.fileName) {
        #              body.file_name = this.sanitize(opts.fileName);
        #          }
        #      其 `USAGE.md` 也写明：`fileName: ... // 仅 FILE 类型有效`。
        #   ② 官方 openclaw-qqbot 走同一个 SDK（上传体里语音没有 file_name）。
        #   ③ QQ 官方推荐的 Hermes（`gateway/platforms/qqbot/adapter.py`）：
        #          body = {"file_type": file_type, "srv_send_msg": srv_send_msg}
        #          ...
        #          if file_type == MEDIA_TYPE_FILE and file_name:
        #              body["file_name"] = file_name
        #
        #   ⇒ 我们原来给**语音**也带文件名（`jbf_v2.silk`），而用户看到的正是
        #     **文件卡片 + 那个文件名**。给语音带名字属于超出文档约定的用法，
        #     按官方口径对齐：非 FILE 一律不带。（图片/视频同理，一并去掉。）
        name = _up_name
        if file_type == FT_FILE and name:
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
    setattr(_upload_file, "_kira_bridge_orig", current)
    try:
        holder._upload_file = _upload_file
    except Exception as exc:
        if logger is not None:
            logger.debug("[QQBOT-BRIDGE] 安装 file_type 修正失败: %s", exc)
        return False

    if not getattr(holder, "_kira_bridge_ftype_logged", False):
        try:
            holder._kira_bridge_ftype_logged = True
        except Exception:
            pass
        if logger is not None:
            logger.info(
                "[QQBOT-BRIDGE] 已修正媒体类型：视频按 2、语音按 3 上传"
                "（官方定义 1图/2视频/3语音/4文件；框架原先一律按 4 发 ⇒ "
                "视频与语音会退化成「文件卡片」，不能内嵌播放）"
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
