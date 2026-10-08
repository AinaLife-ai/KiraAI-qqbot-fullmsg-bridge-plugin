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

__all__ = ["convert_to_silk_forced", "is_silk_path", "silk_magic_ok", "reset_encoder_cache", "reset_ffmpeg_cache", "to_silk_if_needed", "silk_available", "clear_cache"]

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


def is_silk_path(path: str) -> bool:
    """这个路径**看起来**是不是 silk（按扩展名或魔数）。给 media_types 用。

    ⚠ 注意：扩展名是 `.silk` 就返回 True —— **不代表内容真的是 silk**。
      只看扩展名会被"改个名就当 silk 发"的模型骗过（2026-10-08 线上踩到：
      用户那条 `jbf_v2.silk` 浏览器里下下来是文件卡片）。
      要"内容确实是 silk"的判据请用 :func:`silk_magic_ok`。
    """
    try:
        return bool(path) and _is_silk_file(path)
    except Exception:
        return False


def silk_magic_ok(path: str) -> bool:
    """**内容确实是 silk**（按魔数，不看扩展名）。

    腾讯系 = ``\\x02`` + ``#!SILK_V3``；标准系 = ``#!SILK_V3``。两者都算 silk。

    为什么单独开一个函数：`is_silk_path` 只看扩展名，
    而"模型把 ogg 改名成 .silk"这种事太常见了 —— 那种文件我们必须
    **当普通音频去转码**，而不是原样发给 QQ（QQ 会降级成文件卡片）。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except Exception:
        return False
    return head[:1] == b"\x02" and head[1:10] == SILK_MAGIC or head[:9] == SILK_MAGIC


def _is_silk_file(path: str) -> bool:
    """按扩展名或魔数判断是不是 silk。"""
    if path.lower().endswith(_SILK_EXT):
        return True
    return silk_magic_ok(path)


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


def _fix_tencent_header(silk_path: str, logger_: Any = None) -> bool:
    """**确保产物是腾讯系 silk**（QQ/微信认的那种）。

    ## ★★★ 为什么要这一步（2026-10-08 挖到根上）

    从 `silk-v3-decoder` 的 `Encoder.c` 源码里确认了 `-tencent` 的**确切差异**：

    ```c
    // ① 开头：腾讯系**多写一个 0x02 字节**
    if (tencent) {
        static const char Tencent_break[] = "\\x02";
        fwrite(Tencent_break, 1, 1, bitOutFile);
    }
    fwrite("#!SILK_V3", ...);

    // ② 结尾：非腾讯系多写一个 0x00（腾讯系不写）
    if (!tencent) {
        fwrite(&nBytes, 2, 1, bitOutFile);
    }
    ```

    ⇒ **腾讯系 = `\x02` + `#!SILK_V3` …（结尾不带 0x00）**
      标准系   = `#!SILK_V3` … + 0x00

    **这不是"可选优化"，是 QQ 认不认的分水岭** ——
    用标准系（例如 `silk-wasm` 的 `encode(pcm, rate)`，它**没有 tencent 选项**）
    产出的 silk，QQ 会**当成文件**发，显示成文件卡片而不是语音条。

    所以我们**每次都在这里校验一遍**：头不对就自己补/修 ——
    这样即便某个后端的默认值变了（或用户自己接了个不兼容的编码器），
    我们也一定能发出语音条。
    """
    try:
        with open(silk_path, "rb") as f:
            head = f.read(16)
        if head[:1] == b"\x02" and head[1:10] == b"#!SILK_V3":
            return True                       # 已经是腾讯系，不用动
        if head[:9] == b"#!SILK_V3":
            # 标准系：#!SILK_V3 … ⇒ 前面补一个 0x02 就是腾讯系
            with open(silk_path, "rb") as f:
                data = f.read()
            with open(silk_path, "wb") as f:
                f.write(b"\x02" + data)
            if logger_ is not None:
                logger_.info(
                    "[QQBOT-BRIDGE] silk 产物原为**标准格式**，已自动补上腾讯系头部"
                    "（QQ 只认腾讯系：\\x02 + #!SILK_V3）"
                )
            return True
        if logger_ is not None:
            logger_.warning(
                "[QQBOT-BRIDGE] silk 产物头部异常（前 10 字节=%s），QQ 可能不认",
                head[:10].hex(),
            )
        return False
    except Exception as exc:
        if logger_ is not None:
            logger_.debug("[QQBOT-BRIDGE] 校验 silk 头失败: %s", exc)
        return False


async def convert_to_silk_forced(path: str, logger_: Any = None) -> Optional[str]:
    """**强制**把音频转成 silk（不管它现在是什么格式），返回新路径或 None。

    与 `to_silk_if_needed` 的区别：后者对"已经是 silk"会原样返回，
    而这个是"**我要一份 silk**"—— 给"mp3/ogg 原样发被平台拒了、需要重试"用。

    ⚠ **不阻塞**：转码走 `asyncio.to_thread`（`_convert_sync` 里是 subprocess +
      pilk/pysilk 的同步调用），事件循环不受影响。
    """
    if not path or not os.path.isfile(path):
        return None
    if silk_magic_ok(path):
        return path if _fix_tencent_header(path, logger_) else None
    if not silk_available():
        return None
    # 已有缓存就直接用（避免重复转）
    key = _digest(path)
    hit = _CACHE.get(key)
    if hit and os.path.exists(hit[0]):
        return hit[0]
    out_dir = None
    try:
        out_dir = tempfile.mkdtemp(prefix="qqbot_silk_")
        silk = await asyncio.to_thread(_convert_sync, path, out_dir)
        if not silk:
            _rm_tree(out_dir)
            return None
        _prune_cache()
        _CACHE[key] = (silk,) + (out_dir,)
        return silk
    except Exception as exc:
        if out_dir:
            _rm_tree(out_dir)
        if logger_ is not None:
            logger_.warning("[QQBOT-BRIDGE] 强制转 silk 失败：%s", exc)
        return None


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
    # ★ 保险：**必须是腾讯系 silk**，否则 QQ 会当文件发（见 _fix_tencent_header）
    if not _fix_tencent_header(silk_path, logger):
        logger.warning("[QQBOT-BRIDGE] silk 产物不是 QQ 认的格式（后端=%s）—— 本条按文件发送", encoder)
        return None
    return silk_path


async def to_silk_if_needed(path: str, logger_: Any = None) -> Optional[str]:
    """把音频转成 silk 文件路径；**已经是 silk 则原样返回**。

    返回 None 表示"转不了"（缺依赖 / 解码失败 / 源文件异常），
    调用方**应按原文件发送**（退化成文件），不要丢消息。
    """
    if not path or not os.path.isfile(path):
        return None
    if silk_magic_ok(path):
        # ★★★ 内容确实是 silk，但**仍然要过一遍腾讯系头校验**（2026-10-08 修的真 bug）。
        #
        #   原来这里直接 `return path`，等于**跳过了 _fix_tencent_header**
        #   ⇒ 如果这个 silk 是**标准系**（例如 `silk-wasm` 的 `encode(pcm, rate)`
        #   产出 —— 它**没有 tencent 选项**），我们原样发给 QQ
        #   ⇒ QQ 不认 ⇒ **文件卡片**。
        #   用户的 `jbf_v2.silk` 正是这种情况（我们一直没修它）。
        if not _fix_tencent_header(path, logger_):
            if logger_ is not None:
                logger_.warning(
                    "[QQBOT-BRIDGE] 这个 silk 文件不是 QQ 认的格式（%s）—— 本条按文件发送",
                    os.path.basename(path),
                )
            return None
        return path
    # ★ 扩展名写着 .silk、内容却不是 silk（2026-10-08 线上第三种形态）：
    #   **多半只是把 ogg/mp3 改了个名**。这种情况绝不能原样发 ——
    #   QQ 会（不报错地）把它降级成**文件卡片**。下面按普通音频重新编码。
    if path.lower().endswith(_SILK_EXT) and logger_ is not None:
        try:
            with open(path, "rb") as _f:
                _head = _f.read(12).hex()
        except Exception:
            _head = "?"
        logger_.warning(
            "[QQBOT-BRIDGE] %s 扩展名是 silk，但**内容不是 silk**（前 12 字节=%s）——"
            "多半只是改了个名（或编码器产出的不是 QQ 认的格式）。"
            "接下来会按普通音频重新编码成语音条；**若编码器不可用，这条只能以文件卡片发出**",
            os.path.basename(path), _head,
        )
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
                "[QQBOT-BRIDGE] ★ 缺少语音转码依赖，这条发不成语音条 —— "
                "需要 silk 编码器（pysilk/pilk 之一）**和** ffmpeg（系统的或 imageio-ffmpeg）。"
                "修复：pip install silk-python imageio-ffmpeg（或让 KiraAI 的插件依赖安装重跑一次）。"
                "装好后本插件会自动转码，无需任何配置",
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
