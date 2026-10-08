"""发送层解耦（api 层补丁）—— markdown / keyboard / 引用 / @ 的唯一注入点。

为什么必须从 `adapter._send_message` 挪到这里
-------------------------------------------
* **2.x**：框架调 `adapter.send_group_message()` → `adapter._send_message()` → `client.api.post_group_message()`。
* **3.0**：框架调 `adapter.send_group_message()` → `capability.send_group_message()` →
  `capability._send_message()` → `client.api.post_group_message()`。

`adapter._send_message` 在 3.0 上**根本不存在**（实测：`callable=True` 在 2.x、`False` 在 3.0），
所以挂在那一层的补丁在 3.0 上会**整条提前 return**，主动兜底 / 引用注入 / @markdown 全部失效。

而 `client.api.post_group_message` **两版结构完全一致**，且 botpy 用
``payload = locals()`` 组装请求体 —— 多传一个 kwarg 就会进 JSON（已实测）。
⇒ 把补丁挂在这里，代码只写一份，两边都生效。

补丁职责（按优先级）
------------------
1. **markdown**：正文里出现 `<markdown>`（由 rich_content 提取）→ `msg_type=2` + `markdown.content`；
   失败按错误码白名单退回纯文本（剥掉标记）。
2. **keyboard**：把 `keyboard` kwarg 塞进去（官方 `keyboard` 字段）。
3. **引用**：`message_reference`（contextvar 传入的 REFIDX）。
4. **@ 自动转 md**：正文含平台 @ 标记时按 markdown 发（2.x 原有行为，保留）。
5. **记录自己发的消息的 ref_idx**：以后才能引用自己。

作用域保护
--------
`BotAPI` 上没有反指 client 的引用，所以用 `id(api)` 建白名单，
只对**我们登记过的** QQ 官方 client 的 api 生效，避免误伤同进程里的其它 botpy 客户端。
"""

from __future__ import annotations

import contextvars
from typing import Any, Optional

#: 这一次发送要引用哪条消息（REFIDX）。用 contextvar 传给 api 层包装，
#: 避免为了注入 message_reference 去复制一遍适配器的发送逻辑。
QUOTE_REF: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "qqbot_bridge_quote_ref", default=None)

#: 这一次发送的 markdown / keyboard（同样用 contextvar，逐条传递，互不串味）
PENDING_MD: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "qqbot_bridge_pending_md", default=None)
PENDING_KB: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar(
    "qqbot_bridge_pending_kb", default=None)

#: markdown 被拒时的错误码（= 可以安全退回纯文本的信号）。
#: 只有这些码才回退 —— 其它异常必须原样抛出，否则会吞掉真问题。
MD_REJECT_CODES = ("304036", "40034127", "40034011", "40034008", "40034009",
                   "40034124", "40034010", "22006", "340069")


def looks_like_md_reject(exc: Any) -> bool:
    text = str(exc)
    return any(code in text for code in MD_REJECT_CODES)


