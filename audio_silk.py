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

__all__ = ["convert_to_silk_forced", "is_silk_path", "silk_magic_ok", "reset_encoder_cache",
           "reset_ffmpeg_cache", "to_silk_if_needed", "silk_available", "clear_cache",
           "configure_trim", "voice_limit", "probe_duration_sync"]

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

# --------------------------------------------------------------------------- #
# ★★★ 语音条时长上限（2026-10-10 用户情报 + 平台实测）
# --------------------------------------------------------------------------- #
#
# 用户实测：**QQ 官方 bot 的语音条最大 5 分钟（300 秒）—— 5:00 整可以，
# 多 1 秒都失败**，失败后退回文件卡片（至少还能收到，不算丢）。
#
# ⇒ 本模块在**转 silk 的同一个 ffmpeg 调用**里加 `-t <上限>`：
#   * 不超长的音频：`-t` 是个 no-op（零成本、零额外进程）；
#   * 超长的音频：ffmpeg 只解码前 N 秒（比解完再切还快），产物天然 ≤ 上限；
#   * 已经在是 silk 的源：先解码量长度（实测 10 秒音频解码 13ms），
#     真超长才截断 PCM 重编码 —— 常规短语音**不付这笔钱**（见尺寸筛）。
#
# 上限值由插件配置注入（`voice_auto_trim` / `voice_max_seconds`），
# 也可以在调用点显式传 `max_seconds=`（显式优先）。
_MAX_SECONDS: Optional[float] = None

#: silk 尺寸筛：低于「上限秒数 × 这个速率」的 silk 不可能超长（保守取 500 B/s），
#: 直接放过、不必解码。实测 silk ≈ 1.7 KB/s（10 秒 17KB），所以 500 很保守。
_SILK_MIN_BYTES_PER_SEC = 500

#: 语音条统一采样率（silk 宽带；与 `_convert_sync` 的 -ar 一致）
_SILK_RATE = 24000

#: 时长探测缓存：(路径,大小,mtime) → 秒（None = 探测失败）
_DUR_CACHE: dict = {}


def configure_trim(max_seconds: Any) -> None:
    """注入语音条时长上限（秒）。``None`` / ``<=0`` / 非法 ⇒ 关闭剪裁。

    幂等；插件启动与热重载时各调一次即可（改配置立即生效）。
    """
    global _MAX_SECONDS
    try:
        val = float(max_seconds) if max_seconds not in (None, "", False) else None
        _MAX_SECONDS = val if (val and 1.0 <= val <= 3600.0) else None
    except Exception:
        _MAX_SECONDS = None


def voice_limit() -> Optional[float]:
    """当前生效的语音条上限（秒）；``None`` = 不剪裁。"""
    return _MAX_SECONDS


def _effective_cap(max_seconds: Any) -> Optional[float]:
    """算出生效的上限：**显式参数优先**，没给才用模块默认。

    ``max_seconds=0`` / 负数 ⇒ 显式关闭剪裁（覆盖模块默认）。
    """
    if max_seconds is None:
        return _MAX_SECONDS
    try:
        val = float(max_seconds)
    except Exception:
        return None
    if val <= 0:
        return None
    return val if 1.0 <= val <= 3600.0 else None


def _fmt_secs(seconds: Any) -> str:
    """秒 → ``m:ss``（日志用）。"""
    try:
        s = max(0.0, float(seconds))
    except Exception:
        return "?"
    return f"{int(s // 60)}:{int(s % 60):02d}"


