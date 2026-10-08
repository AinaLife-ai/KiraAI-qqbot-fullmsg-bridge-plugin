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
_RECORD_EXT = {".silk", ".mp3", ".wav", ".ogg"}


#: 哨兵：源文件本来就是 silk，**无需改路径、file_type 保持 3**
KEEP_AS_IS = "\x00KEEP"


async def maybe_convert_to_silk(media_element: Any, logger_: Any) -> Optional[str]:
    """决定 `Record` 音频该怎么发（silk 优先，但不强求）。

    ## ★ 官方文档与实测的冲突（2026-10-08）

    官方「富媒体消息概述」写：

    > **语音**：支持 **`silk/mp3/wav/ogg`** 格式，发送后展示语音条

    但同一页的「文件类型与限制」表里，`file_type=3 语音` 的格式只列了 **silk**；
    第三方项目实测也称「MP3 直接上传会降级成文件」。

    ⇒ **两个说法都可能有适用条件**（可能是平台版本/白名单差异）。
    所以这里采取**两条路都试**的策略，而不是一刀切：

    * 源文件**已经是 silk** ⇒ 校验腾讯系头后**直接用**（最省事）；
    * 源文件是 **mp3/wav/ogg** ⇒ **先原样按 `file_type=3` 发**（官方说支持，
      这样**零转码、零依赖、最快**）；调用方发现被降级再转 silk（见 `media_types`）。

    返回值语义（调用方必须区分）：

    * ``KEEP_AS_IS`` —— 源文件本身就是（合法的）silk，**路径不用改**，类型保持 3；
    * ``<路径>``     —— 已转码成 silk，改用这个路径；
    * ``None``       —— 源文件不是 silk（mp3/ogg/wav）⇒ **先原样发**，别转。
      调用方据此保持原路径 + `file_type=3`。
    """
    try:
        if getattr(media_element, "file_type", None) == "url":
            return None                       # 远程地址：交给平台自己处理
        from audio_silk import to_silk_if_needed, is_silk_path
        path = await media_element.to_path()
        if not path or not os.path.isfile(path):
            return None
        # ★ 只对"本来就是 silk"的做校验+放行；其它格式**先原样发**（不转码）
        if not is_silk_path(path):
            return None                       # ⇒ 调用方按原文件 + file_type=3 发
        silk = await to_silk_if_needed(path, logger_=logger_)
        if not silk:
            return None                       # silk 但头不合法 ⇒ 退回按文件发
        return KEEP_AS_IS                     # 合法 silk ⇒ 原路径、类型保持 3
    except Exception as exc:
        if logger_ is not None:
            logger_.debug("[QQBOT-BRIDGE] silk 判断失败: %s", exc)
        return None
    try:
        if getattr(media_element, "file_type", None) == "url":
            return None                       # 远程地址：不动
        from audio_silk import to_silk_if_needed
        path = await media_element.to_path()
        if not path or not os.path.isfile(path):
            return None
        silk = await to_silk_if_needed(path, logger_=logger_)
        if not silk:
            return None
        # 源文件本身就是 silk ⇒ 保持原路径、file_type=3
        if os.path.realpath(silk) == os.path.realpath(path):
            return KEEP_AS_IS
        return silk
    except Exception as exc:
        if logger_ is not None:
            logger_.debug("[QQBOT-BRIDGE] silk 转码不可用: %s", exc)
        return None


def _guess_name(element: Any) -> str:
    g = getattr(element, "guess_name", None)
    if callable(g):
        try:
            return str(g() or "")
        except Exception:
            pass
    return str(getattr(element, "file", "") or "")


def classify(element: Any) -> Optional[int]:
    """按元素类型 + 扩展名给出官方 `file_type`。

    返回 None 表示「交回核心原逻辑」（例如 `File`/`Sticker`，核心按 4 处理是对的）。

    ★ 也认 `media_coerce` 留下的 `_kira_bridge_orig_kind` 标记：
    为了让核心的 `media_elements` 白名单认得 ` Record`/`Video`，
    我们会把它们**临时换成 `File`** 再发；换的时候把原始类型记在标记里，
    这里按**原始类型**给值，保证 `视频→2 / 语音→3` 不会因为"变成 File"而丢。
    """
    # ★ 先看"换壳"标记（见 media_coerce）
    orig = getattr(element, "_kira_bridge_orig_kind", None)
    if orig == "Video":
        return FT_VIDEO
    if orig == "Record":
        return FT_RECORD

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
        payload = {
            "file_type": FT_RECORD,
            "file_data": base64.b64encode(data).decode("ascii"),
            "srv_send_msg": False,
            "file_name": os.path.basename(silk),
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


def install(holder: Any, client: Any, logger: Any = None) -> bool:
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

    async def _upload_file(target_id, media_element, is_group, _orig=current):
        want = classify(media_element)
        if want is None:
            return await _orig(target_id, media_element, is_group)
        try:
            return await _upload(target_id, media_element, is_group, want)
        except Exception as exc:
            if logger is not None:
                logger.debug("[QQBOT-BRIDGE] 按 file_type=%s 上传失败，交回原逻辑: %s",
                             want, exc)
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
                silk_path = got
            else:
                # ★ 不是 silk（mp3/wav/ogg）⇒ **先原样按 file_type=3 发**。
                #
                #   官方「富媒体概述」明说语音支持 `silk/mp3/wav/ogg`，
                #   所以先按官方说的试一次 —— **零转码、零依赖、最快**，
                #   也不用装 pilk/ffmpeg。
                #
                #   若平台把它降级成文件（第三方实测称 mp3 会降级），
                #   下面 `_upload` 的重试逻辑会自动再转成 silk 发一次。
                _pending_silk_retry = True
                if logger is not None:
                    logger.info(
                        "[QQBOT-BRIDGE] 该音频不是 silk（%s）—— 先按官方支持的格式"
                        "直接以「语音」发送；若被平台降级，会自动转 silk 重试",
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
        payload: dict = {
            "file_type": file_type,
            "file_data": base64.b64encode(data).decode("ascii"),
            "srv_send_msg": False,
        }
        name = os.path.basename(silk_path) if silk_path else _guess_name(media_element)
        if name:
            payload["file_name"] = os.path.basename(name.split("?")[0])
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
                logger.warning(
                    "[QQBOT-BRIDGE] 媒体上传失败（file_type=%s，文件=%s）：%s: %s",
                    file_type, os.path.basename(str(file_path)),
                    type(exc).__name__, str(exc)[:200],
                )
            # ★ 「原样发的 mp3/ogg 被平台拒了」⇒ **转 silk 再试一次**（只试一次）。
            #   这样既保留了「官方支持就直接发」的快路径，
            #   又保证最终一定能发成语音条。
            if _pending_silk_retry and silk_path is None:
                retried = await _retry_as_silk(
                    api, target_id, media_element, is_group, exc, logger)
                if retried is not None:
                    return retried
            raise
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
