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
_HANDLE_MARK = "_kira_bridge_handle"
_NICK_MARK = "_kira_bridge_nickname"


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
        # ★ 通讯录的 key 必须与 **2.x 那条路径**（`str(adapter.info.name)`）完全一致，
        #   否则"群里学到的名字"和"私聊要读的名字"会落在两个 bucket 里，
        #   跨场景共享就静默失效了。
        self._adapter = adapter                        # ★ 供「输入中提前发」用
        self._adapter_name = str(
            getattr(getattr(adapter, "info", None), "name", "") or name
        )
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

        # ---- ③ _handle_message：**只旁听学昵称**（不接管、不造事件、不改行为）----
        #
        #   ★ 为什么需要（用户反馈「私聊昵称又变回 hex 了」）：
        #     QQ 官方 API 里 **C2C（私聊）的 `author.username` 恒为空**，
        #     而**群消息是带 username 的**。3.0 核心的 `nickname()` 只从
        #     `author.username` 取，取不到就**回退成 user_id**（那串 hex）
        #     ⇒ 私聊里就显示 hex。
        #
        #     我们的 `IdentityStore` 本来就是为这件事写的（跨场景共享：
        #     群里认识过的人，私聊也认得），但它的「学习入口」挂在
        #     **2.x 的事件处理**里 —— 而 3.0 上我们**不接管事件**
        #     ⇒ 学不到 ⇒ 私聊一直是 hex。
        #
        #   ★ 做法：包一层 `_handle_message`（群聊/私聊的**统一入口**），
        #     在**调用原方法之前**顺手把 `author.username` 记进 IdentityStore。
        #     不接管、不造事件、不改任何返回值 —— 纯粹"旁听"。
        original_handle = getattr(im, "_handle_message", None)
        if callable(original_handle) and not getattr(original_handle, _HANDLE_MARK, False):
            async def handle(message, is_group, force_mention, _orig=original_handle):
                try:
                    enhancer._learn_nickname(message, is_group)
                except Exception:
                    pass                       # 学不到昵称绝不影响消息处理
                result = await _orig(message, is_group, force_mention)
                # ★★ 2026-10-10（用户要求）：「输入中」提到**消息刚到时**发 ——
                #   核心有**合并缓冲**（把连发的几条合成一轮），旧挂点要等缓冲结束、
                #   模型真正开始才发 ⇒ 感觉慢半拍。这里消息一落地就踢一脚
                #   （单聊 + 已有入站 msg_id 才发；50 秒防抖 + 每 msg_id 帧数上限兜底）。
                try:
                    if not is_group:
                        enhancer._typing_early(message)
                except Exception:
                    pass                       # 绝不影响消息处理
                return result

            setattr(handle, _HANDLE_MARK, True)
            im._handle_message = handle
        else:
            handle = original_handle

        # ---- ④ parser.nickname：**私聊显示真昵称**（用户反馈「私聊还是 hex」）----
        #
        #   ★ 为什么光"学"不够（v1.6.0 的缺口，用户实测没生效）：
        #     ③ 只是把群里的 `author.username` **记进** `IdentityStore`；
        #     而 **3.0 显示昵称的唯一路径**是核心的
        #     `QQOfficialMessageParser.nickname()` —— 它只读事件自带的
        #     `author.username`，读不到就 `return user_id`（那串 hex）。
        #     ⇒ 我们写进通讯录的名字，**根本没人读** ⇒ 私聊一直是 hex。
        #
        #   ★ 做法：包一层 parser 实例的 `nickname`。规则只有一条 ——
        #     **事件没带名字时，才去通讯录里按 openid 找**（群里认识过的人）。
        #     找到就借核心自己的实现把名字写回它的缓存（`_names`），
        #     这样 `content_elements()` 里渲染 @ 时也能拿到同一个名字。
        #
        #   不接管、不造事件、不改任何其它字段 —— 纯"补一个来源"。
        parser = getattr(im, "_parser", None)
        original_nick = getattr(parser, "nickname", None) if parser is not None else None
        nickname_holder = None
        if (parser is not None and callable(original_nick)
                and not getattr(original_nick, _NICK_MARK, False)):
            def nickname(is_group, target_id, author, user_id, _orig=original_nick):
                try:
                    raw = _field(author, "username")
                    if not (isinstance(raw, str) and raw.strip()):
                        store = getattr(enhancer.plugin, "identities", None)
                        if store is not None:
                            learned = store.lookup(enhancer._adapter_name, str(user_id or ""))
                            if learned and str(learned).strip() != str(user_id):
                                # ⚠ 用**独立**的日志标记：`nick` 已被"学习"那条占用，
                                #   共用会导致这条（用户真正关心的）永远打不出来。
                                if not enhancer._logged.get("nick_display"):
                                    enhancer._logged["nick_display"] = True
                                    enhancer.logger.info(
                                        "[QQBOT-BRIDGE] 私聊昵称已恢复真名（%s）："
                                        "QQ 单聊事件的 author.username 恒为空，"
                                        "改用通讯录里同一 openid 在群里学到过的昵称",
                                        learned,
                                    )
                                # 借核心实现写回它自己的缓存（@ 渲染也走这个缓存）
                                return _orig(is_group, target_id,
                                             {"username": learned}, user_id)
                except Exception:
                    pass                          # 补昵称失败绝不影响消息处理
                return _orig(is_group, target_id, author, user_id)

            setattr(nickname, _NICK_MARK, True)
            try:
                parser.nickname = nickname
                nickname_holder = nickname
            except Exception:
                nickname_holder = None

        self._patched[name] = {
            "adapter": adapter, "im": im,
            "publish": publish, "quote": is_self_quote,
            "handle": handle, "nick": nickname_holder,
            "parser": parser,
            "has_publish": publish is not original_publish,
            "has_quote": is_self_quote is not original_quote,
            "has_handle": handle is not original_handle,
            "has_nick": nickname_holder is not None,
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
        if state.get("has_handle") and im is not None:
            try:
                del im._handle_message          # 恢复成类上的原方法
                count += 1
            except Exception:
                pass
        if state.get("has_nick") and state.get("parser") is not None:
            try:
                del state["parser"].nickname    # 恢复成类上的原方法
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

    def _typing_early(self, message: Any) -> None:
        """消息**刚落地的瞬间**踢一脚「输入中」（单聊）。

        比 `on_llm_request` 早：覆盖"核心合并缓冲 + 排队等 LLM"那段空窗，
        用户一眼就能看到「正在输入…」。门禁（配置/防抖/帧数/入站 msg_id）
        全部复用 `plugin._typing_kick` —— 这里只负责取 target。
        """
        plugin = getattr(self, "plugin", None)
        adapter = getattr(self, "_adapter", None)
        if plugin is None or adapter is None:
            return
        author = _field(message, "author")
        uid = str(_field(author, "user_openid") or _field(author, "id") or "")
        if not uid:
            return
        plugin._typing_kick(adapter, uid, source="ingest")

    def _learn_nickname(self, message: Any, is_group: bool) -> None:
        """从**原始 payload** 里学昵称，存进跨场景共享的 IdentityStore。

        ## 为什么需要（用户反馈「私聊昵称又变回 hex 了」）

        QQ 官方 API 的 **C2C（私聊）`author.username` 恒为空**，
        而**群消息是带 username 的**。核心的 `nickname()` 只从
        `author.username` 取，取不到就回退成 `user_id`（那串 hex）。

        我们的 `IdentityStore` 就是为这件事写的（群里认识过的人，私聊也认得），
        它的学习入口挂在 **2.x 的事件处理**里 ——
        3.0 上我们**不接管事件**，所以一直学不到。

        ⇒ 这里在 `_handle_message`（群/私聊的统一入口）里**旁听**一次。

        ## 三个免费来源（都是事件里本来就有的字段，零额外请求）

        1. `author.username` —— 群消息里一定有（3.0 的核心 parser 也用这个）；
        2. `mentions[]` —— @ 消息里的用户列表，每个都带 `username`
           （⚠ 文档写"不含 @ 机器人自身"，实测**会带**，要跳过 `is_you`）；
        3. `msg_elements[].author` —— `message_type=103`（引用）时是完整 User 对象。

        **不接管、不造事件、不改行为**：一个字段没有就跳过，绝不影响消息处理。
        """
        store = getattr(self.plugin, "identities", None)
        if store is None:
            return
        adapter_name = self._adapter_name
        scope = ""
        if is_group:
            scope = str(_field(message, "group_openid", "") or "")

        def _remember(uid: Any, name: Any) -> bool:
            if not uid or not isinstance(name, str) or not name.strip():
                return False
            name = name.strip()
            if name == str(uid):        # 占位（名字恰好等于 openid）不算学到
                return False
            return bool(store.remember(adapter_name, scope, str(uid), name))

        learned = 0
        try:
            author = _field(message, "author")
            if author is not None:
                uid = (_field(author, "member_openid") or _field(author, "user_openid")
                       or _field(author, "id"))
                learned += 1 if _remember(uid, _field(author, "username")) else 0

            # ② mentions（@ 列表）—— 群里最常见的动作，通讯录主要的增量来源
            mentions = _field(message, "mentions", [])
            if isinstance(mentions, list):
                for item in mentions:
                    if _field(item, "is_you") is True:
                        continue
                    uid = (_field(item, "member_openid") or _field(item, "user_openid")
                           or _field(item, "id"))
                    learned += 1 if _remember(uid, _field(item, "username")) else 0

            # ③ 引用消息里的作者（message_type=103）
            elements = _field(message, "msg_elements", [])
            if isinstance(elements, list):
                for elem in elements:
                    quoted = _field(elem, "author")
                    if quoted is None:
                        continue
                    uid = (_field(quoted, "member_openid")
                           or _field(quoted, "user_openid") or _field(quoted, "id"))
                    learned += 1 if _remember(uid, _field(quoted, "username")) else 0
        except Exception:
            pass                             # 学昵称失败绝不影响消息处理

        if learned and not self._logged.get("nick"):
            self._logged["nick"] = True
            self.logger.info(
                "[QQBOT-BRIDGE] 已补上跨场景昵称共享：群里的真昵称会带给私聊"
                "（KiraAI 3.0 原实现只认 author.username，而私聊该字段恒为空 ⇒ 显示 hex）"
            )

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
