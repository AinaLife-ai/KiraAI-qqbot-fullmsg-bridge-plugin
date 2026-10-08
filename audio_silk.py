"""把音频转成 QQ 语音条要求的 **silk** 格式（插件侧，不动核心）。

## 为什么需要

官方「文件类型与限制」表：

```
file_type=3  语音  silk  软限制 20MB
```

第三方项目实测印证：

> QQ 官方 API 的 `file_type=3` **只认 SILK**，**MP3 直接上传会降级成文件**。
> 在发送前用 pilk 转 SILK 后，**语音全部以气泡形式送达**。

⇒ 想发**语音条**就必须先转 silk；不然平台把 mp3 当成"不认识的东西"落成文件卡片。

## 为什么不能只靠 ffmpeg

**ffmpeg 只能解码 silk，不能编码**（实测 `ffmpeg -encoders | grep silk` 为空）。
Silk 是 Skype 的专有编解码器，编码必须用专门实现 ⇒ 用 `pilk`。

而 pilk 只吃 **PCM**，所以链路是：

```
mp3/wav/ogg/m4a ──imageio-ffmpeg(静态ffmpeg)──> pcm ──pilk──> silk
        （用户机器上没有系统 ffmpeg，所以用 imageio-ffmpeg 自带的那份）
```

## 设计要点

* **只对「要发语音条」的音频动手**（`Record` 元素）；`File` 元素一律不碰
  ⇒ 「想发语音条」和「想发音频文件」**两条路同时可用**，互不影响。
* **任何一步失败都退回原文件**（按 file_type=4 当文件发），
  **绝不因为转码失败而丢掉消息**。
* 依赖由 `requirements.txt` 交给 KiraAI 插件加载器自动 `pip install`，
  不要求用户有系统 ffmpeg / 管理员权限。
* 结果按 `(路径, 大小, mtime)` 缓存，避免同一条音频重复转码。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["to_silk_if_needed", "silk_available", "clear_cache"]

#: silk 文件以这个魔数开头（`#!SILK_V3`）
SILK_MAGIC = b"#!SILK_V3"

#: 已经是 silk 的扩展名
_SILK_EXT = (".silk", ".slk", ".sil")

#: 需要转码的音频扩展名（其余（如 mp4/aac）尝试让 ffmpeg 自己判断）
_AUDIO_HINT_EXT = (".mp3", ".wav", ".ogg", ".oga", ".m4a", ".aac", ".flac", ".wma", ".amr", ".opus")

#: 转码结果缓存：key=(路径,大小,mtime) → silk 文件路径
_CACHE: dict = {}
_CACHE_MAX = 32

#: 单个音频的上限（官方语音软限制 20MB；给源文件留些余量）
_MAX_SOURCE_BYTES = 60 * 1024 * 1024


def _digest(path: str) -> str:
    try:
        st = os.stat(path)
        return f"{path}|{st.st_size}|{int(st.st_mtime)}"
    except Exception:
        return path


def silk_available() -> bool:
    """两个依赖是否都在（缺任一个都做不了 silk 转码）。"""
    try:
        import pilk  # noqa: F401
    except Exception:
        return False
    try:
        import imageio_ffmpeg  # noqa: F401
    except Exception:
        return False
    return True


def _ffmpeg_exe() -> Optional[str]:
    """拿一个可用的 ffmpeg：先用 imageio-ffmpeg 自带的，再退回系统 PATH 上的。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


def _is_silk_file(path: str) -> bool:
    """按扩展名或魔数判断是不是 silk。"""
    if path.lower().endswith(_SILK_EXT):
        return True
    try:
        with open(path, "rb") as f:
            return f.read(len(SILK_MAGIC)) == SILK_MAGIC
    except Exception:
        return False