def probe_duration_sync(path: str, timeout: int = 10) -> Optional[float]:
    """用 ffmpeg 头解析读音频时长（秒）；读不到返回 ``None``。

    * **只解析文件头**（不给输出文件 ⇒ ffmpeg 立刻带着 "Duration: …" 退出），
      不解码整条音频，所以很快（一次进程启动，通常几十到几百毫秒）；
    * 结果按 (路径,大小,mtime) 缓存；
    * **只在真的要剪裁时才调用**（为了日志能写清"原本多长 → 剪到多长"），
      常规短语音走不到这里 ⇒ 热路径零成本。
    """
    key = _digest(path)
    if key in _DUR_CACHE:
        return _DUR_CACHE[key]
    dur: Optional[float] = None
    try:
        import re as _re

        for ffmpeg in _ffmpeg_candidates():
            try:
                proc = subprocess.run(
                    [ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-i", path],
                    capture_output=True, timeout=timeout, **_ffmpeg_spawn_kwargs())
                err = (proc.stderr or b"").decode(errors="replace")
                m = _re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", err)
                if m:
                    dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                           + float(m.group(3)))
                    break
            except Exception:
                continue
    except Exception:
        dur = None
    if len(_DUR_CACHE) > 128:
        _DUR_CACHE.clear()
    _DUR_CACHE[key] = dur
    return dur


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


#: 插件配置里指定的 ffmpeg 路径（由 main.py 注入；最高优先之一）
_FFMPEG_CFG: Optional[str] = None

#: 「配置路径不存在」只警告一次
_FFMPEG_CFG_WARNED = False

#: ffmpeg 解码单次超时（秒）。★ 2026-10-10：从 300 收紧到 120 ——
#: Windows 上 ffmpeg 启动失败会弹**系统错误对话框**（0xC0000142 DLL 初始化失败），
#: 进程会挂在对话框上等用户点确定 ⇒ 我们只能靠超时兜底；
#: 300 秒太长（被动回复窗口只有 5 分钟，用户消息会被拖到快过期），120 秒足够
#: 任何正常音频解码，卡住时也更快退到"换下一个候选/按文件发"。
FFMPEG_TIMEOUT = 120


def set_ffmpeg_path(path: Optional[str]) -> None:
    """注入插件配置里的 `ffmpeg_path`（每轮巡检调用，幂等；空=清除）。"""
    global _FFMPEG_CFG
    _FFMPEG_CFG = str(path).strip() if path else None


def _ffmpeg_candidates() -> list:
    """按优先级列出**所有候选 ffmpeg**（存在性已校验）。

    顺序：插件配置 `ffmpeg_path` → `IMAGEIO_FFMPEG_EXE` 环境变量 →
    系统 PATH → imageio-ffmpeg 自带二进制。
    去重保序；配置/环境变量指向不存在的路径会记一条 WARNING（只记一次）。
    """
    global _FFMPEG_CFG_WARNED
    cands: list = []

    def _add(p, label: str) -> None:
        if not p:
            return
        if not os.path.exists(str(p)):
            if label in ("配置", "环境变量") and not _FFMPEG_CFG_WARNED:
                _FFMPEG_CFG_WARNED = True
                logger.warning(
                    "[QQBOT-BRIDGE] ffmpeg %s指向的路径不存在：%s —— 已忽略，"
                    "改用其它候选（检查插件配置的 ffmpeg_path / IMAGEIO_FFMPEG_EXE）",
                    label, p)
            return
        p = str(p)
        if p not in cands:
            cands.append(p)

    _add(_FFMPEG_CFG, "配置")
    _add(os.environ.get("IMAGEIO_FFMPEG_EXE"), "环境变量")
    _add(shutil.which("ffmpeg"), "PATH")
    try:
        import imageio_ffmpeg
        _add(imageio_ffmpeg.get_ffmpeg_exe(), "imageio")
    except Exception:
        pass
    return cands


