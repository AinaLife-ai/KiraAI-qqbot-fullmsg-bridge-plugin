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

__all__ = ["reset_encoder_cache", "reset_ffmpeg_cache", "to_silk_if_needed", "silk_available", "clear_cache"]

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
    """**编码器**（pilk）是否可用 —— 这是硬前提。

    ★ 为什么把 ffmpeg 拆出去（对照 `KiraAI_video_comprehension_plugin` 的做法）：
      那个插件「优先用系统已有的 ffmpeg，没有才自己下」，**不把 ffmpeg 当硬依赖**。
      我们这里同理：系统可能本来就装了 ffmpeg（很多服务器都有），
      pip 装 `imageio-ffmpeg` 只是**兜底**。
      ⇒ 只要有 pilk + **任一** ffmpeg（系统的或 imageio 自带的）就能转。
    """
    return _silk_encoder() is not None and _ffmpeg_exe() is not None


#: silk 编码器探测缓存（'pysilk' / 'pilk' / 'silk_v3_encoder' / None）
_ENCODER_CACHE: Any = None
_ENCODER_TRIED = False

#: ffmpeg 查找结果缓存（`None` = 还没找过；找到后固定不变）
_FFMPEG_CACHE: Any = None
_FFMPEG_TRIED = False


def _ffmpeg_exe() -> Optional[str]:
    """拿一个可用的 ffmpeg。

    **顺序（先便宜后昂贵，与视频插件一致）**：

    1. `IMAGEIO_FFMPEG_EXE` 环境变量（用户显式指定，最高优先，不校验存在性）；
    2. **系统 PATH** 上的 `ffmpeg` —— 不下载、不依赖 pip 包，最快；
    3. `imageio-ffmpeg` 自带的静态二进制（pip 装好就有，兜底）。

    ★ 结果缓存：这是**每次发语音都会问一次**的路径，不缓存的话每轮都要
      `shutil.which` 扫 PATH。缓存后固定不变（进程生命周期内 ffmpeg 不会挪窝）。
    """
    global _FFMPEG_CACHE, _FFMPEG_TRIED
    if _FFMPEG_TRIED:
        return _FFMPEG_CACHE
    _FFMPEG_TRIED = True

    # ① 用户显式指定
    env = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if env and os.path.exists(env):
        _FFMPEG_CACHE = env
        return env
    # ② 系统 PATH（最省事，无需任何下载）
    sys_exe = shutil.which("ffmpeg")
    if sys_exe:
        _FFMPEG_CACHE = sys_exe
        return sys_exe
    # ③ imageio-ffmpeg 自带的静态二进制
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            _FFMPEG_CACHE = exe
            return exe
    except Exception:
        pass
    _FFMPEG_CACHE = None
    return None


def _is_silk_file(path: str) -> bool:
    """按扩展名或魔数判断是不是 silk。"""
    if path.lower().endswith(_SILK_EXT):
        return True
    try:
        with open(path, "rb") as f:
            return f.read(len(SILK_MAGIC)) == SILK_MAGIC
    except Exception:
        return False


def _silk_encoder() -> Optional[str]:
    """挑一个**能用的** silk 编码器，返回它的名字；一个都没有时返回 None。

    ## 为什么要有多个后端（2026-10-08 踩坑）

    原来只用 `pilk`，但它的 **Windows wheel 只到 cp311** ——
    用 Python 3.12+（KiraAI 自带 3.13）的机器 pip 会去**编译源码**，
    Windows 上编译需要 MSVC ⇒ 直接失败：

        error: Microsoft Visual C++ 14.0 or greater is required.

    ⇒ 现在按顺序找：

    1. **`pysilk`（PyPI 包名 `silk-python`）** —— wheel 覆盖 **cp38–cp314**，
       py3.12/3.13 上装**预编译包即可，无需编译器**。首选。
    2. `pilk` —— 老牌，cp311 及以下可用。
    3. 外部 `silk_v3_encoder` 可执行文件（用户自己装了的话）。

    结果缓存（这是每次发语音都会问的路径）。
    """
    global _ENCODER_CACHE, _ENCODER_TRIED
    if _ENCODER_TRIED:
        return _ENCODER_CACHE
    _ENCODER_TRIED = True

    for mod in ("pysilk", "pilk"):
        try:
            __import__(mod)
            _ENCODER_CACHE = mod
            return mod
        except Exception:
            continue
    if shutil.which("silk_v3_encoder"):
        _ENCODER_CACHE = "silk_v3_encoder"
        return _ENCODER_CACHE
    _ENCODER_CACHE = None
    return None


def reset_encoder_cache() -> None:
    """清掉编码器探测缓存（测试 / 装完依赖后重试用）。"""
    global _ENCODER_CACHE, _ENCODER_TRIED
    _ENCODER_CACHE, _ENCODER_TRIED = None, False