class ApiSendPatcher:
    """把发送增强挂到 `client.api.post_group_message` / `post_c2c_message` 上。"""

    #: 只对我们登记过的 api 生效（id 白名单）
    _OWNED: set = set()

    def __init__(self, plugin: Any, logger: Any):
        self.plugin = plugin
        self.logger = logger
        self._originals: dict = {}      # name -> [(api, method_name, orig)]
        self._flags: dict = {}          # name -> dict（一次性日志标记）

    # ------------------------------------------------------------------ #
    def install(self, adapter: Any, name: str, client: Any) -> bool:
        """安装补丁（幂等）。返回是否新装上了。

        行为开关（markdown / 键盘 / 引用 / @自动转md）统一由插件实例上的
        配置决定 —— 见 `_send`，**不接受 per-install 参数**，避免"装了一套行为、
        跑的时候又按另一套行为"的双份配置漂移。
        """
        api = getattr(client, "api", None)
        if api is None:
            return False
        self._OWNED.add(id(api))

        patched = self._originals.setdefault(name, [])
        flags = self._flags.setdefault(name, {})
        installed = False

        originals = self._originals_of(api)
        for method_name, is_group in (("post_group_message", True),
                                      ("post_c2c_message", False)):
            current = getattr(api, method_name, None)
            if not callable(current):
                continue
            if getattr(current, "_kira_bridge_send", False):
                # 已有我们（或旧实例）的补丁 —— 取回真正的原始实现
                orig = originals.get(method_name) or getattr(current, "_kira_bridge_orig", None)
                if not callable(orig):
                    continue
            else:
                orig = current
            originals[method_name] = orig

            bridge = self

            async def _patched(*args, _orig=orig, _is_group=is_group, _flags=flags,
                               _api=api, _cli=client, **kwargs):
                # 作用域保护：只对我们**当前登记**的 api 生效。
                # 还原（restore）会把 id 从白名单摘掉 —— 之后即使函数引用还残留在
                # 某个对象上（热重载/多实例），也只会原样透传，绝不误伤别的 botpy 客户端。
                if not bridge.owns(_api):
                    return await _orig(*args, **kwargs)
                return await bridge._send(adapter, _orig, _is_group, _flags,
                                          *args, _client=_cli, **kwargs)

            setattr(_patched, "_kira_bridge_send", True)
            setattr(_patched, "_kira_bridge_orig", orig)
            setattr(api, method_name, _patched)
            patched.append((api, method_name, orig))
            installed = True
        return installed

    @staticmethod
    def _originals_of(api: Any) -> dict:
        originals = getattr(api, "_qqbot_bridge_api_orig", None)
        if not isinstance(originals, dict):
            originals = {}
            try:
                api._qqbot_bridge_api_orig = originals
            except Exception:
                pass
        return originals

    def restore(self, name: str) -> int:
        """还原某适配器的全部 api 补丁。"""
        count = 0
        for api, method_name, orig in self._originals.pop(name, []):
            try:
                setattr(api, method_name, orig)
                self._OWNED.discard(id(api))
                count += 1
            except Exception:
                pass
        self._flags.pop(name, None)
        return count

    def owns(self, api: Any) -> bool:
        return id(api) in self._OWNED

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    async def _fix_md_images(self, md_text: str, flags: dict, client: Any,
                             target_id: str, is_group: bool) -> str:
        """把 md 里的图片地址换成 QQ 能真正下载到的公网地址（**不改 md 结构**）。

        * 本地路径 → 走官方「分片上传」拿 `raw_url`（COS 预签名 GET URL）；
        * 公网 URL → 验真（跟随跳转 / 查 Content-Type），
          不是可下载的图片就**原样保留**并告警（不静默改动用户内容）。

        任何失败都**原样返回**，绝不因为图片转存失败而丢掉整条消息。
        """
        if not md_text or "![" not in md_text:
            return md_text
        try:
            from md_media import fix_markdown_images
            out = await fix_markdown_images(
                md_text,
                client=client,
                target_id=str(target_id),
                is_group=is_group,
                logger=self.logger,
            )
            if out != md_text and not flags.get("md_img_logged"):
                flags["md_img_logged"] = True
                self.logger.info(
                    "[QQBOT-BRIDGE] markdown 内图片已换成公网可访问地址"
                    "（QQ 只认公网 URL，本地路径会退化成 alt 文字）"
                )
            # ★ 诊断：把**最终要发出去的 markdown** 原样记一条（图片相关时才记）。
            #   线上排查图片问题时，"我们到底发了什么"是最关键的信息，
            #   而之前日志里完全没有 —— 只能靠猜（2026-10-08 教训）。
            if "![" in out and not flags.get("md_final_logged"):
                flags["md_final_logged"] = True
                self.logger.info(
                    "[QQBOT-BRIDGE] 本条 markdown 实际内容（含图片地址）：\n%s", out)
            return out
        except Exception as exc:
            self.logger.debug("[QQBOT-BRIDGE] markdown 图片处理失败（原样发送）: %s", exc)
            return md_text

    @staticmethod
    def _target_of(args: tuple, kwargs: dict, is_group: bool) -> str:
        """从 botpy 的调用参数里取出目标 id。

        `post_group_message(group_openid=..., ...)` / `post_c2c_message(openid=..., ...)`，
        也可能按位置传。取不到就返回空串（图片转存会跳过，不影响发送）。
        """
        key = "group_openid" if is_group else "openid"
        v = kwargs.get(key)
        if not v and args:
            v = args[0]
        return str(v or "")

    async def _send(self, adapter: Any, orig: Any, is_group: bool, flags: dict,
                    *args, _client: Any = None, **kwargs):
        # 作用域由 install 时的白名单（id(api)）保证 —— 补丁只挂在登记过的 api 上。
        # ① 引用（只在有明确引用意图时注入）
        ref = QUOTE_REF.get()
        if ref and not kwargs.get("message_reference"):
            kwargs = dict(kwargs)
            kwargs["message_reference"] = {"message_id": ref}

        # ② 显式 markdown / keyboard（由 rich_content 从消息链提取后放进 contextvar）
        md_text = PENDING_MD.get()
        keyboard = PENDING_KB.get()

        # ③ 兼容原有行为：正文含 @ 标记 → 自动走 markdown
        auto_md = None
        content = kwargs.get("content")
        if (md_text is None and isinstance(content, str)
                and ("<@" in content or "qqbot-at-user" in content)):
            auto_md = content

        target_md = md_text or auto_md
        if target_md:
            # ★★★ 图片修复：markdown 里的图片必须是**公网可访问的地址**，
            #   本地路径（data/temp/x.jpg）QQ 根本下不到 ⇒ 会渲染成 alt 文字
            #   （用户实测：`![香香](data/temp/...)` 显示成「[香香]」）。
            #   这里只替换 `(...)` 里的 URL，**md 结构一字不动**
            #   （标题/列表/引用/链接/代码块全部保留，行数也不变）。
            target_md = await self._fix_md_images(
                target_md, flags, _client, self._target_of(args, kwargs, is_group),
                is_group)

            kwargs = dict(kwargs)
            kwargs["msg_type"] = 2
            kwargs["markdown"] = {"content": target_md}
            kwargs["content"] = None
            if auto_md and not flags.get("at_md_logged"):
                flags["at_md_logged"] = True
                self.logger.info(
                    "[QQBOT-BRIDGE] 正文含 @ 标记 → 本条改按 markdown 发送"
                    "（纯文本消息没有 @ 能力，会被显示成文本）"
                )
            elif md_text and not flags.get("md_logged"):
                flags["md_logged"] = True
                self.logger.info("[QQBOT-BRIDGE] 首次按 markdown 发送（<markdown> 标签生效）")

        if keyboard:
            kwargs = dict(kwargs)
            kwargs["keyboard"] = keyboard
            if not flags.get("kb_logged"):
                flags["kb_logged"] = True
                self.logger.info("[QQBOT-BRIDGE] 首次发送内联键盘（<keyboard> 标签生效）")

        # ④ 发送（markdown 失败 → 退纯文本）
        if not target_md:
            result = await orig(*args, **kwargs)
        else:
            try:
                result = await orig(*args, **kwargs)
            except Exception as exc:
                if not looks_like_md_reject(exc):
                    raise
                fallback = dict(kwargs)
                fallback["msg_type"] = 0
                fallback["markdown"] = None
                fallback["content"] = strip_at_markup(target_md)
                fallback.pop("keyboard", None)   # 键盘依赖 markdown 的消息类型，一并放弃
                if not flags.get("md_fallback_logged"):
                    flags["md_fallback_logged"] = True
                    self.logger.warning(
                        "[QQBOT-BRIDGE] markdown 发送失败（%s），已退回纯文本%s —— "
                        "若群里看到的就是这种情况，说明该机器人没有 markdown 消息权限",
                        str(exc)[:120],
                        "并剥掉 @ 标记" if auto_md else "",
                    )
                result = await orig(*args, **fallback)

        # ⑤ 记住"机器人自己发的这条"的 ref_idx（以后才能引用它）
        try:
            sent_ref = _extract_ref_idx(result)
            if sent_ref:
                target = kwargs.get("group_openid") if is_group else kwargs.get("openid")
                sent_id = result.get("id") if isinstance(result, dict) else None
                if target and sent_id:
                    self.plugin.remember_sent_ref(adapter, str(target), str(sent_id),
                                                  sent_ref, is_group)
        except Exception as exc:
            self.logger.debug("[QQBOT-BRIDGE] 记录已发送消息的 ref_idx 失败: %s", exc)
        return result


def _extract_ref_idx(result: Any) -> Optional[str]:
    from qqbot_bridge import extract_sent_ref_idx

    return extract_sent_ref_idx(result)


def strip_at_markup(text: str) -> str:
    """把正文里的平台 @ 标记整个去掉（markdown 发不出去时的兜底）。"""
    if not text or ("<@" not in text and "qqbot-at-user" not in text):
        return text
    from qqbot_bridge import strip_at_markup as _strip

    return _strip(text)
