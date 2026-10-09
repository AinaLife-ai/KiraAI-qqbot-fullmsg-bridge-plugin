"""让 QQ 官方 bot 的 markdown **真的显示出图片** —— 且**不破坏 md 格式**。

## 背景：官方三条硬约束（全部有文档出处）

1. **md 内图片只吃公网 URL**
   ：「对于 markdown 消息内的图片资源，请使用**可在公网访问的资源 url**，
     开放平台会下载转存该资源。」
2. **`msg_type` 互斥**
   ：`0=纯文本(content) / 2=Markdown(markdown) / 7=富媒体(media)`，
     「传了 markdown 后此字段必须为空」⇒ **图文无法在一条消息里**。
3. **富媒体上传只返回 `file_info`**（一串不透明二进制），
   ⇒ 本地图片**无法**变成 md 能引用的地址。

## 但官方留了一个口子（本模块的关键）

上传接口的响应字段里有：

    raw_url  string  文件下载链接（COS 预签名 GET URL），有效期与 ttl 一致
             ★ 仅分片上传合并（upload_id 路径）且 file_type 为图片/视频/语音时返回

⇒ **走「分片上传」路线，能拿到一个公网可访问的 COS URL**。

于是本地图片也能这样发：

    ![香香](data/temp/compressed_xxx.jpg)      ← md 里原本是本地路径（QQ 看不见）
    ![香香](https://cos.xxx/xxx?sign=...)      ← 换成分片上传拿到的公网 URL

**md 的标题 / 列表 / 引用 / 链接 / 代码块全部原样保留** —— 只是把方括号里那个
URL 换成一个能用的，**不拆消息、不改结构**。

## 另外：URL 图片要先「验真」

实测用户给过的一个维基地址：

    https://zh.wikipedia.org/wiki/Special:FilePath/Luka_Megurine.png
    → HTTP 302, content-type: text/html, content-length: 0   ← 下载到 0 字节

**那不是图片地址，是跳转地址**。QQ 下载器拿到空 HTML，校验失败，
就回退成 alt 文字（`![香香](...)` 渲染成「[香香]」）。
官方错误码 `850026`「下载原始文件失败——请检查 URL 是否可访问」正是这个。

所以本模块还有一个 `resolve_image_url()`：**先 HEAD/GET 探一下**，
若是跳转就跟随到真实图片地址；若确实不是图片，就如实告诉调用方
（而不是把坏地址塞进 md 让它又变成文字）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

#: markdown 图片语法：`![alt](url)`，alt 里可能带 `#208px #320px` 尺寸标记
_MD_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: 判定"这已经是公网地址"，不需要转存
_REMOTE_RE = re.compile(r"^https?://", re.I)

#: QQ 的 markdown 图片**尺寸后缀**：`alt #宽 #高`（可以是 px，也可以只写数字）。
#: 官方示例：`![text #208px #320px]`、`![img#618px #249px]`。
#: ⚠ 只用来**识别**模型自己写的尺寸，**我们不去伪造尺寸**（见 `_ensure_size`）。
_SIZE_RE = re.compile(r"#[^#\]]*(?:px)?\s*#[^#\]]*(?:px)?\s*$")


def _ensure_size(alt: str, width: Optional[int] = None,
                 height: Optional[int] = None) -> str:
    """给图片 alt 补上 QQ 要求的**真实尺寸** `#Wpx #Hpx`。

    ## ★★★ 为什么必须带尺寸、而且必须填**真实值**（2026-10-08 用户实测）

    用户给了一个**没装本插件**的 KiraAI 3.0 成功示例 —— 它用的是
    **QQ 官方文档里那个示例原样**：

        ![text #208px #320px](https://resource5-…/building.png)
                     ^^^^^^^^^^ 官方示例本身就是带尺寸的

    官方文档「图片」节的原话：

        图片：`![text #wpx #hpx](图片链接)`
        **必须带尺寸**，否则可能加载失败

    ### 我踩过的两个坑（都别再犯）

    1. **不带尺寸** ⇒ 群里只显示 `[alt 文字]`（图渲染不出来）；
    2. **带 `#0 #0`** ⇒ 群里**连占位都没有**（QQ 把它当**真实 0×0 像素**，
       渲染成零尺寸 ⇒ 完全不可见）。

    ⇒ 所以**必须填图片的真实宽高**。本地图用 Pillow 读；
    远程图从下载到的字节里读（见 `fix_markdown_images`）。

    拿不到尺寸时**保持原样**（不加）—— 宁可维持"有占位"的现状，
    也不能塞个假尺寸把它变成"完全不可见"。
    """
    a = (alt or "").rstrip()
    if _SIZE_RE.search(a):
        return a                       # 模型/用户已经写了尺寸 ⇒ 尊重它
    if not width or not height:
        return a                       # 拿不到真实尺寸 ⇒ **不加**（别塞假值）
    return f"{a} #{int(width)}px #{int(height)}px".strip()


def _image_size_from_bytes(data: bytes) -> Optional[Tuple[int, int]]:
    """从图片字节里读真实宽高；读不出返回 None。

    Pillow 可用就用 Pillow（格式支持最全），否则退回手写 PNG/JPEG 头解析
    （这两种覆盖了绝大多数情况，且**零依赖**）。
    """
    if not data:
        return None
    try:
        import io
        from PIL import Image as _PILImage
        with _PILImage.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w and h:
                return int(w), int(h)
    except Exception:
        pass
    return _size_from_header(data)


def _size_from_header(data: bytes) -> Optional[Tuple[int, int]]:
    """不依赖 Pillow 的兜底：直接读 PNG / JPEG / GIF / WEBP 文件头。"""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return (w, h) if w and h else None
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            n = len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = int.from_bytes(data[i + 2:i + 4], "big")
                # SOF0..SOF3 / SOF5..SOF7 / SOF9..SOF11 / SOF13..SOF15
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                               0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return (w, h) if w and h else None
                i += 2 + seg_len
            return None
        if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
            w = int.from_bytes(data[6:8], "little")
            h = int.from_bytes(data[8:10], "little")
            return (w, h) if w and h else None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
            fmt = data[12:16]
            if fmt == b"VP8X":
                w = int.from_bytes(data[24:27], "little") + 1
                h = int.from_bytes(data[27:30], "little") + 1
                return w, h
            if fmt == b"VP8 " and len(data) >= 30:
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                return (w, h) if w and h else None
    except Exception:
        pass
    return None

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def _is_remote(url: str) -> bool:
    return bool(_REMOTE_RE.match(url or ""))


def _guess_mime(path: str) -> str:
    return _MIME_BY_EXT.get(os.path.splitext(path)[1].lower(), "image/jpeg")


def sniff_image_mime(data: bytes, name: str = "") -> str:
    """从**字节魔数**判断图片 MIME（扩展名只作兜底）。

    ## ★★★ 为什么必须按字节嗅探（2026-10-08 定案）

    分片上传时，**每个分片 PUT 的 `Content-Type` 决定平台把文件存成什么类型**。
    不带头的话 COS / 平台会把对象存成 `application/octet-stream`，
    于是 QQ 的 markdown 图片链路判定"这不是图片" ⇒ 前端显示 **加载失败**。

    ⇒ 我们按**真实字节**给出 `image/png` / `image/jpeg` / … ，
    扩展名只在认不出魔数时兜底（比如 IMG 里塞了别的格式）。
    """
    head = data[:16] if data else b""
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if head[:2] == b"BM":
        return "image/bmp"
    return _guess_mime(name or "image.jpg")


def _verify_public_url(url: str, logger: Any = None, timeout: float = 8.0) -> None:
    """回抓一次刚上传的直链，把 **HTTP 状态 / Content-Type / 大小** 记进日志。

    ## 为什么要多这一趟（2026-10-08 教训）

    用户反馈"图片还是加载失败"时，日志里**只有"已转存为公网地址"这一句正面信息** ——
    完全看不出那条 `raw_url` 到底是好的、403 了、还是被存成了 `octet-stream`。
    排查只能靠猜（这已经浪费过两轮）。

    这一步只做**观测**，不改任何行为：
      * 200/206 + `image/*` ⇒ 打印一次确认；
      * 其余（403 / octet-stream / 超时）⇒ **WARNING**，把原因写清楚。

    ⚠ 用 **GET + `Range: bytes=0-1`** 而不是 HEAD：
      预签名 URL 的签名**包含 HTTP 方法**（预签名的是 GET），
      HEAD 必然 403 —— 那是"我们自己的探测方式"出错，不是链接坏了，
      反而会把排查方向带偏。
    """
    if not url:
        return
    try:
        import urllib.request

        req = urllib.request.Request(url, method="GET")
        req.add_header("Range", "bytes=0-1")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            status = getattr(resp, "status", 200)
            ctype = (resp.headers.get("Content-Type") or "").strip()
            length = resp.headers.get("Content-Range") or resp.headers.get("Content-Length")
        if logger is not None:
            if status in (200, 206) and ctype.lower().startswith("image/"):
                logger.info(
                    "[QQBOT-BRIDGE] 直链自检通过：HTTP %s Content-Type=%s（%s）",
                    status, ctype, length or "?",
                )
            else:
                logger.warning(
                    "[QQBOT-BRIDGE] 直链自检异常：HTTP %s Content-Type=%s（%s）—— "
                    "QQ 侧多半会渲染成「加载失败」；若是 octet-stream，"
                    "说明分片上传没带图片 Content-Type",
                    status, ctype or "无", length or "?",
                )
    except Exception as exc:
        if logger is not None:
            logger.warning(
                "[QQBOT-BRIDGE] 直链自检失败（%s: %s）—— 仅作观测，不影响本条发送；"
                "若 QQ 里显示「加载失败」请把这条一起反馈",
                type(exc).__name__, str(exc)[:120],
            )


# --------------------------------------------------------------------------- #
# 结果缓存 —— 避免「每次发 markdown 都重新验真 / 重新上传」
# --------------------------------------------------------------------------- #
#: url → (最终公网 url, 过期时刻)。验真结果缓存 10 分钟；
#: 本地文件的转存结果缓存到 ttl（QQ 的 raw_url 自带有效期，取小值保守些）。
_URL_CACHE: dict = {}
_LOCAL_CACHE: dict = {}
_URL_TTL = 600.0
_LOCAL_TTL = 240.0


def _cache_get(cache: dict, key: str):
    import time
    hit = cache.get(key)
    if not hit:
        return None
    val, exp = hit
    if time.monotonic() >= exp:
        cache.pop(key, None)
        return None
    return val


def _cache_put(cache: dict, key: str, val: Any, ttl: float) -> None:
    import time
    cache[key] = (val, time.monotonic() + ttl)


def _digest(path: str) -> str:
    """用 (路径, 大小, mtime) 做键 —— 文件被换掉时自然会重新上传。"""
    try:
        st = os.stat(path)
        return f"{path}|{st.st_size}|{int(st.st_mtime)}"
    except Exception:
        return path


def clear_caches() -> None:
    """清空缓存（测试 / 配置变更时用）。"""
    _URL_CACHE.clear()
    _LOCAL_CACHE.clear()


# --------------------------------------------------------------------------- #
# 一、公网 URL 验真
# --------------------------------------------------------------------------- #
async def resolve_image_url(client: Any, url: str, logger: Any = None,
                            timeout: float = 12.0) -> Optional[str]:
    """把「可能是跳转/非图片」的 URL 解析成**真正可直接下载的图片地址**。

    返回真实图片 URL；确认不是图片时返回 None（调用方据此如实告警，不要硬塞）。

    为什么需要（2026-10-07 用户实测）：QQ 的 md 下载器不做 HTML 解析，
    拿到 302 / text/html 就判定失败，于是 `![香蛋](坏地址)` 渲染成「[香蛋]」。
    """
    if not url:
        return None
    try:
        import aiohttp
    except Exception:  # pragma: no cover
        return url          # 没有 aiohttp 就不验（保持原行为，不阻断）
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; qqbot-bridge/1.0)"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            # 先 HEAD（省流量）；有些图床不支持 HEAD，失败再 GET 读前几字节
            ctype = None
            ctype2 = None
            try:
                async with s.head(url, headers=headers, allow_redirects=True) as r:
                    ctype = r.headers.get("Content-Type")
                    final = str(r.url)
                    if r.status == 200 and ctype and ctype.startswith("image/"):
                        return final
            except Exception:
                pass
            try:
                async with s.get(url, headers=headers, allow_redirects=True) as r:
                    ctype2 = r.headers.get("Content-Type")
                    final = str(r.url)
                    head = await r.content.read(16)
                    if r.status == 200 and ctype2 and ctype2.startswith("image/"):
                        return final
                    # 魔数兜底：有些图床 Content-Type 不准
                    if head[:8] == b"\x89PNG\r\n\x1a\n" or head[:3] == b"\xff\xd8\xff" \
                            or head[:6] in (b"GIF87a", b"GIF89a"):
                        return final
            except Exception:
                pass
            if logger is not None:
                logger.warning(
                    "[QQBOT-BRIDGE] 图片地址不是可直接下载的图片（Content-Type=%s/%s）"
                    "——QQ 会把它渲染成 alt 文字。若这是跳转地址，请改用真实图片 URL",
                    ctype, ctype2,
                )
            return None
    except Exception:
        return url


# --------------------------------------------------------------------------- #
# 二、本地图片 → 公网 COS URL（走分片上传，拿 raw_url）
# --------------------------------------------------------------------------- #
async def _route_request(api: Any, route: Any, **kwargs: Any) -> Any:
    """调用 botpy 的底层 HTTP（框架就是这么干的）。"""
    return await api._http.request(route, **kwargs)


# --------------------------------------------------------------------------- #
# 分片上传的重试策略（**逐条对齐官方实现**，2026-10-09）
# --------------------------------------------------------------------------- #
#: 官方 `@tencent-connect/qqbot-nodejs` 的 `retry.ts`：
#:   * `UPLOAD_RETRY_POLICY`          = maxRetries 2 / base 1000ms / 指数退避（prepare、合并）
#:   * `COMPLETE_UPLOAD_RETRY_POLICY` = maxRetries 2 / base **2000ms** / 指数退避（合并那一步）
#:   * `PART_FINISH_RETRY_POLICY`     = maxRetries 2 / base 1000ms / 指数退避
#:   * `buildPartFinishPersistentPolicy`：命中 **40093001** 时进入**持久重试**，
#:     间隔 1s，时限 = prepare 下发的 `retry_timeout`（默认 120s，上限 **600s**）
#:   * `UPLOAD_PREPARE_FALLBACK_CODE = 40093002`（日额度）⇒ **不重试**
#: Hermes 的 `gateway/platforms/qqbot/chunked_upload.py` 是同一套数字（Python 版），
#: 两边逐项一致 ⇒ 这里照抄，不自创参数。
_UPLOAD_RETRIES = 2              # 共 3 次尝试
_UPLOAD_BASE_DELAY = 1.0
_COMPLETE_BASE_DELAY = 2.0
_PART_FINISH_INTERVAL = 1.0
_PART_FINISH_DEFAULT_TIMEOUT = 120.0
_PART_FINISH_MAX_TIMEOUT = 600.0
_PART_PUT_TIMEOUT = 300.0        # 单个分片 PUT 的超时（官方 300s）
_DAILY_LIMIT_CODE = "40093002"   # 日额度：不重试，直接如实上报
_PART_RETRYABLE_CODE = "40093001"  # 分片确认的可重试码：进持久重试

#: **整个上传（含全部重试）的软预算**，必须小于图片处理的总预算
#: （`_IMAGE_BUDGET_SECONDS = 45`）——否则重试会把整条消息拖死。
#: 到点了就不再重试，按"转存失败"处理（消息照发，图退化成原地址）。
_UPLOAD_SOFT_BUDGET = 35.0

#: 参数类错误重试没有意义（官方 `UPLOAD_RETRY_POLICY.shouldRetry` 里就是按
#: "400 / 401 / Invalid / timeout" 这几个关键字**排除**的）。
#: 不值得重试的错误特征。
#: ★ 2026-10-09：加"格式不支持" —— qq-botpy 的 ServerError **只有 message 没有码**
#: （"富媒体文件格式不支持"），原来靠 "400" 字符串判断，对这种异常完全失配，
#: 导致"格式被拒"被当作网络抖动**反复重试 3 次**（用户日志实锤：合并分片重试 3 次）。
_NO_RETRY_MARKERS = ("401", "403", "invalid", "timeout", "timed out", "超时", "格式不支持")


def _api_retryable(exc: BaseException, *, allow_code: str = "") -> bool:
    """这次 API 失败值不值得重试（对齐官方 `shouldRetry` 的语义）。"""
    text = str(exc)
    low = text.lower()
    if _DAILY_LIMIT_CODE in text:
        return False                   # 日额度：重试只会浪费额度
    if allow_code and allow_code in text:
        return True                    # 显式允许的码（40093001）
    if "400" in text:
        return False                   # 参数类 400（含 850019/850026/850031…）
    if any(m in low for m in _NO_RETRY_MARKERS):
        return False
    return True                        # 5xx / 网络抖动 / 未知 ⇒ 重试一次看看


async def _with_retry(fn: Any, *, label: str, max_retries: int = _UPLOAD_RETRIES,
                      base_delay: float = _UPLOAD_BASE_DELAY, deadline: Any = None,
                      should_retry: Any = None, logger: Any = None,
                      retry_any: bool = False) -> Any:
    """按官方策略重试一个异步调用（指数退避 + **预算感知**）。

    :param deadline: `loop.time()` 语义的绝对时间；到点就不再重试（宁可失败也不拖死）。
    :param retry_any: True 时任何异常都重试（官方分片 PUT 就是这种：不带分类）。
    """
    last: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as exc:
            last = exc
            if attempt >= max_retries:
                raise
            if not retry_any and should_retry is not None and not should_retry(exc):
                raise
            delay = base_delay * (2 ** attempt)
            if deadline is not None:
                remain = deadline - asyncio.get_running_loop().time()
                if remain <= 0:
                    raise
                delay = min(delay, max(0.0, remain))
            if logger is not None:
                logger.warning(
                    "[QQBOT-BRIDGE] %s 第 %d 次失败（%.1fs 后重试，共 %d 次机会）：%s",
                    label, attempt + 1, delay, max_retries + 1, str(exc)[:120],
                )
            await asyncio.sleep(delay)
    raise last if last is not None else RuntimeError(label)   # pragma: no cover


async def _call_part_finish(api: Any, route: Any, body: dict, *, label: str,
                            retry_timeout: float, deadline: Any = None,
                            logger: Any = None) -> Any:
    """`upload_part_finish`：**先按官方快策略重试，命中 40093001 再进持久重试**。

    持久重试的时限取 prepare 下发的 `retry_timeout`（默认 120s、上限 600s），
    同时不得越过我们自己的软预算 —— 这是官方策略 + 我们的"不拖死消息"约束的折中。
    """
    try:
        return await _with_retry(
            lambda: _route_request(api, route, json=body),
            label=label, max_retries=_UPLOAD_RETRIES, base_delay=_UPLOAD_BASE_DELAY,
            deadline=deadline, logger=logger,
            should_retry=lambda e: _api_retryable(e, allow_code=_PART_RETRYABLE_CODE),
        )
    except Exception as exc:
        if _PART_RETRYABLE_CODE not in str(exc):
            raise
        loop = asyncio.get_running_loop()
        start = loop.time()
        limit = min(float(retry_timeout or _PART_FINISH_DEFAULT_TIMEOUT),
                    _PART_FINISH_MAX_TIMEOUT)
        if deadline is not None:
            limit = min(limit, max(0.0, deadline - start))
        attempt = 0
        while True:
            elapsed = loop.time() - start
            if elapsed >= limit:
                raise
            attempt += 1
            delay = min(_PART_FINISH_INTERVAL, max(0.0, limit - elapsed))
            if logger is not None:
                logger.warning(
                    "[QQBOT-BRIDGE] %s 命中可重试错误（%s），进入持久重试 #%d"
                    "（已 %.0fs / 上限 %.0fs）：%s",
                    label, _PART_RETRYABLE_CODE, attempt, elapsed, limit, str(exc)[:100],
                )
            await asyncio.sleep(delay)
            try:
                return await _route_request(api, route, json=body)
            except Exception as exc2:
                if _PART_RETRYABLE_CODE not in str(exc2):
                    raise
                exc = exc2


# --------------------------------------------------------------------------- #
# ★★★ 动图策略（2026-10-09 新增）：候选链「原图 → APNG → 静态 PNG」
# --------------------------------------------------------------------------- #
#
# 背景：平台上传接口只收 png/jpg，GIF 直传实测被拒（850019）；
# 而官方文档又把 gif 列进了"图片"支持格式（`富媒体消息概述`：
# 「支持 jpg/png/gif/webp/bmp 格式，发送后直接展示图片」）——口径矛盾。
# APNG 是"魔数仍是 PNG"的保底（平台一定收），但**客户端是否播放动画不可控**
# （用户实测：显示出来只有一帧）。
# ⇒ 既然"哪个形态能真动"没人能打包票，就让**平台自己挑**：
#   先试原始动图（唯一有机会真动的形态），被拒再 APNG、最后静态 PNG。
#   每一级都写日志 —— 下次线上哪个通道能用，一眼可见。
#   `md_gif_mode=static` 可回到"直接转存 APNG/PNG"的保守行为。

#: 动图策略（由插件配置注入；auto=试原图优先 / static=直接转存）
_MD_GIF_MODE = "auto"

#: 原始动图被平台拒过（(md5) -> 时间戳）；10 分钟内不再重复试，
#: 免得同一条动图每发一次就白撞一次 850019。有界（最多 64 条）。
_RAW_GIF_REJECTED: dict = {}
_RAW_GIF_REJECT_TTL = 600.0


def set_md_gif_mode(mode: str) -> None:
    """插件在初始化/巡检时注入动图策略（幂等）。

    取值：
      * ``auto``   —— 候选链「原图 → APNG → 静态 PNG」（默认；保证显示，动画看平台脸色）；
      * ``url``    —— 远程动图**保留原始公网地址**（平台自己下载转存，绕开上传接口的
                      格式限制；本地动图仍走候选链）；
      * ``static`` —— 跳过原图直传，直接 APNG/静态 PNG（最保守）。
    """
    global _MD_GIF_MODE
    m = str(mode or "auto").strip().lower()
    _MD_GIF_MODE = m if m in ("auto", "url", "static") else "auto"


def get_md_gif_mode() -> str:
    return _MD_GIF_MODE


def _count_frames(data: bytes) -> int:
    """动图帧数（读不出来按 1 帧算）。"""
    try:
        import io

        from PIL import Image as _PILImage

        with _PILImage.open(io.BytesIO(data)) as im:
            return int(getattr(im, "n_frames", 1) or 1)
    except Exception:
        return 1


def _make_upload_candidates(data: bytes, name: str, logger: Any):
    """按当前策略给出转存候选链：[(标签, bytes, 文件名), ...]。

    * png/jpeg ⇒ 原样一条（零开销）；
    * 动图（gif/webp 多帧）且 auto ⇒ 原图 → APNG → 静态 PNG（逐级退守）；
    * 其它非 png/jpg（webp 静图 / bmp / …）⇒ 一次规范化（静态 PNG / 可转 APNG）。
    """
    try:
        from media_types import normalize_image_data, sniff_image_format
    except Exception:
        return [("", data, name)]
    fmt = sniff_image_format(data)
    if fmt in ("png", "jpeg") or not data:
        return [("", data, name)]

    base_name = name or "image.png"
    if fmt in ("gif", "webp") and _count_frames(data) > 1:
        if _MD_GIF_MODE == "auto":
            import hashlib as _h
            import time as _t

            key = _h.md5(data).hexdigest()
            ts = _RAW_GIF_REJECTED.get(key, 0.0)
            fresh = (_t.time() - ts) < _RAW_GIF_REJECT_TTL
            cands = [] if fresh else [("原始动图（保动画优先）", data, base_name)]
            apng, apng_name, _n1 = normalize_image_data(data, base_name, logger,
                                                        allow_anim=True)
            if apng is not data and not any(apng == c[1] for c in cands):
                cands.append(("APNG（动图版 PNG）", apng, apng_name))
            png, png_name, _n2 = normalize_image_data(data, base_name, logger,
                                                      allow_anim=False)
            if png is not data and not any(png == c[1] for c in cands):
                cands.append(("静态 PNG（第一帧）", png, png_name))
            return cands or [("", data, base_name)]
        # static：跳过原图，直接 APNG → PNG
        cands = []
        apng, apng_name, _n1 = normalize_image_data(data, base_name, logger,
                                                    allow_anim=True)
        if apng is not data:
            cands.append(("APNG（动图版 PNG）", apng, apng_name))
        png, png_name, _n2 = normalize_image_data(data, base_name, logger,
                                                  allow_anim=False)
        if png is not data and not any(png == c[1] for c in cands):
            cands.append(("静态 PNG（第一帧）", png, png_name))
        return cands or [("", data, base_name)]

    # 非动图：一次规范化（保持 v1.6.8 行为）
    norm, nname, _note = normalize_image_data(data, base_name, logger)
    if norm is data:
        return [("", data, base_name)]
    return [("PNG 规范化", norm, nname)]


async def _upload_bytes_to_qq(
    client: Any, target_id: str, is_group: bool, data: bytes, name: str,
    logger: Any = None, file_type: int = 1,
) -> Optional[str]:
    """把**字节内容**上传到 QQ，返回公网可访问的 COS URL（`raw_url`）。

    走官方「分片上传」路线 —— **只有这条路**才会返回 `raw_url`：

        upload_prepare  →  upload_id / block_size / parts[].presigned_url
        分片 PUT 到 presigned_url
        upload_part_finish（逐片确认）
        上传接口（带 upload_id）合并  →  file_info **+ raw_url**

    ★ 2026-10-09：按「候选链」逐级退守（见 `_make_upload_candidates`）——
    动图先试**原图**（有机会真动），被平台以格式为由拒收就换 APNG，再不行静态 PNG。
    每个候选成功/被拒都会写日志，哪条路通、哪条路不通一清二楚。

    失败返回 None（调用方按原样发送，绝不会因此丢掉整条消息）。
    """
    if not data:
        return None
    try:
        api = getattr(client, "api", None)
        if api is None:
            return None
    except Exception:
        return None

    candidates = await asyncio.to_thread(_make_upload_candidates, data, name, logger)
    last_exc: Optional[BaseException] = None
    for _i, (label, cdata, cname) in enumerate(candidates):
        try:
            url = await _upload_one_bytes(client, target_id, is_group, cdata,
                                          cname, logger=logger, file_type=file_type)
        except Exception as exc:
            from media_types import is_format_error as _is_fmt

            if _is_fmt(exc):
                last_exc = exc
                # 「原图被平台拒」要记下来：10 分钟内不再对同一张图白撞
                if label.startswith("原始动图"):
                    try:
                        import hashlib as _h
                        import time as _t

                        _RAW_GIF_REJECTED[_h.md5(data).hexdigest()] = _t.time()
                        if len(_RAW_GIF_REJECTED) > 64:
                            _oldest = sorted(_RAW_GIF_REJECTED.items(),
                                             key=lambda kv: kv[1])[:16]
                            for _k, _ in _oldest:
                                _RAW_GIF_REJECTED.pop(_k, None)
                    except Exception:
                        pass
                nxt = candidates[_i + 1][0] if _i + 1 < len(candidates) else None
                if logger is not None:
                    if nxt:
                        logger.info(
                            "[QQBOT-BRIDGE] 动图候选「%s」被平台拒收（%s）—— 换下一个（%s）",
                            label or "原样", str(exc)[:70], nxt)
                    else:
                        logger.warning(
                            "[QQBOT-BRIDGE] 动图候选「%s」被平台拒收（%s）—— 没有更多候选了",
                            label or "原样", str(exc)[:70])
                continue
            if logger is not None:
                logger.warning("[QQBOT-BRIDGE] 转存上传失败（%s: %s）",
                               type(exc).__name__, str(exc)[:120])
            return None
        if url:
            if logger is not None and label:
                if label.startswith("原始动图"):
                    logger.info(
                        "[QQBOT-BRIDGE] ★ 动图**原图直传成功**（平台收了 %s）——"
                        "md 里这条**有机会真动**；若客户端仍显示静图，把 md_gif_mode 设为 static 可回退",
                        name or "动图")
                    # 清掉"曾被拒"的旧记忆（平台侧行为可能已变化）
                    try:
                        import hashlib as _h

                        _RAW_GIF_REJECTED.pop(_h.md5(data).hexdigest(), None)
                    except Exception:
                        pass
                else:
                    logger.info(
                        "[QQBOT-BRIDGE] 动图已按「%s」转存成功（逐步退守的结果）", label)
            return url
        # 返回 None 且无异常（例如响应里没有 raw_url）：
        # 还有候选就继续换（可能"原图"路径给不出 raw_url 而"转档"路径可以）；
        # 最后一个候选也拿不到 ⇒ 停（失败如实返回 None，由调用方按原样处理）。
        if _i + 1 < len(candidates):
            if logger is not None:
                logger.info("[QQBOT-BRIDGE] 候选「%s」转存未拿到 raw_url —— 换下一个",
                            label or "原样")
            continue
        if logger is not None and len(candidates) > 1:
            logger.warning("[QQBOT-BRIDGE] 所有候选都没拿到 raw_url —— 本条按原地址/alt 处理")
        return None
    if logger is not None:
        logger.warning("[QQBOT-BRIDGE] 动图所有候选都被平台拒收（最后：%s）—— 本条按原地址/alt 处理",
                       str(last_exc)[:100] if last_exc else "?")
    return None


async def _upload_one_bytes(
    client: Any, target_id: str, is_group: bool, data: bytes, name: str,
    logger: Any = None, file_type: int = 1,
) -> Optional[str]:
    """单个候选的实际转存（分片上传 → raw_url）。逻辑与 v1.6.8 相同。"""
    try:
        from botpy.http import Route
    except Exception:
        return None
    api = getattr(client, "api", None)
    if api is None or not data:
        return None
    size = len(data)
    name = name or "image.png"
    md5 = hashlib.md5(data).hexdigest()
    sha1 = hashlib.sha1(data).hexdigest()
    md5_10m = hashlib.md5(data[:10002432]).hexdigest()

    # ★★★ 分片 PUT 必须带**图片 Content-Type**（2026-10-08 定案，见 sniff_image_mime）。
    #   不带 ⇒ 平台存成 octet-stream ⇒ QQ 认为"不是图片" ⇒ md 里显示「加载失败」。
    #   实测对照：官方文档那条能正常渲染的示例图是 `image/png`，
    #   而 QQ 自己转存出来的对象是 `application/octet-stream`（用户日志实证）。
    mime = sniff_image_mime(data, name)
    part_headers = {"Content-Type": mime}
    #: 整个上传（含全部重试）的软预算 —— 到点就不再重试，避免把消息拖死
    deadline = asyncio.get_running_loop().time() + _UPLOAD_SOFT_BUDGET

    try:
        if is_group:
            prep_route = Route("POST", "/v2/groups/{group_id}/upload_prepare",
                               group_id=target_id)
            finish_route = Route("POST", "/v2/groups/{group_id}/upload_part_finish",
                                 group_id=target_id)
            files_route = Route("POST", "/v2/groups/{group_openid}/files",
                                group_openid=target_id)
        else:
            prep_route = Route("POST", "/v2/users/{user_id}/upload_prepare",
                               user_id=target_id)
            finish_route = Route("POST", "/v2/users/{user_id}/upload_part_finish",
                                 user_id=target_id)
            files_route = Route("POST", "/v2/users/{openid}/files", openid=target_id)

        # ★ prepare 也重试（官方 UPLOAD_RETRY_POLICY：2 次 / 1s 指数退避）
        prep = await _with_retry(
            lambda: _route_request(api, prep_route, json={
                "file_type": int(file_type),   # 1=图片 2=视频 3=语音 4=文件
                "file_size": str(size),
                "file_name": name,
                "md5": md5,
                "sha1": sha1,
                "md5_10m": md5_10m,
            }),
            label="upload_prepare", deadline=deadline, logger=logger,
            should_retry=_api_retryable,
        )
        # prepare 会下发分片确认的持久重试时限（官方：默认 120s、上限 600s）
        _cfg = _get(prep, "upload_config") or {}
        try:
            retry_timeout = float(_get(_cfg, "retry_timeout") or _PART_FINISH_DEFAULT_TIMEOUT)
        except Exception:
            retry_timeout = _PART_FINISH_DEFAULT_TIMEOUT
        upload_id = _get(prep, "upload_id")
        parts = _get(prep, "parts") or []
        if not upload_id or not parts:
            if logger is not None:
                logger.debug("[QQBOT-BRIDGE] 预上传没拿到 upload_id/parts: %s", prep)
            return None

        import aiohttp
        # ★★★ 分片必须**按 index 排序、用各自 block_size 累加偏移**。
        #
        #   踩过的坑（2026-10-07 线上 850019「富媒体文件格式不支持」）：
        #   原来写成 `chunk = data[idx * bsize:(idx+1)*bsize]`，而**最后一片的
        #   block_size 比前面小**（例：12 MB 文件按 5 MB 分片 ⇒ [5MB, 5MB, 2MB]），
        #   第 2 片就会算成 `data[4MB:6MB]`（应该是 10MB 起）⇒ 拼出来的文件是坏的
        #   ⇒ 平台合并后校验格式失败，回 400 / 850019，图还是显示不出来。
        #
        # ★ 2026-10-08 又踩一次：修上面这个 bug 时，把 `async with ... as sess`
        #   那行连同旧循环一起删掉了，却没补回来 ⇒ 运行时 `NameError: name 'sess'
        #   is not defined` ⇒ **图片转存整条链路全废**（用户日志抓到的）。
        #   教训：**改缩进/搬代码块时，一定要确认外层上下文（with / try / 变量）
        #   还在**，光看语法通过没用 —— NameError 是运行时才炸的。
        ordered = [p for p in parts if _get(p, "index") is not None]
        ordered.sort(key=lambda p: int(_get(p, "index")))
        # ★ 分片 index **可能不从 0 开始**（AstrBot 用 `part_index_base = min(index)`
        #   来算偏移，说明平台不保证从 0 起）。按基准值算偏移更稳。
        base = int(_get(ordered[0], "index")) if ordered else 0
        offset = 0
        # 单次请求超时对齐官方（PART_UPLOAD_TIMEOUT_MS = 300s）；
        # 真正兜底的是上面的 deadline（35s 软预算），所以不会真的等 300s
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_PART_PUT_TIMEOUT)) as sess:
            for part in ordered:
                idx = int(_get(part, "index"))
                purl = _get(part, "presigned_url")
                bsize = int(_get(part, "block_size") or 0)
                if not purl:
                    continue
                # bsize<=0 时按「剩下的全部」兜底（不该发生，但不至于拼错）
                chunk = data[offset:offset + bsize] if bsize > 0 else data[offset:]
                if not chunk:
                    break
                offset += len(chunk)
                # ★ 带上 Content-Type：预签名 URL 只签了 host，
                #   多带一个头不会破签，但它**决定平台存下来是什么类型**。
                #
                # ★★ 每个分片 PUT 都要重试（官方 PART_UPLOAD_MAX_RETRIES=2 × 1s 指数退避，
                #    单次超时 300s；Hermes 同款）。原来**一个都没重试** ——
                #    网络抖一下整条转存就失败，用户只看到"图又发不出来"。
                async def _put_once(_purl=purl, _chunk=chunk):
                    async with sess.put(_purl, data=_chunk, headers=part_headers) as r:
                        if r.status >= 300:
                            raise RuntimeError(f"COS PUT returned HTTP {r.status}")

                await _with_retry(_put_once, label=f"分片 {idx} PUT",
                                  deadline=deadline, logger=logger, retry_any=True)
                # 分片确认：先快重试，命中 40093001 进持久重试
                await _call_part_finish(
                    api, finish_route, {
                        "upload_id": upload_id,
                        "part_index": idx,
                        "block_size": str(len(chunk)),
                        "md5": hashlib.md5(chunk).hexdigest(),
                    },
                    label=f"upload_part_finish#{idx}",
                    retry_timeout=retry_timeout, deadline=deadline, logger=logger,
                )

        # ★ 完整性自检：拼出来的必须和原文件一字不差，否则**宁可不传**
        #   （传个坏文件上去只会换来一个看不懂的平台错误）。
        if offset != len(data):
            if logger is not None:
                logger.warning(
                    "[QQBOT-BRIDGE] 分片拼装不完整（%s/%s 字节），已放弃转存，按原样发送",
                    offset, len(data),
                )
            return None

        merged = await _with_retry(
            lambda: _route_request(api, files_route, json={
                "file_type": int(file_type),
                "srv_send_msg": False,
                "file_name": name,
                "url": "",              # 分片合并路径可留空，但字段要带上
                "upload_id": upload_id,
            }),
            label="合并分片（/files）", max_retries=_UPLOAD_RETRIES,
            base_delay=_COMPLETE_BASE_DELAY, deadline=deadline, logger=logger,
            should_retry=_api_retryable,
        )
        raw = _get(merged, "raw_url")
        if raw:
            if logger is not None:
                logger.info(
                    "[QQBOT-BRIDGE] 图片已转存为公网地址（Content-Type=%s），markdown 可直接引用",
                    mime,
                )
            # 观测（不阻塞事件循环）：回抓一次，把状态/类型写进日志
            await asyncio.to_thread(_verify_public_url, str(raw), logger)
            return str(raw)
        if logger is not None:
            logger.debug("[QQBOT-BRIDGE] 合并响应没有 raw_url: %s", merged)
        return None
    except Exception as exc:
        # ★ 这条**必须可见**（原来是 debug）：
        #   转存失败 ⇒ 图还是 alt 文字。若不提示，用户只会看到"图片又不显示"，
        #   然后来问"为什么"——而日志里什么都没有。线上就被这个坑过一次
        #   （`400 / 850019 富媒体文件格式不支持`，因为分片拼装错了）。
        if logger is not None:
            try:
                from media_types import humanize_upload_error

                _hint = humanize_upload_error(exc)
            except Exception:
                _hint = ""
            logger.warning(
                "[QQBOT-BRIDGE] 图片转存到 QQ 失败（%s: %s）%s",
                type(exc).__name__, str(exc)[:160],
                ("\n    → " + _hint) if _hint else "",
            )
        # ★★★ 2026-10-09：**必须重新抛出** —— 外层候选链要靠异常类型做退守
        #   （`850019` ⇒ 换下一个候选：原图→APNG→静态 PNG）。
        #   原来这里 `return None` 把格式类错误也吞成了"无声的 None"，
        #   候选链根本看不到"被平台拒收"这件事 ⇒ 直接放弃、不会退守。
        raise


async def upload_local_to_public_url(
    client: Any, target_id: str, is_group: bool, file_path: str,
    logger: Any = None,
) -> Optional[str]:
    """本地图片 → 公网 COS URL。读文件后交给 `_upload_bytes_to_qq`。"""
    try:
        with open(file_path, "rb") as f:
            data = f.read()
    except Exception as exc:
        if logger is not None:
            logger.debug("[QQBOT-BRIDGE] 读本地图片失败: %s", exc)
        return None
    return await _upload_bytes_to_qq(client, target_id, is_group, data,
                                     os.path.basename(file_path), logger=logger)


async def upload_remote_to_public_url(
    client: Any, target_id: str, is_group: bool, url: str,
    logger: Any = None, timeout: float = 30.0, want_size: bool = False,
):
    """**远程图片** → 转存到 QQ 自己的 COS，返回公网 URL。

    ## ★★★ 为什么要做这件事（2026-10-07 的教训）

    官方文档说「md 内图片请使用**可在公网访问**的资源 url，开放平台会下载转存」，
    但实测**公网 + 国内 + 200 + 真图片**照样失败：

        用户日志：萌娘百科国内站直链（HEAD 200、content-type: image/png）
                 ⇒ 群里依旧只显示 [巡音流歌 V4X]（alt 文字）

    原因是**平台的转存是异步的、失败只回一个错误码给日志**（`304010 CHANGE_IMAGE_URL
    图片转存错误` / `304021 GET_FILE 下载文件错误` / `304020 FILE_SIZE 文件大小超限`），
    前端拿不到任何反馈 ⇒ 用户只看到 alt 文字。用户那张 `Luka1.jpg` 有 **13.7 MB**，
    很可能就是撞了大小限制。

    ⇒ **最可靠的做法：我们自己把图取下来，走 QQ 自己的上传通道转存一次**
    （`raw_url` 是 QQ 自己的 COS 预签名地址，平台去下载它必然成功），
    顺便还能把过大的图**压缩**到软限制内。
    """
    if not url:
        return (None, None) if want_size else None
    data = await _fetch_bytes(url, logger=logger, timeout=timeout)
    if not data:
        return (None, None) if want_size else None
    # ★ 尺寸要在**压缩之前**读（压缩会缩尺寸），这样填进去的是原图真实宽高
    size = _image_size_from_bytes(data)
    # 太大的图先压缩（官方软限制：图片 20 MB，超过会降级成"文件"）
    data, name = await _shrink_if_needed(data, url, logger=logger)
    pub = await _upload_bytes_to_qq(client, target_id, is_group, data, name,
                                    logger=logger)
    if want_size:
        return pub, size
    return pub


async def _fetch_bytes(url: str, logger: Any = None,
                       timeout: float = 30.0) -> Optional[bytes]:
    """下载远程图片字节。失败返回 None（调用方原样发送）。

    ★ 失败一定**打 WARNING 并带上原因**（2026-10-07 教训）：
      线上出过「维基共享的图明明 200，日志却只说 `Content-Type=None/None`」——
      因为 `Content-Type` 只是**拿到响应之后**才有；连不上时它是 None，
      日志里却看不出到底是**超时**、**403** 还是**被墙**，用户没法排查。

      现在把「HTTP 状态码 / Content-Type / 异常类型 / 是否超时」都写清楚，
      用户把日志发来就能一眼定位。
    """
    if not url:
        return None
    try:
        import aiohttp
    except Exception:
        return None

    # ★ 带浏览器 UA：不少图床 / CDN（Wikimedia 就是）对陌生 UA 直接 403，
    #   而 403 的响应体是 text/html，会被误判成"这不是图片"。
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.get(url, headers=headers, allow_redirects=True) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                if r.status != 200:
                    if logger is not None:
                        logger.warning(
                            "[QQBOT-BRIDGE] 下载图片失败：HTTP %s（Content-Type=%s）—— %s",
                            r.status, ctype or "无", url,
                        )
                    return None
                data = await r.content.read()
                head = data[:16]
                is_img = ctype.startswith("image/") or head[:8] == b"\x89PNG\r\n\x1a\n" \
                    or head[:3] == b"\xff\xd8\xff" or head[:6] in (b"GIF87a", b"GIF89a") \
                    or (head[:4] == b"RIFF" and data[8:12] == b"WEBP")
                if not is_img:
                    if logger is not None:
                        logger.warning(
                            "[QQBOT-BRIDGE] 该地址返回的不是图片（HTTP 200，"
                            "Content-Type=%s，前面字节=%s）—— %s；"
                            "可能它其实是网页/跳转页，请换成图片直链",
                            ctype or "无", head[:4].hex() or "空", url,
                        )
                    return None
                return data
    except Exception as exc:
        if logger is not None:
            _timeout = "timeout" in type(exc).__name__.lower() or "Timeout" in str(exc)
            logger.warning(
                "[QQBOT-BRIDGE] 下载图片异常%s：%s: %s —— %s（若你的网络访问不了该站，"
                "QQ 平台多半也访问不了，建议换成国内可直连的图床）",
                "（超时）" if _timeout else "",
                type(exc).__name__, str(exc)[:120], url,
            )
        return None


async def _shrink_if_needed(data: bytes, url: str, logger: Any = None,
                            soft_limit: int = 20 * 1024 * 1024) -> tuple:
    """超过官方软限制（图片 20 MB）就压一下 —— 避免被降级成"文件"。"""
    name = os.path.basename(url.split("?")[0]) or "image.jpg"
    if len(data) <= soft_limit:
        return data, name
    try:
        import io
        from PIL import Image as PILImage
    except Exception:
        return data, name
    try:
        def _do() -> bytes:
            im = PILImage.open(io.BytesIO(data))
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            quality = 85
            for _ in range(4):
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=quality, optimize=True)
                out = buf.getvalue()
                if len(out) <= soft_limit:
                    return out
                quality -= 15
            # 还大就缩尺寸
            im.thumbnail((1600, 1600))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80, optimize=True)
            return buf.getvalue()

        import asyncio
        out = await asyncio.to_thread(_do)
        if logger is not None:
            logger.info("[QQBOT-BRIDGE] 图片过大已压缩：%.1f MB → %.1f MB",
                        len(data) / 1048576, len(out) / 1048576)
        return out, os.path.splitext(name)[0] + ".jpg"
    except Exception as exc:
        if logger is not None:
            logger.debug("[QQBOT-BRIDGE] 压缩图片失败（按原图上传）: %s", exc)
        return data, name


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# --------------------------------------------------------------------------- #
# 三、把 md 里的图片地址「就地换掉」
# --------------------------------------------------------------------------- #
async def fix_markdown_images(
    md_text: str, *, client: Any, target_id: str, is_group: bool,
    adapter: Any = None, logger: Any = None,
) -> str:
    """把 markdown 里的图片地址换成**QQ 能真正下载到的公网地址**。

    * **本地路径** → 分片上传拿 `raw_url`（公网 COS）；
    * **公网 URL** → 先验真（跟随跳转 / 查 Content-Type）；
      确实不是图片就**保持原样**（不静默删改用户内容），并给出告警。

    返回**结构完全不变**的 markdown（只替换了 `(...)` 里的 URL）。
    """
    if not md_text or "![" not in md_text:
        return md_text

    matches = list(_MD_IMAGE_RE.finditer(md_text))
    if not matches:
        return md_text

    # ★★★ **并行**解析所有图片（原来是串行 for ⇒ 3 张图最坏要等 3 倍时间）。
    #
    #   为什么可以并行：每张图的处理（下载 / 验证 / 上传）互相独立，
    #   而且各自都有超时；并发上限压到 `_MAX_CONCURRENT`，避免一次几十张时
    #   把网络打满或触发平台限流（上传接口官方限 10 QPS）。
    #
    #   注意：**替换仍然按原顺序**（下面按 index 写回），
    #   所以 md 结构、图片顺序、行数全都不变。
    sem = asyncio.Semaphore(_MAX_CONCURRENT)

    async def _resolve_one(idx: int, alt: str, url: str) -> Tuple[int, str, Optional[Tuple[int, int]]]:
        """返回 `(下标, 新URL, 尺寸或None)`。

        ★ 尺寸必须**在这里一起拿到**（2026-10-08 定案）：
          * 本地图 → 直接读文件算尺寸；
          * 远程图 → 转存时我们**已经把字节下载下来过**，顺手算一次，零额外开销；
          * 拿不到 → None（下游就**不加尺寸**，保持原样）。
        """
        async with sem:
            try:
                new_url, size = await _resolve_image_url(
                    url, client=client, target_id=target_id, is_group=is_group,
                    adapter=adapter, logger=logger, want_size=True)
                return idx, new_url, size
            except Exception as exc:
                if logger is not None:
                    logger.debug("[QQBOT-BRIDGE] 处理第 %d 张图失败（原样保留）: %s", idx, exc)
                return idx, url, None

    results = await _gather_with_budget(
        {i: _resolve_one(i, m.group("alt"), m.group("url"))
         for i, m in enumerate(matches)},
        logger=logger)
    new_urls = {idx: url for idx, url, _sz in results}
    sizes = {idx: sz for idx, _url, sz in results}

    out = []
    last = 0
    changed = 0
    for i, m in enumerate(matches):
        out.append(md_text[last:m.start()])
        alt, url = m.group("alt"), m.group("url")
        new_url = new_urls.get(i, url)
        if new_url != url:
            changed += 1
        _sz = sizes.get(i)
        _w, _h = _sz if _sz else (None, None)
        out.append(f"![{_ensure_size(alt, _w, _h)}]({new_url})")
        last = m.end()
    out.append(md_text[last:])

    if logger is not None and ("![" in md_text):
        _with = sum(1 for i, _m in enumerate(matches) if sizes.get(i))
        logger.info(
            "[QQBOT-BRIDGE] markdown 图片处理：共 %d 张，%d 张换成了公网地址，%d 张补上了真实尺寸"
            "（QQ 的 md 图片**必须**带 `#宽px #高px`，否则不渲染）",
            len(matches), changed, _with,
        )
    return "".join(out)


#: 一条消息里同时处理几张图（多了会打满网络 / 撞上传接口 10 QPS 限流）
_MAX_CONCURRENT = 4

#: ★ 整条消息的图片处理**总预算**（秒）。
#:
#   为什么必须有：图片处理是**内联在发送路径上**的 ——
#   每张图最坏是「下载 30s + 分片上传 120s」，6 张图串成 ceil(6/4)×150 ≈ 5 分钟，
#   而 **QQ 被动回复窗口只有 5 分钟**（过期就发不出去）。
#   所以宁可**超时就保留原地址**（至少消息发得出去），
#   也不能让转存把整条消息拖死。
_IMAGE_BUDGET_SECONDS = 45.0


async def _gather_with_budget(tasks: Dict[int, Any], logger: Any = None) -> List[Any]:
    """`asyncio.gather` + **总超时预算**：超时的那些任务**按原样保留**。

    `tasks` 是 `{下标: 协程}`；返回 `[(下标, 结果url), ...]`。
    超时的任务返回**它自己的原始 url**（由调用方在 `except` 分支保证），
    这里只把没跑完的**取消掉**并让调用方按原样发送。

    为什么要预算：图片处理是**内联在发送路径上**的 ——
    每张图最坏「下载 30s + 分片上传 120s」，6 张图能串到 ≈5 分钟，
    而 **QQ 被动回复窗口只有 5 分钟**（过期就整条发不出去）。
    宁可超时后保留原地址（消息至少发得出去），也不能让转存把消息拖死。
    """
    if not tasks:
        return []
    # ★ 必须自己造 Task：`asyncio.gather` 内部会把协程包成 Task，
    #   但**我们手里拿到的仍是协程对象**（没有 `.done()`）——
    #   超时后想检查"哪些跑完了"就必须先自己 `ensure_future`。
    #   （踩过：直接对协程调 `.done()` ⇒ AttributeError）
    wrapped = {idx: asyncio.ensure_future(c) for idx, c in tasks.items()}
    try:
        done = await asyncio.wait_for(
            asyncio.gather(*wrapped.values(), return_exceptions=False),
            timeout=_IMAGE_BUDGET_SECONDS,
        )
        return list(done)
    except asyncio.TimeoutError:
        if logger is not None:
            logger.warning(
                "[QQBOT-BRIDGE] 图片转存超过 %.0f 秒预算（本条 %d 张）——"
                "未完成的按原地址发送，保证消息能发出去",
                _IMAGE_BUDGET_SECONDS, len(tasks),
            )
        out: List[Any] = []
        for idx, t in wrapped.items():
            if t.done() and not t.cancelled() and t.exception() is None:
                out.append(t.result())
            else:
                t.cancel()
        return out


async def _remote_animated_keep_url(client: Any, url: str, logger: Any):
    """``md_gif_mode=url``：远程**动图**保留原公网地址（让平台自己下载转存）。

    返回 ``(最终URL, 尺寸)``；返回 None = "这条不适用"（拉不到 / 不是动图 / 过大），
    调用方原样落回常规"下载 → 转存"路径（保证显示）。

    背景（2026-10-09 用户情报）：QQ 里能看到别的机器人发出**会动的 GIF**。
    官方文档「富媒体消息概述」把 gif 列进了图片支持格式，但**上传接口**只收
    png/jpg（GIF 直传实测 850019）——两处口径矛盾。md 图片走的是**平台侧的
    下载转存管道**（"开放平台会下载转存该资源"），与上传接口的限制无关，
    是"让平台自己挑格式"的另一条路。此模式只对**确实的动图**生效：
    我们仍然把图拉下来验证一次（确认多帧动图 + 量出真实尺寸，尺寸是 md
    图片的必需项），但**不转存**，原址交给平台。
    """
    try:
        data = await _fetch_bytes(url, logger=logger)
        if not data:
            return None
        if len(data) > 20 * 1024 * 1024:
            if logger is not None:
                logger.info(
                    "[QQBOT-BRIDGE] 动图超过图片软限制（20MB）—— 不使用『保留原址』"
                    "（平台大概率转存失败），改走转存/压缩路径")
            return None
        fmt = ""
        frames = 1
        try:
            from media_types import sniff_image_format

            fmt = sniff_image_format(data)
            import io as _io

            from PIL import Image as _PILImage

            with _PILImage.open(_io.BytesIO(data)) as im:
                frames = int(getattr(im, "n_frames", 1) or 1)
        except Exception:
            return None
        if frames <= 1 or fmt not in ("gif", "webp"):
            return None                     # 静图 / 认不出 ⇒ 走常规转存（稳）
        size = _image_size_from_bytes(data)
        final = url
        try:
            got = await resolve_image_url(client, url, logger=None)
            if got:
                final = got
        except Exception:
            pass
        if logger is not None:
            logger.info(
                "[QQBOT-BRIDGE] 动图走『保留原址』（md_gif_mode=url）：%s（%s，%d 帧，"
                "%.1f KB）——平台会自己下载转存；若群里显示不出/不动，把 md_gif_mode 改回 auto",
                url, fmt, frames, len(data) / 1024.0)
        return final, size
    except Exception:
        return None


async def _resolve_image_url(url: str, *, client: Any, target_id: str,
                             is_group: bool, adapter: Any, logger: Any,
                             want_size: bool = False):
    """把**一个**图片地址解析成 QQ 能下载到的地址；任何失败都原样返回。

    `want_size=True` 时返回 `(新URL, (宽,高) 或 None)` —— 尺寸用来补
    QQ md 图片**必需**的 `#Wpx #Hpx` 后缀（见 `_ensure_size`）。
    """
    if not url:
        return (url, None) if want_size else url
    if _is_remote(url):
        # ★★★ 远程图也**转存到 QQ 自己的 COS**（不再只是"验真"）。
        #
        #   为什么（用户实测）：官方说「用公网 URL，平台会下载转存」，
        #   但**公网 + 国内 + 200 + 真图片**照样失败 —— 萌娘百科国内站直链
        #   （HEAD 200、content-type: image/png）在群里依旧只显示 alt 文字。
        #   平台转存是**异步**的、失败只回错误码（304010 CHANGE_IMAGE_URL /
        #   304021 GET_FILE / 304020 FILE_SIZE），前端完全拿不到反馈。
        #
        #   ⇒ 我们自己取下来，走 QQ 自己的上传通道转存一次：
        #     raw_url 是 QQ 自己的 COS 预签名地址，平台去下载它**必然成功**；
        #     顺便还能把过大的图压到软限制内。
        cached = _cache_get(_URL_CACHE, url)
        if cached is None:
            # ★ md_gif_mode=url（2026-10-09 新增）：远程**动图**保留原公网地址，
            #   让平台自己下载转存 —— 绕开上传接口只收 png/jpg 的限制。
            #   拉不到 / 不是动图 / 过大 ⇒ 返回 None，原样落回下面的常规转存路径。
            if get_md_gif_mode() == "url":
                try:
                    got = await _remote_animated_keep_url(client, url, logger)
                except Exception:
                    got = None
                if got is not None:
                    _cache_put(_URL_CACHE, url, got, _URL_TTL)
                    _fu, _fs = got
                    if want_size:
                        return _fu, _fs
                    return _fu
            pub = None
            size = None
            try:
                # ★ 转存时我们**本来就要把字节下载下来**，顺手把真实尺寸也算出来，
                #   零额外开销（尺寸是 QQ md 图片的**必需项**，见 `_ensure_size`）。
                pub, size = await upload_remote_to_public_url(
                    client, target_id, is_group, url, logger=logger, want_size=True)
            except Exception as exc:
                if logger is not None:
                    logger.debug("[QQBOT-BRIDGE] 远程图转存失败: %s", exc)
            if pub:
                cached = pub
            else:
                # 转存不成 ⇒ 退回"验真"：能确认真图就保留原地址（也许平台能转成功）
                real = await resolve_image_url(client, url, logger=logger)
                cached = real or url
            _cache_put(_URL_CACHE, url, (cached, size), _URL_TTL)
        elif isinstance(cached, tuple):
            cached, size = cached
        else:
            size = None
        if want_size:
            return cached, size
        return cached

    # 本地路径：转存成公网 URL（按 (路径,大小,mtime) 缓存，避免重复上传）
    path = url
    if not os.path.isabs(path):
        base = getattr(adapter, "workspace", None) or os.getcwd()
        cand = os.path.join(str(base), path)
        if os.path.exists(cand):
            path = cand
    if not os.path.exists(path):
        return (url, None) if want_size else url
    key = _digest(path)
    cached = _cache_get(_LOCAL_CACHE, key)
    if cached is None:
        pub = await upload_local_to_public_url(
            client, target_id, is_group, path, logger=logger)
        cached = pub or url
        _cache_put(_LOCAL_CACHE, key, cached, _LOCAL_TTL)
    if want_size:
        return cached, _local_image_size(path)
    return cached


def _local_image_size(path: str) -> Optional[Tuple[int, int]]:
    """读本地图片的真实宽高（Pillow 优先，失败退回读文件头）。"""
    try:
        import io
        from PIL import Image as _PILImage
        with _PILImage.open(path) as im:
            w, h = im.size
            if w and h:
                return int(w), int(h)
    except Exception:
        pass
    try:
        with open(path, "rb") as f:
            return _image_size_from_bytes(f.read(65536))
    except Exception:
        return None