def _encode_silk(encoder: str, pcm_path: str, silk_path: str, rate: int) -> bool:
    """用指定后端把 PCM 编成 silk。成功返回 True。

    三个后端的 API 形态不同，这里统一收口：

    * `pysilk`（silk-python）：`encode(pcm_fp, silk_fp, pcm_rate, bit_rate)`
      —— 接受 **file-like object**；
    * `pilk`：`encode(pcm路径, silk路径, pcm_rate=…, tencent=True)`
      —— 接受**路径字符串**，且要 `tencent=True` 才是腾讯系变体；
    * `silk_v3_encoder`：外部可执行文件，`-tencent` 参数。
    """
    if encoder == "pysilk":
        import pysilk
        with open(pcm_path, "rb") as fin, open(silk_path, "wb") as fout:
            pysilk.encode(fin, fout, rate, rate)
        return os.path.exists(silk_path) and os.path.getsize(silk_path) > 0

    if encoder == "pilk":
        import pilk
        pilk.encode(pcm_path, silk_path, pcm_rate=rate, tencent=True)
        return os.path.exists(silk_path) and os.path.getsize(silk_path) > 0

    if encoder == "silk_v3_encoder":
        exe = shutil.which("silk_v3_encoder")
        if not exe:
            return False
        proc = subprocess.run(
            [exe, pcm_path, silk_path, "-Fs_API", str(rate), "-tencent"],
            capture_output=True, timeout=300,
        )
        return proc.returncode == 0 and os.path.exists(silk_path)

    return False


def _convert_sync(src: str, out_dir: str) -> Optional[str]:
    """同步转码：`src`（任意音频）→ silk 文件路径；失败返回 None。

    链路：**ffmpeg 解码成 PCM → silk 编码器编成 silk**。

    ★ 注意：**ffmpeg 压不出 silk**（它没有 silk 编码器），
      只负责第一步的 PCM 解码 —— 这一点已被实测确认。
    """
    encoder = _silk_encoder()
    if not encoder:
        logger.warning(
            "[QQBOT-BRIDGE] 没有可用的 silk 编码器（pysilk / pilk / silk_v3_encoder 都没有），"
            "无法把音频转成语音条 silk —— 本条会按「文件」发送。"
            "装依赖后即可自动转（插件已在 requirements.txt 声明 silk-python）"
        )
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

    # ② PCM → silk（采样率必须与上面 ffmpeg 的 -ar 一致）
    #
    #   后端形态不同，统一交给 `_encode_silk`：
    #     * pysilk —— 接受 file-like；直接用，无需 tencent 参数
    #     * pilk   —— 接受路径 + `tencent=True`（腾讯系变体）
    #     * silk_v3_encoder —— 外部二进制，带 `-tencent`
    try:
        ok = _encode_silk(encoder, pcm_path, silk_path, rate)
    except Exception as exc:
        logger.warning(
            "[QQBOT-BRIDGE] silk 编码失败（后端=%s）：%s: %s",
            encoder, type(exc).__name__, exc,
        )
        return None
    finally:
        try:
            os.remove(pcm_path)
        except Exception:
            pass

    if not ok or not os.path.exists(silk_path) or os.path.getsize(silk_path) == 0:
        logger.warning("[QQBOT-BRIDGE] silk 转码产物为空（后端=%s）", encoder)
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
            _rm_tree(out_dir)              # ★ 失败也要清，否则临时目录越积越多
            return None
        _prune_cache()                     # ★ 淘汰前先把旧产物删掉（不只清 dict）
        _CACHE[key] = (silk,) + (out_dir,)
        if logger_ is not None:
            logger_.info(
                "[QQBOT-BRIDGE] 已把音频转成 silk 语音条（%.1f KB）—— "
                "官方 file_type=3 只认 silk，转完才会显示成语音条而不是文件",
                os.path.getsize(silk) / 1024,
            )
        return silk
    except Exception as exc:
        if out_dir:
            _rm_tree(out_dir)
        if logger_ is not None:
            logger_.warning("[QQBOT-BRIDGE] silk 转码异常：%s: %s", type(exc).__name__, exc)
        return None


def _rm_tree(path: Any) -> None:
    """删掉一个临时目录（best-effort，绝不抛）。"""
    try:
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _prune_cache() -> None:
    """缓存满时淘汰一半 —— **同时删掉它们的临时目录**。

    ★ 为什么不是 `_CACHE.clear()` 了事（原来的写法）：
      dict 清了但磁盘上的 `qqbot_silk_xxx/` 目录还在 ⇒ 长期运行的 bot
      会攒下一堆临时文件。这里按写入顺序淘汰（dict 保持插入序）。
    """
    if len(_CACHE) < _CACHE_MAX:
        return
    keys = list(_CACHE.keys())
    for k in keys[: max(1, len(keys) // 2)]:
        entry = _CACHE.pop(k, None)
        if entry and len(entry) > 1:
            _rm_tree(entry[1])


def reset_ffmpeg_cache() -> None:
    """清掉 ffmpeg 查找缓存（配置变更 / 测试用）。

    ★ 为什么要有这个公开入口：缓存是**进程级**的，装了新 ffmpeg 或
      改了 `IMAGEIO_FFMPEG_EXE` 之后需要能重找一次；
      测试也需要它来隔离每个用例（直接改内部变量太脆）。
    """
    global _FFMPEG_CACHE, _FFMPEG_TRIED
    _FFMPEG_CACHE, _FFMPEG_TRIED = None, False


def clear_cache() -> None:
    """清空转码缓存**并删除临时产物**（测试 / 还原用）。"""
    global _FFMPEG_CACHE, _FFMPEG_TRIED
    for entry in list(_CACHE.values()):
        if entry and len(entry) > 1:
            _rm_tree(entry[1])
    _CACHE.clear()
    reset_ffmpeg_cache()
    reset_encoder_cache()