def _convert_sync(src: str, out_dir: str) -> Optional[str]:
    """同步转码：`src`（任意音频）→ silk 文件路径；失败返回 None。

    链路：ffmpeg 解码成 PCM → pilk 编码成 silk。
    """
    try:
        import pilk
    except Exception as exc:
        logger.warning("[QQBOT-BRIDGE] 缺少 pilk，无法把音频转成语音条 silk：%s", exc)
        return None

    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        logger.warning("[QQBOT-BRIDGE] 找不到 ffmpeg（imageio-ffmpeg 也没装上），无法转 silk")
        return None

    base = os.path.splitext(os.path.basename(src))[0] or "voice"
    pcm_path = os.path.join(out_dir, base + ".pcm")
    silk_path = os.path.join(out_dir, base + ".silk")

    # ① 解码成 PCM。采样率必须和后面 pilk 的 pcm_rate 一致。
    #    24000 是 SILK 宽带模式（QQ/微信语音常用），单声道。
    rate = 24000
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", src,
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ac", "1", "-ar", str(rate),
        pcm_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0 or not os.path.exists(pcm_path):
        logger.warning(
            "[QQBOT-BRIDGE] ffmpeg 解码音频失败（exit=%s）：%s",
            proc.returncode, (proc.stderr or b"")[:180].decode(errors="replace"),
        )
        return None

    # ② PCM → silk（pilk 的 pcm_rate 必须与上面 -ar 一致）
    #
    #   ★ pilk 的 API 是 `encode(pcm路径, silk路径, pcm_rate=…, tencent=True)`
    #     —— **接受路径字符串**，不是文件对象。
    #   ★ tencent=True：输出腾讯系（QQ/微信）认的 silk 变体。
    try:
        pilk.encode(pcm_path, silk_path, pcm_rate=rate, tencent=True)
    except Exception as exc:
        logger.warning("[QQBOT-BRIDGE] pilk 编码 silk 失败：%s: %s", type(exc).__name__, exc)
        return None
    finally:
        try:
            os.remove(pcm_path)
        except Exception:
            pass

    if not os.path.exists(silk_path) or os.path.getsize(silk_path) == 0:
        logger.warning("[QQBOT-BRIDGE] silk 转码产物为空")
        return None
    return silk_path


async def to_silk_if_needed(path: str, logger_: Any = None) -> Optional[str]:
    """把音频转成 silk 文件路径；**已经是 silk 则原样返回**。

    返回 None 表示"转不了"（缺依赖 / 解码失败 / 源文件异常），
    调用方**应按原文件发送**（退化成文件），不要丢消息。
    """
    if not path or not os.path.isfile(path):
        return None
    if path.lower().endswith(_SILK_EXT) or _is_silk_file(path):
        return path                       # 已经是 silk，不用动
    try:
        if os.path.getsize(path) > _MAX_SOURCE_BYTES:
            if logger_ is not None:
                logger_.warning(
                    "[QQBOT-BRIDGE] 音频过大（%.1f MB），跳过 silk 转码，按文件发送",
                    os.path.getsize(path) / 1048576,
                )
            return None
    except Exception:
        return None

    key = _digest(path)
    hit = _CACHE.get(key)
    if hit and os.path.exists(hit[0]):
        return hit[0]

    if not silk_available():
        if logger_ is not None:
            logger_.warning(
                "[QQBOT-BRIDGE] 未安装 pilk / imageio-ffmpeg，无法把音频转成语音条 —— "
                "本条按「文件」发送。装好后即可自动转（插件已在 requirements.txt 声明）",
            )
        return None

    out_dir = None
    try:
        out_dir = tempfile.mkdtemp(prefix="qqbot_silk_")
        silk = await asyncio.to_thread(_convert_sync, path, out_dir)
        if not silk:
            return None
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = (silk,) + (out_dir,)
        if logger_ is not None:
            logger_.info(
                "[QQBOT-BRIDGE] 已把音频转成 silk 语音条（%.1f KB）—— "
                "官方 file_type=3 只认 silk，转完才会显示成语音条而不是文件",
                os.path.getsize(silk) / 1024,
            )
        return silk
    except Exception as exc:
        if logger_ is not None:
            logger_.warning("[QQBOT-BRIDGE] silk 转码异常：%s: %s", type(exc).__name__, exc)
        return None


def clear_cache() -> None:
    """清空转码缓存（测试 / 还原用）。"""
    _CACHE.clear()