def _ffmpeg_spawn_kwargs() -> dict:
    """子进程 spawn 参数（2026-10-10 加固）。

    * `stdin=DEVNULL`：永远不让 ffmpeg 去读 stdin（经典卡死来源之一）；
    * Windows 上 `CREATE_NO_WINDOW`：不弹控制台窗，后台干净跑。
    """
    import subprocess as _sp

    kw = {"stdin": _sp.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
    return kw


def _ffmpeg_startup_failure(code) -> bool:
    """退出码看着像「**进程根本没跑起来**」吗？（Windows 弹窗类错误）

    0xC0000142 = STATUS_DLL_INIT_FAILED —— 用户线报的正是它：
    `ffmpeg-win-x86_64-v7.1.exe - 应用程序无法正常启动(0xc0000142)`，
    进程挂在系统错误对话框上不退出 ⇒ 换一个候选二进制往往就能绕过。
    0xC0000135 = 缺 DLL；0xC0000005/0xC000001D 等也一并按"启动类"处理。
    """
    try:
        u = int(code) & 0xFFFFFFFF
    except Exception:
        return False
    return u in (0xC0000142, 0xC0000135, 0xC0000005, 0xC000001D, 0xC00000FD)


def _ffmpeg_exe() -> Optional[str]:
    """拿一个可用的 ffmpeg（上一次成功的优先；没有就取候选第一个）。

    **候选顺序**（先便宜后昂贵）：插件配置 `ffmpeg_path` →
    `IMAGEIO_FFMPEG_EXE` → 系统 PATH → `imageio-ffmpeg` 自带二进制。
    转换成功后会记住那个二进制（`_FFMPEG_CACHE`），后续优先复用。
    """
    global _FFMPEG_CACHE, _FFMPEG_TRIED
    if _FFMPEG_TRIED and _FFMPEG_CACHE and os.path.exists(_FFMPEG_CACHE):
        return _FFMPEG_CACHE
    _FFMPEG_TRIED = True
    cands = _ffmpeg_candidates()
    _FFMPEG_CACHE = cands[0] if cands else None
    return _FFMPEG_CACHE


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


async def convert_to_silk_forced(path: str, logger_: Any = None,
                                 max_seconds: Any = None) -> Optional[str]:
    """**强制**把音频转成 silk（不管它现在是什么格式），返回新路径或 None。

    与 `to_silk_if_needed` 的区别：后者对"已经是 silk"会原样返回，
    而这个是"**我要一份 silk**"—— 给"mp3/ogg 原样发被平台拒了、需要重试"用。

    ``max_seconds``：语音条上限（秒）；给了就保证产物**不超过**它
    （超长部分剪掉并写日志）。``None`` ⇒ 用模块默认（`configure_trim` 注入）。

    ⚠ **不阻塞**：转码走 `asyncio.to_thread`（`_convert_sync` 里是 subprocess +
      pilk/pysilk 的同步调用），事件循环不受影响。
    """
    cap = _effective_cap(max_seconds)
    if not path or not os.path.isfile(path):
        return None
    if silk_magic_ok(path):
        if not _fix_tencent_header(path, logger_):
            return None
        # ★ 已经是 silk 也要过上限：外部工具（silk-wasm 等）产出的长语音会超 5 分钟
        return await asyncio.to_thread(_silk_with_cap, path, cap, logger_)
    if not silk_available():
        return None
    # 已有缓存就直接用（避免重复转）；★ 上限不同 ⇒ 产物不同 ⇒ key 带上上限
    key = _digest(path) + (f"|cap{cap:.3f}" if cap else "")
    hit = _CACHE.get(key)
    if hit and os.path.exists(hit[0]):
        return hit[0]
    out_dir = None
    try:
        out_dir = tempfile.mkdtemp(prefix="qqbot_silk_")
        silk = await asyncio.to_thread(_convert_sync, path, out_dir, cap)
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


def _truncate_silk(path: str, cap: float, logger_: Any):
    """**已经（合法）silk 的文件**超长时：解码量长度，真超长就截断 PCM 重编码。

    返回 ``(新 silk 路径, 临时目录)``；不需要剪 / 剪不了 ⇒ ``None``。

    ## 成本（**为什么热路径不付这笔钱**）

    * 先过**尺寸筛**：``size <= cap × 500 B/s`` 的 silk 不可能超长 ⇒ 直接放过。
      实测 silk ≈ 1.7 KB/s（10 秒 = 17KB），500 是保守下限 ⇒ 60 秒以内的
      语音（QQ 语音消息的最长档）全部走筛子，**不解码、零成本**；
    * 只有大文件才解码量长度（实测 **10 秒音频解码 13ms**，纯 C，很快）；
    * 真超长才重编码（PCM 截断到整数秒 ⇒ 产物时长**精确** = 上限）。

    fail-open：任何异常都放过原文件（绝不因为剪裁把一条语音搞丢）。
    """
    try:
        size = os.path.getsize(path)
    except Exception:
        return None
    if size <= cap * _SILK_MIN_BYTES_PER_SEC:
        return None                       # 便宜筛：这么点字节不可能超过 cap 秒
    # ★ 量过的长度也要缓存：大而不超长的 silk（1.5–5 分钟档）重复发送时
    #   不必每次都解码（实测 10 秒音频 13ms，5 分钟级就是几百毫秒）
    mkey = "silk|" + _digest(path)
    measured = _DUR_CACHE.get(mkey)
    if measured is not None and measured <= cap:
        return None                       # 上次量过：没超 ⇒ 直接放过
    encoder = _silk_encoder()
    if encoder not in ("pysilk", "pilk"):
        return None                       # 没有能解码的后端 ⇒ 放过
    out_dir = None
    try:
        import pysilk

        out_dir = tempfile.mkdtemp(prefix="qqbot_silkcut_")
        pcm_path = os.path.join(out_dir, "cut.pcm")
        with open(path, "rb") as fi, open(pcm_path, "wb") as fo:
            pysilk.decode(fi, fo, _SILK_RATE)
        got = os.path.getsize(pcm_path)
        cap_bytes = int(cap * _SILK_RATE) * 2
        _DUR_CACHE[mkey] = got / (_SILK_RATE * 2)      # ★ 记住这次量到的长度
        if len(_DUR_CACHE) > 256:                      # 有界，防长会话里无限增长
            _DUR_CACHE.clear()
        if got <= cap_bytes:
            _rm_tree(out_dir)
            return None                   # 量完发现没超 ⇒ 原样用（也不会留临时目录）
        keep = cap_bytes - (cap_bytes % 2)
        with open(pcm_path, "r+b") as f:
            f.truncate(keep)
        silk_path = os.path.join(out_dir, "cut.silk")
        if not _encode_silk(encoder, pcm_path, silk_path, _SILK_RATE):
            _rm_tree(out_dir)
            return None
        if not _fix_tencent_header(silk_path, logger_):
            _rm_tree(out_dir)
            return None
        if logger_ is not None:
            logger_.info(
                "[QQBOT-BRIDGE] 语音超长：已自动剪裁（原 %s → %s）——"
                "平台语音条上限 5 分钟（超 1 秒都会失败并退回文件卡片），多出的部分已丢弃。"
                "（voice_max_seconds 可调上限；voice_auto_trim=false 可关）",
                _fmt_secs(got / (_SILK_RATE * 2)), _fmt_secs(keep / (_SILK_RATE * 2)),
            )
        return silk_path, out_dir
    except Exception as exc:
        if out_dir:
            _rm_tree(out_dir)
        if logger_ is not None:
            logger_.debug("[QQBOT-BRIDGE] silk 剪裁失败（放过原文件）：%s", exc)
        return None


def _silk_with_cap(path: str, cap: Optional[float], logger_: Any) -> Optional[str]:
    """已合法 silk：``cap`` 生效且确实超长 ⇒ 返回剪裁后的新路径，否则返回**原路径**。

    带缓存（``_CACHE``，与转码产物同一张表、同一套淘汰/清理逻辑）：
    同一份超长音频重复发送时**不重复解码/重编码**。
    """
    if not cap:
        return path
    key = _digest(path) + f"|trim{cap:.3f}"
    hit = _CACHE.get(key)
    if hit and os.path.exists(hit[0]):
        return hit[0]
    made = _truncate_silk(path, cap, logger_)
    if not made:
        return path
    silk_path, out_dir = made
    _prune_cache()
    _CACHE[key] = (silk_path, out_dir)
    return silk_path


def _convert_sync(src: str, out_dir: str,
                  max_seconds: Any = None) -> Optional[str]:
    """同步转码：`src`（任意音频）→ silk 文件路径；失败返回 None。

    链路：**ffmpeg 解码成 PCM → silk 编码器编成 silk**。

    ★ 注意：**ffmpeg 压不出 silk**（它没有 silk 编码器），
      只负责第一步的 PCM 解码 —— 这一点已被实测确认。

    ★★ 2026-10-10 加固（用户线报 "5 分钟音频 ffmpeg 报
      `应用程序无法正常启动(0xc0000142)` 并把窗口卡住"）：
      * 解码步骤**遍历全部 ffmpeg 候选**（配置 → 环境变量 → PATH → imageio），
        遇到**启动类错误/超时**就换下一个候选（0xC0000142 是
        `STATUS_DLL_INIT_FAILED`，换一个二进制往往就能绕过）；
      * `-nostdin` + `stdin=DEVNULL`（经典卡死来源）+ Windows `CREATE_NO_WINDOW`；
      * 单次超时 300s → **120s**（弹窗卡住时更快退到"换候选/按文件发"，
        不给被动回复窗口添乱）；
      * 成功一次后就记住那个二进制，后续优先复用。
    """
    global _FFMPEG_CACHE
    encoder = _silk_encoder()
    if not encoder:
        logger.warning(
            "[QQBOT-BRIDGE] 没有可用的 silk 编码器（pysilk / pilk / silk_v3_encoder 都没有），"
            "无法把音频转成语音条 silk —— 本条会按「文件」发送。"
            "装依赖后即可自动转（插件已在 requirements.txt 声明 silk-python）"
        )
        return None

    cands = _ffmpeg_candidates()
    if not cands:
        logger.warning("[QQBOT-BRIDGE] 找不到 ffmpeg（imageio-ffmpeg 也没装上），无法转 silk")
        return None
    # 上次成功的二进制优先
    if _FFMPEG_CACHE and _FFMPEG_CACHE in cands:
        cands = [_FFMPEG_CACHE] + [c for c in cands if c != _FFMPEG_CACHE]

    base = os.path.splitext(os.path.basename(src))[0] or "voice"
    pcm_path = os.path.join(out_dir, base + ".pcm")
    silk_path = os.path.join(out_dir, base + ".silk")

    # ① 解码成 PCM。采样率必须和后面 pilk 的 pcm_rate 一致。
    #    24000 是 SILK 宽带模式（QQ/微信语音常用），单声道。
    #
    #    ★★ 2026-10-10：带上 `-t <语音条上限>` —— 超长音频**只解码前 N 秒**
    #      （用户实测：平台语音条上限 5 分钟，超 1 秒都失败、退回文件卡片）。
    #      没超时 `-t` 是 no-op（零成本）；超了就是免费截断（比解完再切还快）。
    rate = _SILK_RATE
    cap = _effective_cap(max_seconds)
    cap_clip = ["-t", f"{cap:.3f}"] if cap else []
    #: 命中上限（= 源比上限长）的判据：PCM 长度顶到上限（容差一个帧的量级）
    cap_bytes = int(cap * rate) * 2 if cap else None
    proc = None
    ok_decode = False
    for i, ffmpeg in enumerate(cands):
        cmd = [
            ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
            "-i", src,
            *cap_clip,
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(rate),
            pcm_path,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=FFMPEG_TIMEOUT,
                                  **_ffmpeg_spawn_kwargs())
        except subprocess.TimeoutExpired:
            logger.warning(
                "[QQBOT-BRIDGE] ffmpeg 解码超时（%ss，候选 %d/%d：%s）—— "
                "Windows 上常见于启动弹窗卡死（0xC0000142）；已杀掉，尝试下一个候选",
                FFMPEG_TIMEOUT, i + 1, len(cands), os.path.basename(str(ffmpeg)))
            proc = None
            continue
        except Exception as exc:
            logger.warning(
                "[QQBOT-BRIDGE] ffmpeg 启动失败（候选 %d/%d：%s）：%s: %s",
                i + 1, len(cands), os.path.basename(str(ffmpeg)),
                type(exc).__name__, str(exc)[:120])
            proc = None
            continue
        if proc.returncode == 0 and os.path.exists(pcm_path) and os.path.getsize(pcm_path) > 0:
            _FFMPEG_CACHE = ffmpeg          # 记住这个能用的
            ok_decode = True
            break
        if _ffmpeg_startup_failure(proc.returncode):
            logger.warning(
                "[QQBOT-BRIDGE] ffmpeg **启动类错误**（exit=0x%X，候选 %d/%d：%s）—— "
                "0xC0000142 = 应用程序无法正常启动（DLL 初始化失败，会弹窗卡住）；"
                "已换下一个候选",
                int(proc.returncode) & 0xFFFFFFFF, i + 1, len(cands),
                os.path.basename(str(ffmpeg)))
            proc = None
            continue
        # 普通解码错误（文件损坏/没有音轨）⇒ 换候选也没用，直接失败
        break

    if not ok_decode:
        _err = ""
        try:
            if proc is not None:
                _err = (proc.stderr or b"")[:180].decode(errors="replace")
        except Exception:
            _err = ""
        logger.warning(
            "[QQBOT-BRIDGE] ffmpeg 解码音频失败（%d 个候选都试过）%s —— "
            "若在 Windows 看到「应用程序无法正常启动(0xC0000142)」弹窗，"
            "建议装一个系统 ffmpeg（winget install Gyan.FFmpeg）并在插件配置里"
            "填 ffmpeg_path，或在环境变量里设 IMAGEIO_FFMPEG_EXE；本插件会自动优先使用",
            len(cands), ("：" + _err) if _err else "")
        return None

    # ★★ 命中上限 ⇒ 源比上限长：ffmpeg 已按 `-t` 截断（只解码了前 N 秒）。
    #   写清"原本多长 → 剪成多长"（原时长要单独探一次头，**只在真的剪裁时**才付这点成本）。
    if cap and cap_bytes:
        try:
            _got = os.path.getsize(pcm_path)
        except Exception:
            _got = 0
        if _got and _got >= max(0, cap_bytes - 4096):
            _src_len = probe_duration_sync(src)
            # ★ 源正好等于上限（或只差一个帧）⇒ 其实没剪掉什么，不谎报"已剪裁"；
            #   探测不到长度（None）时照常写日志（无法证伪，如实带个 "?"）。
            if _src_len is None or _src_len > cap + 0.05:
                logger.info(
                    "[QQBOT-BRIDGE] 语音超长：已自动剪裁（原 %s → %s）——"
                    "平台语音条上限 5 分钟（超 1 秒都会失败并退回文件卡片），多出的部分已丢弃。"
                    "（voice_max_seconds 可调上限；voice_auto_trim=false 可关）",
                    _fmt_secs(_src_len) if _src_len else "?", _fmt_secs(cap),
                )

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


async def to_silk_if_needed(path: str, logger_: Any = None,
                            max_seconds: Any = None) -> Optional[str]:
    """把音频转成 silk 文件路径；**已经是 silk 则原样返回**。

    ``max_seconds``：语音条上限（秒）。超长的音频会被自动剪裁到上限
    （见 `_convert_sync` / `_truncate_silk`）；``None`` ⇒ 用模块默认。
    返回 ``None`` 表示"转不了"（缺依赖 / 解码失败 / 源文件异常），
    调用方**应按原文件发送**（退化成文件），不要丢消息。
    """
    cap = _effective_cap(max_seconds)
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
        # ★ 已是合法 silk，但**也可能超长**（外部工具产出的长语音）⇒ 过一遍上限
        return await asyncio.to_thread(_silk_with_cap, path, cap, logger_)
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

    key = _digest(path) + (f"|cap{cap:.3f}" if cap else "")
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
        silk = await asyncio.to_thread(_convert_sync, path, out_dir, cap)
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
    _DUR_CACHE.clear()                    # ★ 时长/量长缓存一并清（测试与还原用）
    reset_ffmpeg_cache()
    reset_encoder_cache()
