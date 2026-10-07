"""★ 核心漏掉的媒体元素（`Record` / `Video`）由**插件侧**补回发送链。

## 线上现象（2026-10-07 用户实测）

模型发：

```xml
<msg><file type="record">data/temp/ncm_downloader/xxx.mp3</file></msg>
```

群里收到的却是 **`[Unsupported message element]`** —— 语音条根本没发出去。

## 根因：核心的媒体元素白名单里**没有 Record / Video**

两代都一样（`im.py:363` / `qq_official.py:462`）：

```python
media_elements = [element for element in send_message_obj
                  if isinstance(element, (File, Image))]     # ← 没有 Record / Video
```

⇒ `Record` 既**没被挑出来当媒体上传**，`_text_content` 也**不认识它**
（只认 `Text / At / Emoji / Reply / File / Image`），于是走 else 分支填空：

```python
parts.append("[Unsupported message element]")     # im.py:201
```

⇒ 最后发出去的就是这句占位文本。

## 为什么在插件侧修（不动核心）

用户明确要求 **不给 KiraAI 核心开 PR**，一切在插件侧处理。
这里的做法是**包装 `_text_content`**（本来就被我们包着，用于 @ 渲染）：

* 遇到 `Record` / `Video` / 其它媒体元素时，**不再填占位文本**，
  而是返回空串 —— 让 `content` 为空，同时由调用方把媒体补进
  `media_elements`（见 `media_types.py` 对 `_upload_file` 的包装 +
  这里的 `ensure_media_element`）。

  ⚠ 但「返回空串」会让核心判定 `not content and not media_elements` ⇒ 报
  "cannot send an empty message"。所以**必须同时**让媒体元素被认出来。

* 我们的 `_upload_file` 包装（`media_types.py`）已经能正确给
  `Video→2 / Record→3` 定 `file_type`，**只差把元素送进去**。

⇒ 本模块提供一个 **`coerce_media_chain(chain)`**：把链里的 `Record` / `Video`
**临时替换成等价的 `File`**（`File` 在核心白名单里，且 `_upload_file` 会按
我们包装后的规则给出正确的 `file_type`），发完再换回去。

这样**完全不碰核心**，核心的 `media_elements` 自然就认得它们了。
"""
from __future__ import annotations

from typing import Any, List, Tuple

__all__ = ["coerce_media_chain", "restore_media_chain", "MEDIA_TYPES"]

#: 需要「伪装成 File」才能被核心发送链认出来的元素类型
#: （核心白名单只有 File / Image；这两个是它漏掉的）
MEDIA_TYPES = ("Record", "Video")


def _type_name(obj: Any) -> str:
    return type(obj).__name__


def coerce_media_chain(chain: Any) -> Tuple[Any, List[Tuple[int, Any]]]:
    """把链里核心不认识的媒体元素**临时换成 `File`**，让核心愿意发送它。

    返回 ``(可能被改动过的 chain, [(下标, 原元素), ...])``；
    发完**务必**调用 `restore_media_chain` 换回去（我们不改用户的消息链）。

    为什么换 `File` 而不是换 `Image`：
      * 核心的 `file_type` 规则是「Image ⇒ 1，其余 ⇒ 4」；
      * 我们包装过的 `_upload_file` 会按**原始类型**给正确的值
        （Video⇒2 / Record⇒3），所以换成 `File` 不影响类型判定，
        却能让它进入核心的 `media_elements` 白名单。
    """
    if chain is None:
        return chain, []
    try:
        from core.chat.message_elements import File
    except Exception:
        return chain, []

    items = getattr(chain, "message_list", None)
    if not isinstance(items, list):
        return chain, []

    swapped: List[Tuple[int, Any]] = []
    for i, ele in enumerate(items):
        if _type_name(ele) not in MEDIA_TYPES:
            continue
        # File(file, name, size, mime) —— 从原元素上把信息搬过去
        try:
            f = File(
                getattr(ele, "file", None) or getattr(ele, "record", None),
                getattr(ele, "name", None),
                getattr(ele, "size", None),
                getattr(ele, "mime", None),
            )
            # ★ 记下原始类型，供我们的 `_upload_file` 包装定 file_type
            f._kira_bridge_orig_kind = _type_name(ele)
            items[i] = f
            swapped.append((i, ele))
        except Exception:
            continue
    return chain, swapped


def restore_media_chain(chain: Any, swapped: List[Tuple[int, Any]]) -> None:
    """把 `coerce_media_chain` 换掉的位置**还原**回去（务必在 finally 里调）。"""
    if not swapped:
        return
    items = getattr(chain, "message_list", None)
    if not isinstance(items, list):
        return
    for i, orig in swapped:
        try:
            if 0 <= i < len(items):
                items[i] = orig
        except Exception:
            pass
