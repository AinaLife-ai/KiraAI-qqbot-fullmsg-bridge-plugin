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

#: 核心发送链**认不出来**的媒体元素（白名单只有 File / Image）。
#:
#: * `Record` / `Video` ⇒ 换成等价的 `File`（我们包装过的 `_upload_file` 会按原始类型
#:   给 `file_type=3/2`）；
#: * `Sticker`（框架内置表情包插件的 `<sticker>` 产出，本质也是一张图）
#:   ⇒ 换成等价的 **`Image`**（`file_type=1`，QQ 直接当图片展示）。
MEDIA_TYPES = ("Record", "Video", "Sticker")


def _type_name(obj: Any) -> str:
    return type(obj).__name__


#: 默认的"表情包类"关键词（可被插件配置覆盖）：类名里含它就当图片发
DEFAULT_IMAGE_KEYWORDS = ("sticker",)


def coerce_media_chain(chain: Any, image_keywords: Any = DEFAULT_IMAGE_KEYWORDS
                       ) -> Tuple[Any, List[Tuple[int, Any]]]:
    """把链里核心不认识的媒体元素**临时换成 `File` / `Image`**，让核心愿意发送它。

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
        from core.chat.message_elements import File, Image
    except Exception:
        return chain, []

    items = getattr(chain, "message_list", None)
    if not isinstance(items, list):
        return chain, []

    kws = tuple(str(k).lower() for k in (image_keywords or ()) if str(k).strip())

    def _is_image_like(name: str) -> bool:
        """类名里含配置的"表情包关键词" ⇒ 当图片发（默认包含 sticker）。"""
        low = name.lower()
        return any(k in low for k in kws)

    swapped: List[Tuple[int, Any]] = []
    for i, ele in enumerate(items):
        name = _type_name(ele)
        if name not in MEDIA_TYPES and not _is_image_like(name):
            continue
        # 把信息从原元素搬过去（两个类的构造签名不同，统一用关键字参数）
        kind = _type_name(ele)
        raw = (getattr(ele, "file", None) or getattr(ele, "record", None)
               or getattr(ele, "sticker", None))
        try:
            if kind != "Record" and kind != "Video":
                # Image(image, mime, name, caption) —— 注意第 2 个位置参数是 mime！
                f = Image(image=raw, mime=getattr(ele, "mime", None),
                          name=getattr(ele, "name", None))
            else:
                f = File(raw, getattr(ele, "name", None),
                         getattr(ele, "size", None), getattr(ele, "mime", None))
            # ★ 记下原始类型，供我们的 `_upload_file` 包装定 file_type
            f._kira_bridge_orig_kind = kind
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
