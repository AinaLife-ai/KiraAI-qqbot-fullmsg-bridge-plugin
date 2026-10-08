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
    """`Record` 音频若不是 silk，就转成 silk 并返回**新文件路径**。

    返回值语义（调用方必须区分）：

    * ``KEEP_AS_IS`` —— 源文件本来就是 silk，**路径不用改**，`file_type` 保持 3；
    * ``<路径>``     —— 转码成功，**改用这个路径**上传；
    * ``None``       —— 转不了（缺依赖 / 解码失败 / 远程地址）
      ⇒ 调用方**按文件（file_type=4）发**，宁可降级也不失败。

    ★ 只处理**本地文件**；远程 URL 交给平台自己处理。
    """
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
        if file_type == FT_RECORD:
            got = await maybe_convert_to_silk(media_element, logger)
            if got == KEEP_AS_IS:
                pass                          # 本来就是 silk，路径/类型都不动
            elif got:
                silk_path = got
            else:
                # 转不了 ⇒ 按「文件」发（用户仍能下载/播放，不会丢消息）
                file_type = FT_FILE
                if logger is not None:
                    logger.info(
                        "[QQBOT-BRIDGE] 无法把音频转成 silk（缺依赖或解码失败）——"
                        "本条按「文件」发送；装上 pilk + imageio-ffmpeg 后即可自动转成语音条",
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
        return await api._http.request(route, json=payload)

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
