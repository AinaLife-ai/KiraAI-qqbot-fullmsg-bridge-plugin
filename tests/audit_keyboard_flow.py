"""★ 内联键盘端到端（2026-10-10：QQ 里只看到文字 + [Unsupported message element]、按钮不出现）。

## 两个独立的真问题（用户截图 + 官方文档定位）

1. **脏文本**：核心 `_text_content()` 只认 `Text`，我们的 `MarkdownText` / `KeyboardMarker`
   落进 `else` 分支 ⇒ 拼出字面量 `[Unsupported message element]` 发到 QQ
   （截图里那句「想让香香干嘛……[Unsupported message element]」就是它）。
   ⇒ 修：两个元素**继承核心 `Text`**（键盘渲染成**零宽空格**：不可见，
   又能让带键盘的消息通过核心的「不能发空消息」检查）。

2. **按钮不渲染**：官方 Node/Python SDK「发送带有按钮的消息」原话 ——
   **仅 markdown 消息支持消息按钮**。纯文本（msg_type=0）+ keyboard
   ⇒ 平台不渲染按钮，用户只看到文字。
   ⇒ 修：发送侧发现"有键盘但不是 markdown 消息"时**自动升格成 markdown**。

## 本测试断言

A. 真实核心（2.x / 3.0 各自）：
   * 两个元素都是核心 `Text` 的子类；
   * 真实 `_text_content(chain)` 输出**不含** `[Unsupported message element]`，
     且 markdown 正文原样保留；
   * 带键盘的纯文本消息 `content` 非空（能过核心空检查）。

B. 发送层（api_send，真实补丁）：
   * 纯文本 + 键盘 ⇒ `msg_type=2`、`markdown.content`=正文、`content=None`、键盘挂上；
   * 已经是 markdown + 键盘 ⇒ 原样（不覆盖 markdown）；
   * 富媒体 + 键盘 ⇒ 保持富媒体（不硬塞 markdown）且有一条 WARNING；
   * 群聊路径同样升格；
   * 零宽占位**不会**漏进 markdown 正文。
"""
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, botpy_parent as _BOTPY_DIR, core_root as _CORE_ROOT

import asyncio
import logging
import os
import sys
from types import SimpleNamespace

BR = str(_BR())
sys.path.insert(0, BR)
sys.path.insert(0, str(_BOTPY_DIR()))

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class L:
    def __init__(self):
        self.lines = []

    def _p(self, lv, a):
        self.lines.append((lv, (a[0] % tuple(a[1:])) if len(a[1:]) else str(a[0])))

    def info(self, *a):
        self._p("info", a)

    def warning(self, *a):
        self._p("warning", a)

    def debug(self, *a):
        pass


