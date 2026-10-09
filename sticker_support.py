"""让 QQ 官方 bot 能**发表情包**（框架内置表情包插件的 `<sticker>` 标签）。

## 两个卡点（都在插件侧解决，不动核心）

### ① 标签**根本没被注册**

`core/plugin/builtin_plugins/sticker/main.py`（v3.0.0-alpha.3 写法；更早版本为
`self.ctx.sticker_manager`，A3 保留了这个旧名兼容别名，本插件两个名字都认）：

```python
async def inject_sticker_tag(self, event, _, tag_set):
    supported_elements = event.supported_elements
    if "sticker" in supported_elements:          # ← 门槛
        sticker_dict = self.ctx.sticker_mgr.sticker_dict
        tag_set.register(build_sticker_tag(sticker_dict=sticker_dict))
```

而 **QQ 官方适配器声明的类型清单里没有 `sticker`**：

* 3.0 `QQOfficialIMCapability._SUPPORTED_ELEMENTS = ["text","img","at","reply","record","file","video","emoji"]`
* 2.x `QQOfficialAdapter.message_types = ["text","img","at","reply","record","file","video","emoji"]`

⇒ 表情包插件**不会**给 QQ 会话注册 `<sticker>`：模型看不到这个标签（提示词里没有），
就算自己写出来也不会被解析 ⇒ 用户看到的就是"表情包发不出来"。

### ② 就算解析成 `Sticker` 元素，也发不出去

适配器的富媒体白名单只认 `File` / `Image`：

```python
media_elements = [element for element in send_message_obj if isinstance(element, (File, Image))]
```

`Sticker`（`core/chat/message_elements.py`，也是 `BaseMediaElement`，本质就是一张图：
`sticker_id` + `file`）不在里面 ⇒ 不会被上传；`_text_content` 还会把它写成
`[Unsupported message element]`。

⇒ 由 `media_coerce` 把它**临时换成等价的 `Image`** 发送（`file_type=1`，QQ 直接当图片展示）。

## ⚠ 容易搞混的一点：**"门槛词"和"标签名"是两个东西**

| | 谁写的 | 是什么 |
|---|---|---|
| 门槛词（要进 supported_elements 的那个） | **插件自己**在注册前检查 | 内置与第三方**都查 `"sticker"`** |
| 标签名（模型写的那个） | 插件注册时命名 | 内置 = `<sticker>`；第三方 = `<sticker_plus>` |

第三方「增强表情包」的源码（`kira-ai-plugin-sticker-plus/main.py`）就是活的例子：

```python
async def inject_sticker_plus_tag(self, event, _, tag_set):
    if "sticker" not in event.message_types:     # ← 门槛词是 sticker
        return
    tag_set.register(self._build_sticker_plus_tag(event))   # ← 注册的标签叫 sticker_plus
```

⇒ 只要声明 `sticker`（本插件默认），**两家的标签都会注册**：
模型既能用 `<sticker>` 也能用 `<sticker_plus>`。用户不必再加 `sticker_plus` 这个词
（真要加也无害 —— 那只是让"声明支持"的清单更宽，不影响别的插件）。

## 本模块负责 ①

把 `"sticker"` 加进该适配器**声明的类型清单**（实例级、可还原、幂等）。

⚠ 只在**真的装了表情包**时才加 —— 否则标签说明里会是一份空清单，
反而诱导模型去发不存在的 sticker id（内置插件不检查清单是否为空）。
"""
from __future__ import annotations

from typing import Any, Optional

#: 默认关键词（可被插件配置覆盖）。内置表情包插件与第三方「增强表情包」
#: （`kira-ai-plugin-sticker-plus`）**都是看 `"sticker"` 这个词**才注册自己的标签：
#:   core/plugin/builtin_plugins/sticker/main.py : if "sticker" in supported_elements
#:   kira-ai-plugin-sticker-plus/main.py         : if "sticker" not in event.message_types: return
DEFAULT_TAG_NAME = "sticker"

#: 两种世代的落点不同：3.0 在能力对象的 `_supported_elements`，2.x 在适配器的 `message_types`
_ATTRS = ("_supported_elements", "message_types")


def supported_list(holder: Any) -> Optional[list]:
    """取这个宿主**真正在用的**那份类型清单（拿不到返回 None）。"""
    for attr in _ATTRS:
        value = getattr(holder, attr, None)
        if isinstance(value, list) and value:
            return value
    return None


def install(holder: Any, logger: Any = None, tags: Any = None) -> bool:
    """把关键词加进清单（幂等）。**没装表情包时不要调用**（见模块说明）。

    :param tags: 关键词（可配置）。默认只有 `"sticker"` —— 它同时覆盖
                 内置表情包与第三方「增强表情包」插件。
    """
    lst = supported_list(holder)
    if lst is None:
        return False
    wanted = [str(t).strip() for t in (tags or (DEFAULT_TAG_NAME,)) if str(t).strip()]
    newly = [t for t in wanted if t not in lst]
    if not newly:
        return True
    try:
        lst.extend(newly)
    except Exception:
        return False
    if logger is not None:
        try:
            logger.info(
                "[QQBOT-BRIDGE] 已让本适配器声明支持表情包标签（%s）—— 框架内置表情包插件"
                "与第三方「增强表情包」都是看这个词才注册自己的标签（否则 QQ 会话里"
                "模型根本看不到那个标签）；发送时由我们换成等价图片（file_type=1）",
                "、".join(newly),
            )
        except Exception:
            pass
    return True


def restore(holder: Any, tags: Any = None) -> bool:
    """还原（幂等）：只摘掉我们加的那些关键词，别的一律不碰。"""
    lst = supported_list(holder)
    if lst is None:
        return False
    wanted = [str(t).strip() for t in (tags or (DEFAULT_TAG_NAME,)) if str(t).strip()]
    changed = False
    for tag in wanted:
        if tag in lst:
            try:
                lst.remove(tag)
                changed = True
            except Exception:
                pass
    return changed


#: 已加载插件的 id/名字里出现关键词 ⇒ 说明确实有人在管表情包（判据用）
_ID_ATTRS = ("plugin_id", "id", "name", "display_name")


def plugin_present(plugin_mgr: Any, keywords: Any) -> bool:
    """插件注册表里有没有"名字含关键词"的插件（如 `kira-ai-plugin-sticker-plus`）。

    为什么需要：内置表情包里可能一张图都没有，但用户装的是**第三方**表情包插件
    （它有自己的图库）⇒ 只看内置管理器会漏掉这种情形，结果不声明关键词、
    第三方标签也就注册不了。

    ⚠ 全程 `getattr` 防御式读取：拿不到就当"没有"（绝不因此报错）。
    """
    if plugin_mgr is None:
        return False
    kws = [str(k).lower() for k in (keywords or ()) if str(k).strip()]
    if not kws:
        return False
    for getter in ("list_plugins", "get_registered_plugins"):
        fn = getattr(plugin_mgr, getter, None)
        if not callable(fn):
            continue
        try:
            items = fn() or []
        except Exception:
            continue
        entries = list(items.values()) if isinstance(items, dict) else list(items)
        for item in entries:
            texts = []
            if isinstance(item, str):
                texts.append(item)
            else:
                for attr in _ID_ATTRS:
                    value = getattr(item, attr, None)
                    if isinstance(value, str):
                        texts.append(value)
                if isinstance(item, dict):
                    for attr in _ID_ATTRS:
                        value = item.get(attr)
                        if isinstance(value, str):
                            texts.append(value)
            for text in texts:
                low = text.lower()
                if any(k in low for k in kws):
                    return True
    return False
