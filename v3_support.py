"""KiraAI 3.0 专属的增量增强（不重复造核心已有的事件）。

3.0 已经把"全量群消息 / 真昵称 / @ 解析 / 引用收发 / 去重 / 富内容归一化"全做完了，
所以这里**只做核心没做的两件小事**，且都在事件发布前的一瞬间完成：

1. **群名**：3.0 依旧写 `Group(group_id=target_id, group_name=target_id)`。
   在 `adapter.publish` 上包一层，命中本地缓存就把 `group_name` 换成中文名
   （`Session.session_title` 直接取它，所以 WebUI 会话列表会跟着变中文）。

2. **「引用机器人自己发的消息 = 被唤醒」的持久判据**：
   3.0 的判据是 `(is_group, target_id, quoted_id) in self._sent_message_ids` ——
   **纯内存**。实测：重启后同一条引用 → `is_mentioned=False`（不唤醒）；
   别名 LRU 淘汰后同样失效。
   bridge 补的判据读的是**原始 payload**（被引用消息的 `author.bot` + 机器人昵称），
   是平台下发的事实，**重启后依然成立**（2.x 实测 `is_mentioned=True`）。

为什么挂在 `_is_self_quote` 而不是 `publish`
----------------------------------------
`is_mentioned` 是在 `_handle_message` 里**算好写进事件**的，publish 时再去翻链已经
拿不到被引用消息的作者（3.0 的 `Reply` 元素只保留 display_id + 内容链，作者被丢了）。
而 `_is_self_quote(message, is_group, target_id)` 是能力对象上的方法，
`_handle_message` 正好会调它 —— 在这里补判据既精准又零开销。
"""

from __future__ import annotations

from typing import Any

#: 标记：确保我们只包装一次
_PUBLISH_MARK = "_kira_bridge_publish"
_QUOTE_MARK = "_kira_bridge_selfquote"


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class V3Enhancer:
    """3.0 下的两处增量补丁（幂等、可还原）。"""

    def __init__(self, plugin: Any, logger: Any):
        self.plugin = plugin
        self.logger = logger
        self._patched: dict = {}      # name -> {"adapter":…, "im":…, "publish":orig, "quote":orig}
        self._logged = {"group": False, "quote": False}

    # ------------------------------------------------------------------ #
    def install(self, adapter: Any, name: str, profile: Any) -> bool:
        im = profile.im_capability
        if im is None:
            return False
        state = self._patched.get(name)
        if state is not None and state.get("adapter") is adapter:
            return False
        if state is not None:
            self.restore(name)

        original_publish = getattr(adapter, "publish", None)
        original_quote = getattr(im, "_is_self_quote", None)
        enhancer = self

        # ---- ① publish：换群名 ----
        if callable(original_publish) and not getattr(original_publish, _PUBLISH_MARK, False):
            def publish(event, _orig=original_publish):
                try:
                    enhancer._fill_group_name(event)
                except Exception:
                    pass
                return _orig(event)

            setattr(publish, _PUBLISH_MARK, True)
            adapter.publish = publish
        else:
            publish = original_publish

        # ---- ② _is_self_quote：引用机器人 = 被唤醒（重启后仍成立）----
        if callable(original_quote) and not getattr(original_quote, _QUOTE_MARK, False):
            def is_self_quote(message, is_group, target_id, _orig=original_quote):
                try:
                    if _orig(message, is_group, target_id):
                        return True
                except Exception:
                    return False
                try:
                    result = enhancer._quoted_is_self(message, is_group, target_id)
                    if result and not enhancer._logged["quote"]:
                        enhancer._logged["quote"] = True
                        enhancer.logger.info(
                            "[QQBOT-BRIDGE] 已补上「引用机器人的消息 = 被唤醒」的持久判据"
                            "（KiraAI 3.0 原判据只认内存里发过的消息，重启后失效）"
                        )
                    return result
                except Exception:
                    return False

            setattr(is_self_quote, _QUOTE_MARK, True)
            im._is_self_quote = is_self_quote
        else:
            is_self_quote = original_quote

        self._patched[name] = {
            "adapter": adapter, "im": im,
            "publish": publish, "quote": is_self_quote,
            "has_publish": publish is not original_publish,
            "has_quote": is_self_quote is not original_quote,
        }
        return True

    def restore(self, name: str) -> int:
        state = self._patched.pop(name, None)
        if not state:
            return 0
        count = 0
        adapter = state.get("adapter")
        im = state.get("im")
        if state.get("has_publish") and adapter is not None:
            try:
                del adapter.publish
                count += 1
            except Exception:
                pass
        if state.get("has_quote") and im is not None:
            try:
                del im._is_self_quote
                count += 1
            except Exception:
                pass
        self._logged["quote"] = False
        return count

    def installed(self, name: str) -> bool:
        return name in self._patched

    # ------------------------------------------------------------------ #
    def _fill_group_name(self, event: Any) -> bool:
        """命中缓存才改，零 await / 零 I/O。"""
        msg = getattr(event, "message", None)
        group = getattr(msg, "group", None)
        if group is None:
            return False
        gid = str(getattr(group, "group_id", "") or "")
        if not gid:
            return False
        adapter_name = str(getattr(getattr(event, "adapter", None), "name", "") or "")
        name = self.plugin.group_names.lookup(adapter_name, gid)
        if not name:
            return False
        if getattr(group, "group_name", None) != name:
            group.group_name = name
        session = getattr(event, "session", None)
        if session is not None:
            try:
                session.session_title = name
            except Exception:
                pass
        if not self._logged["group"]:
            self._logged["group"] = True
            self.logger.info(
                "[QQBOT-BRIDGE] 已在 3.0 上把会话标题换成中文群名：%s → %r",
                gid[:12] + "…", name,
            )
        return True

    def _quoted_is_self(self, message: Any, is_group: bool, target_id: str) -> bool:
        """读**原始 payload** 判断"被引用的是不是机器人自己"。

        payload 结构（官方《群消息（全量模式）》）：
        `message_type=103` ⇒ `msg_elements[0].author{id, username, bot}`。
        平台直接给了作者，不需要额外请求。
        """
        try:
            mtype = int(_field(message, "message_type", 0) or 0)
        except (TypeError, ValueError):
            mtype = 0
        if mtype != 103 and not _field(message, "message_reference"):
            return False
        elements = _field(message, "msg_elements", [])
        if not isinstance(elements, list) or not elements:
            return False
        first = elements[0]
        author = _field(first, "author")
        if not isinstance(author, dict):
            return False
        if author.get("bot") is not True:
            return False

        # 机器人自己的 openid / 昵称（plugin 侧维护）
        ident = self.plugin.self_identity_for(target_id)
        oid = author.get("id") or author.get("member_openid")
        if ident.get("openid") and oid and str(oid) == str(ident["openid"]):
            return True
        robot_name = ident.get("name")
        if robot_name and str(author.get("username") or "") == str(robot_name):
            return True
        # 实在认不出：只要是 bot 且内容来自被引用消息，也认为是"引用机器人"
        # —— 群里只有机器人是 bot（官方 msg_elements 里 bot=true 的都是机器人）。
        return not robot_name and not ident.get("openid") and bool(oid)