for GEN in ("3", "2"):
    CORE = str(_CORE_ROOT(GEN))
    if not os.path.isdir(CORE):
        print(f"\n═══ A{GEN}. 世代 {GEN}：核心不可用 ⇒ 跳过本代 ═══")
        continue
    print(f"\n═══ A{GEN}. 真实核心 {GEN}（{CORE}）═══")
    # 清理可能存在的旧核心模块（两代同名模块不能混）
    for mod in [m for m in list(sys.modules) if m.split(".")[0] == "core"]:
        sys.modules.pop(mod, None)
    sys.path.insert(0, CORE)
    for mod in ("main", "rich_content", "api_send"):
        sys.modules.pop(mod, None)
    try:
        import rich_content as RC
        from core.chat.message_elements import Text
        from core.chat.message_utils import MessageChain
    except Exception as exc:
        print(f"  skip  导入失败（{type(exc).__name__}: {exc}）")
        sys.path.remove(CORE)
        continue

    check("★ MarkdownText 继承核心 Text（核心才会当文本渲染）",
          issubclass(RC.MarkdownText, Text), str(RC.MarkdownText.__mro__[:3]))
    check("★ KeyboardMarker 继承核心 Text", issubclass(RC.KeyboardMarker, Text),
          str(RC.KeyboardMarker.__mro__[:3]))

    kb = {"content": {"rows": [{"buttons": [{"id": "b1",
                                             "render_data": {"label": "点我", "style": 1},
                                             "action": {"type": 2, "data": "/签到",
                                                        "permission": {"type": 2}}}]}]}}
    km = RC.KeyboardMarker(kb)
    check("★ 键盘元素的文本是零宽空格（不可见但非空）",
          km.text == RC.KB_TEXT_PLACEHOLDER and km.text.strip() != "", repr(km.text))
    check("★ 零宽空格能被 strip_kb_placeholder 去掉",
          RC.strip_kb_placeholder("正文" + RC.KB_TEXT_PLACEHOLDER) == "正文")

    chain = MessageChain([Text("想让香香干嘛就点下面的按钮"),
                          RC.MarkdownText("**加粗** 正文"),
                          RC.KeyboardMarker(kb)])

    # 真实能力的 _text_content（3.0 在能力对象上、2.x 在适配器实例上）
    text_content = None
    try:
        from core.adapter.adapter_info import AdapterInfo
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter

        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo", platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        if GEN == "3":
            from core.adapter.context import AdapterContext

            ad = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
            from core.adapter.capabilities import IMCapability

            cap = ad.get_capability(IMCapability)
            text_content = cap._text_content
        else:
            ad = QQOfficialAdapter(info, asyncio.Queue())
            text_content = ad._text_content
    except Exception as exc:
        print(f"  skip  适配器构造失败（{type(exc).__name__}: {exc}）")

    if text_content is not None:
        rendered = text_content(chain)
        check("★★★ 真实 _text_content 不再吐 [Unsupported message element]",
              "[Unsupported message element]" not in rendered, repr(rendered))
        check("★★ 正文与 markdown 内容都在（没被吞）",
              "想让香香干嘛就点下面的按钮" in rendered and "**加粗** 正文" in rendered,
              repr(rendered))
        check("★ 键盘只贡献一个零宽占位（用户看不到字符）",
              rendered.endswith(RC.KB_TEXT_PLACEHOLDER), repr(rendered[-6:]))
        check("★ 纯键盘消息的 content 非空（能过核心的「不能发空消息」检查）",
              bool(text_content(MessageChain([RC.KeyboardMarker(kb)])).strip()),
              repr(text_content(MessageChain([RC.KeyboardMarker(kb)]))))
    sys.path.remove(CORE)

print("\n═══ B. 发送层：键盘必须挂 markdown 消息（官方：仅 markdown 支持按钮）═══")
import api_send as A  # noqa: E402


class FakeAPI:
    def __init__(self, reject_md=False):
        self.calls = []
        self.reject_md = reject_md

    async def post_c2c_message(self, **kw):
        self.calls.append(("c2c", dict(kw)))
        if self.reject_md and kw.get("markdown"):
            raise RuntimeError("304036 no markdown permission")
        return {"id": "M1"}

    async def post_group_message(self, **kw):
        self.calls.append(("group", dict(kw)))
        return {"id": "M2"}


class FakeClient:
    def __init__(self, api):
        self.api = api


PLUGIN = SimpleNamespace(md_drop_reference=True, markdown_enabled=True,
                         keyboard_enabled=True, c2c_stream=None, typing_enabled=False)

KB = {"content": {"rows": [{"buttons": [{"id": "b1",
                                        "render_data": {"label": "点我", "style": 1},
                                        "action": {"type": 2, "data": "/签到",
                                                   "permission": {"type": 2}}}]}]}}


async def send(api, *, kb=None, md=None, content=None, is_group=False, msg_type=0, media=None,
               reference=None):
    log = L()
    patcher = A.ApiSendPatcher(PLUGIN, log)
    client = FakeClient(api)
    patcher.install(SimpleNamespace(), "qqo", client)
    tok_md = A.PENDING_MD.set(md)
    tok_kb = A.PENDING_KB.set(kb)
    try:
        kwargs = {"msg_type": msg_type, "content": content}
        if media:
            kwargs["media"] = media
        if reference:
            kwargs["message_reference"] = {"message_id": reference}
        if is_group:
            await api.post_group_message(group_openid="G1", **kwargs)
        else:
            await api.post_c2c_message(openid="U1", **kwargs)
    finally:
        A.PENDING_KB.reset(tok_kb)
        A.PENDING_MD.reset(tok_md)
        try:
            patcher.restore("qqo")          # 每个用例用独立的 api，还原避免串味
        except Exception:
            pass
    return api.calls[-1][1], log


print("\n[B1] 纯文本 + 键盘 ⇒ 自动升格成 markdown（按钮才渲染得出来）")
api = FakeAPI()
sent, log = asyncio.run(send(api, kb=KB, content="想让香香干嘛就点下面的按钮"))
check("★★ msg_type 升格为 2", sent.get("msg_type") == 2, str(sent.get("msg_type")))
check("★★ markdown.content = 原正文", sent.get("markdown", {}).get("content")
      == "想让香香干嘛就点下面的按钮", str(sent.get("markdown")))
check("★ content 必须为空（官方：传了 markdown 后 content 必须为空）",
      not sent.get("content"), repr(sent.get("content")))
check("★ keyboard 挂在同一条上", bool(sent.get("keyboard")), str(sent.get("keyboard")))
check("★ 有可见日志说明升格原因（含官方依据）",
      any("升格" in m and "markdown" in m for _lv, m in log.lines), str(log.lines[-1:]))

print("\n[B2] 已经是 markdown + 键盘 ⇒ 不覆盖原 markdown")
api = FakeAPI()
sent, _ = asyncio.run(send(api, kb=KB, md="# 标题\n正文", content=None))
check("★ msg_type=2 且 markdown 原文一字不改",
      sent.get("msg_type") == 2 and sent["markdown"]["content"] == "# 标题\n正文",
      str(sent.get("markdown")))
check("★ keyboard 挂上", bool(sent.get("keyboard")))

print("\n[B3] 富媒体(msg_type=7) + 键盘 ⇒ 不硬塞 markdown，但会提示限制")
api = FakeAPI()
sent, log = asyncio.run(send(api, kb=KB, content=None, msg_type=7,
                             media={"file_info": "FI"}))
check("★ 仍是富媒体消息（不破坏发图/发语音）",
      sent.get("msg_type") == 7 and sent.get("media") == {"file_info": "FI"},
      str(sent))
check("★ 有一条 WARNING 说明「按钮只支持 markdown 消息」",
      any("只支持 markdown" in m and lv == "warning" for lv, m in log.lines),
      str(log.lines[-2:]))

print("\n[B4] 群聊路径同样升格（官方键盘群聊也支持）")
api = FakeAPI()
sent, _ = asyncio.run(send(api, kb=KB, content="群里的按钮", is_group=True))
check("★ 群聊 msg_type=2 + markdown + keyboard",
      sent.get("msg_type") == 2 and sent.get("markdown", {}).get("content") == "群里的按钮"
      and bool(sent.get("keyboard")), str(sent))

print("\n[B5] 零宽占位不会漏进正文；只有键盘时用不可见占位")
api = FakeAPI()
sent, _ = asyncio.run(send(api, kb=KB, content="正文\u200b"))
check("★ 正文里的零宽占位被清掉", sent["markdown"]["content"] == "正文",
      repr(sent["markdown"]["content"]))
api = FakeAPI()
sent, _ = asyncio.run(send(api, kb=KB, content=""))
check("★ 只有键盘：markdown 用不可见占位（不是空串）",
      sent["markdown"]["content"] == "\u200b", repr(sent["markdown"]["content"]))

print("\n[B6] markdown 被平台拒 ⇒ 退回纯文本（键盘一并放弃，不重复报错）")
api = FakeAPI(reject_md=True)
sent, log = asyncio.run(send(api, kb=KB, content="正文"))
check("★ 最终发出的是纯文本（msg_type=0）",
      sent.get("msg_type") == 0 and sent.get("content") == "正文", str(sent))
check("★ 回退时不再带 keyboard（键盘依赖 markdown 消息）",
      not sent.get("keyboard"), str(sent.get("keyboard")))

print(f"\n结果：{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
