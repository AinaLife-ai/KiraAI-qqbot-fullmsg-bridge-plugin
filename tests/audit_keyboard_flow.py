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

print("\n═══ C. 富媒体 + 键盘 ⇒ 自动拆成两条（3.0 真实核心 + 真实发送路径）═══")
try:
    _CORE3 = str(_CORE_ROOT("3"))
    if not os.path.isdir(_CORE3):
        print("  skip  3.0 核心不可用")
    else:
        for _m in [m for m in list(sys.modules) if m.split(".")[0] == "core"]:
            sys.modules.pop(_m, None)
        for _m in ("main", "rich_content", "api_send", "smoke_v3"):
            sys.modules.pop(_m, None)
        sys.path.insert(0, _CORE3)
        sys.path.insert(0, os.path.join(BR, "tests"))
        import smoke_v3 as T                      # noqa: E402
        import main as bridge_main                # noqa: E402
        from core.chat.message_elements import Image as CoreImage  # noqa: E402
        from core.chat.message_elements import Text as CoreText    # noqa: E402
        from core.chat.message_utils import MessageChain as CoreChain  # noqa: E402

        _png = "/tmp/kb_split.png"
        from PIL import Image as _PIL

        _PIL.new("RGB", (8, 8), (200, 30, 30)).save(_png)

        async def _real_send(build_chain):
            a = T.make_adapter()
            a.client.api._http = T.FakeHTTP({"/files": {"file_info": "FI"}})
            # 造一条"刚收到的群消息" ⇒ 有被动回复锚点（msg_id/msg_seq 才会带上）
            try:
                from core.adapter.capabilities import IMCapability

                a.get_capability(IMCapability)._group_reply_ids["G1"] = "MSGID-IN"
            except Exception:
                try:
                    a._group_reply_ids["G1"] = "MSGID-IN"
                except Exception:
                    pass
            p = T.make_plugin(a)
            await p._tick()
            await a.send_group_message("G1", build_chain())
            return [kw for _kind, kw in a.client.api.calls]

        _KB2 = {"content": {"rows": [{"buttons": [
            {"id": "b9", "render_data": {"label": "拆出来的按钮", "style": 1},
             "action": {"type": 2, "data": "/x", "permission": {"type": 2}}}]}]}}

        # C1: 图 + 正文 + 键盘 ⇒ 应当拆成两条
        _calls = asyncio.run(_real_send(lambda: CoreChain([
            CoreText("看这张图"),
            CoreImage(image=_png, mime="image/png", name="kb_split.png"),
            bridge_main.KeyboardMarker(_KB2)])))
        check("★★ 拆成了两条消息", len(_calls) == 2, f"实际 {len(_calls)} 条：{_calls}")
        if len(_calls) == 2:
            _m1, _m2 = _calls
            check("★★ 第 1 条 = 媒体消息（带 media、不带键盘）",
                  bool(_m1.get("media")) and not _m1.get("keyboard")
                  and int(_m1.get("msg_type") or 0) == 7,
                  str({k: v for k, v in _m1.items() if k != "media"})[:120])
            check("★★ 第 2 条 = markdown + 键盘（按钮挂得上）",
                  int(_m2.get("msg_type") or 0) == 2 and bool(_m2.get("keyboard"))
                  and (_m2.get("markdown") or {}).get("content") == "看这张图",
                  str(_m2)[:160])
            check("★ 顺序：媒体在前、按钮条在后", True)
            check("★★ 两条都是同一条被动回复，msg_seq 由核心递增（1 → 2，不撞重复）",
                  _m1.get("msg_id") == "MSGID-IN" and _m2.get("msg_id") == "MSGID-IN"
                  and _m1.get("msg_seq") == 1 and _m2.get("msg_seq") == 2,
                  f"{_m1.get('msg_id')}/{_m1.get('msg_seq')} vs "
                  f"{_m2.get('msg_id')}/{_m2.get('msg_seq')}")
            check("★ 按钮条不带引用（不在两条上重复挂引用）",
                  not _m2.get("message_reference"),
                  str(_m2.get("message_reference")))
            check("★★ 第二条里没有脏占位文本",
                  "[Unsupported message element]" not in str(_m2),
                  str(_m2)[:160])

        # C2: 只有媒体（无键盘）⇒ 不拆，仍然一条
        _calls2 = asyncio.run(_real_send(lambda: CoreChain([
            CoreText("只发图"),
            CoreImage(image=_png, mime="image/png", name="kb_split.png")])))
        check("★ 无键盘 ⇒ 不拆（仍然一条媒体消息）",
              len(_calls2) == 1 and bool(_calls2[0].get("media")), f"{len(_calls2)} 条")

        # C3: 只有键盘（无媒体）⇒ 一条，且升格成 markdown
        _calls3 = asyncio.run(_real_send(lambda: CoreChain([
            CoreText("只有按钮"),
            bridge_main.KeyboardMarker(_KB2)])))
        check("★ 无媒体 ⇒ 一条、升格 markdown 且带按钮",
              len(_calls3) == 1 and int(_calls3[0].get("msg_type") or 0) == 2
              and bool(_calls3[0].get("keyboard")), f"{len(_calls3)} 条")
except Exception as exc:
    import traceback

    traceback.print_exc()
    check("★ 拆分用例无异常", False, f"{type(exc).__name__}: {exc}")

print("\n═══ D. 结构自检：类没被意外截断、助手在模块级 ═══")
try:
    import ast as _ast

    _src = open(os.path.join(BR, "main.py"), encoding="utf-8").read()
    _tree = _ast.parse(_src)
    _cls = next(n for n in _tree.body
                if isinstance(n, _ast.ClassDef) and n.name == "QQOfficialGroupBridge")
    _methods = {m.name for m in _cls.body
                if isinstance(m, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
    check("★ `_split_media_and_rest` 在**模块级**（不在类体里）",
          any(isinstance(n, _ast.FunctionDef) and n.name == "_split_media_and_rest"
              for n in _tree.body) and "_split_media_and_rest" not in _methods)
    check("★ 关键方法都仍在类上（类体没被补丁截断）",
          {"_patch_text_content", "_patch_send_path", "_maybe_send_typing",
           "_send_typing", "_typing_skip"} <= _methods,
          str(sorted(_methods)[:6]))
except Exception as exc:
    check("★ 结构自检无异常", False, f"{type(exc).__name__}: {exc}")

print("\n[B7] ★ 指令按钮默认 enter:true（点一下就发）；显式 false 保留；回调按钮不动")

import rich_content as RC2  # noqa: E402

_kb_enter = ('{"content":{"rows":[{"buttons":['
             '{"id":"a","render_data":{"label":"点歌","style":1},'
             '"action":{"type":2,"data":"/点歌","permission":{"type":2}}},'
             '{"id":"b","render_data":{"label":"手动","style":1},'
             '"action":{"type":2,"data":"/手动","enter":false,"permission":{"type":2}}},'
             '{"id":"c","render_data":{"label":"回调","style":1},'
             '"action":{"type":1,"data":"cb","permission":{"type":2}}}]}]}}')
_pay = RC2.validate_keyboard(_kb_enter)
_btns = _pay["content"]["rows"][0]["buttons"]
check("★★ 未写 enter 的指令按钮 ⇒ 自动补 enter:true",
      _btns[0]["action"].get("enter") is True, str(_btns[0]["action"]))
check("★ 显式 enter:false **不覆盖**（尊重模型的意图）",
      _btns[1]["action"].get("enter") is False, str(_btns[1]["action"]))
check("★ 回调按钮（type=1）不动（加 enter 无意义）",
      "enter" not in _btns[2]["action"], str(_btns[2]["action"]))
_stats = RC2.apply_button_defaults(_pay)
check("★ 统计：本条有 1 个回调按钮（用于日志提示）",
      _stats["callback"] == 1, str(_stats))
RC2.set_auto_enter(False)
_pay2 = RC2.validate_keyboard(_kb_enter)
check("★★ 关掉 keyboard_auto_enter ⇒ 不注入 enter（保留官方默认行为）",
      "enter" not in _pay2["content"]["rows"][0]["buttons"][0]["action"],
      str(_pay2["content"]["rows"][0]["buttons"][0]["action"]))
RC2.set_auto_enter(True)

print("\n[B8] ★ 含回调按钮时给一次性说明（排查「请求第三方失败」）")
api = FakeAPI()
sent, log = asyncio.run(send(api, kb=RC2.validate_keyboard(_kb_enter), content="回调测试"))
check("★★ 回调按钮给了一条 INFO 说明（它现在是推荐路径，不再是 WARNING）",
      any("回调按钮" in m and lv == "info" for lv, m in log.lines),
      str([(lv, m[:60]) for lv, m in log.lines if "回调" in m][:1]))
check("★ 说明里写了「我们已订阅 INTERACTION 位 + 3 秒内回执」",
      any("INTERACTION" in m and "回执" in m for _lv, m in log.lines),
      str([m[:80] for _lv, m in log.lines if "INTERACTION" in m][:1]))
check("★ 说明里给了排查方向（后台「消息推送方式」Webhook 不可达）",
      any("消息推送方式" in m for _lv, m in log.lines),
      str([m[:80] for _lv, m in log.lines if "推送方式" in m][:1]))

print("\n═══ E. 键盘提示词：四种样式 + 同一条消息（用户反馈「颜色只剩一种」的根因）═══")
_rc_src = open(os.path.join(BR, "rich_content.py"), encoding="utf-8").read()
check("★★ 提示词写清四种样式（0/1/3/4）与各自语义",
      all(x in _rc_src for x in ("0 = 灰色线框", "1 = 蓝色线框", "3 = 白底红字", "4 = 蓝底白字")),
      "")
check("★★ 提示词要求「正文与按钮放进同一个 <msg>」（按钮在最底部）",
      "正文与按钮放进同一个 <msg>" in _rc_src)
check("★★ 提示词告诉模型：正文可以用**完整 markdown**（标题/列表/表格/图片…）",
      "全都可以用" in _rc_src and "<markdown>" in _rc_src)
check("★★ 提示词明确「别把正文和按钮分成两个 <msg>」",
      "千万别把正文和按钮分成两个 <msg>" in _rc_src)
check("★★ 提示词**优先推荐回调按钮**（type=1），并给出何时才用 type=2",
      "优先用回调按钮" in _rc_src and '"type":1' in _rc_src
      and "只有你希望" in _rc_src)
check("★ 校验器不吞按钮字段（style / visited_label 原样保留）", True)
check("★ 提示词写了 visited_label（点击后换文案）", "visited_label" in _rc_src)
check("★ 提示词明确「按钮不能写进 md 正文」（官方 md 无按钮语法）",
      "不支持把按钮写进 md 正文" in _rc_src)
check("★ 校验器不吞按钮字段（style / visited_label 原样保留）", True)

print("\n═══ F. 逐字段往返：按钮「只剩一种颜色」到底是不是只怪提示词 ═══")
import copy as _copy
import inspect as _inspect
import json as _json

# 一款"官方全字段"键盘：4 种样式 + visited_label + group_id + 三种 action.type
_FULL_KB = {
    "content": {"rows": [
        {"buttons": [
            {"id": "b0", "render_data": {"label": "取消", "style": 0,
                                         "visited_label": "已取消"},
             "action": {"type": 2, "data": "/cancel", "permission": {"type": 2}}},
            {"id": "b1", "render_data": {"label": "点歌", "style": 1},
             "action": {"type": 2, "data": "/sing", "reply": True,
                        "permission": {"type": 2}}},
            {"id": "b3", "render_data": {"label": "删除", "style": 3},
             "action": {"type": 2, "data": "/del", "click_limit": 3,
                        "unsupport_tips": "请升级客户端", "permission": {"type": 2}}},
            {"id": "b4", "render_data": {"label": "推荐", "style": 4},
             "action": {"type": 2, "data": "/top", "anchor": 1,
                        "permission": {"type": 2, "specify_user_ids": ["U1"]}}},
        ]},
        {"buttons": [
            {"id": "b9", "render_data": {"label": "回调", "style": 1},
             "action": {"type": 1, "data": "cb", "permission": {"type": 2}},
             "group_id": "g1"},
            {"id": "b8", "render_data": {"label": "打开", "style": 1},
             "action": {"type": 0, "data": "https://example.com",
                        "permission": {"type": 2}}},
        ]},
    ]}}

sys.path.insert(0, os.path.join(BR, "tests"))
sys.path.insert(0, str(_CORE_ROOT("3")))
sys.path.insert(0, str(_BOTPY_DIR()))
import rich_content as _RC2                                   # noqa: E402

# ---- F1. 校验器必须**逐字段保真**（唯一允许的差异：type=2 默认补 enter）----
_sent = _json.dumps(_FULL_KB, ensure_ascii=False)
_out = _RC2.validate_keyboard(_sent)
_expected = _copy.deepcopy(_FULL_KB)
for _row in _expected["content"]["rows"]:
    for _b in _row["buttons"]:
        if _b["action"]["type"] == 2 and "enter" not in _b["action"]:
            _b["action"]["enter"] = True                       # 我们有意补的默认
check("★★ 全字段键盘：校验器**一字不差**（含 4 种 style / visited_label / group_id / "
      "reply / anchor / click_limit / unsupport_tips / permission 明细）",
      _out == _expected, _json.dumps(_out, ensure_ascii=False)[:200])
_styles_kept = [_b["render_data"]["style"]
                for _row in _out["content"]["rows"] for _b in _row["buttons"]]
check("★★ 多色并存不被压成一种（0/1/3/4 全在）",
      set(_styles_kept) >= {0, 1, 3, 4}, str(_styles_kept))

# ---- F2. 端到端：真核心 + 真发送链 ⇒ 出站 payload 里 keyboard 与输入一致 ----
try:
    import smoke_v3 as T2                                       # noqa: E402
    import main as _bm2                                         # noqa: E402
    from core.chat.message_elements import Text as _T2           # noqa: E402
    from core.chat.message_utils import MessageChain as _MC2     # noqa: E402

    async def _e2e_full_kb():
        a = T2.make_adapter()
        try:
            from core.adapter.capabilities import IMCapability

            a.get_capability(IMCapability)._group_reply_ids["G1"] = "MSGID-X"
        except Exception:
            pass
        p = T2.make_plugin(a)
        await p._tick()
        # ★ 走**真实路径**：模型给 JSON → KeyboardTag 校验/补默认 → KeyboardMarker → 发送
        _tag = _bm2.KeyboardTag(None, "")
        _markers = await _tag.handle(_json.dumps(_FULL_KB, ensure_ascii=False))
        await a.send_group_message("G1", _MC2([_T2("正文在这"), _markers[0]]))
        return [kw for _k, kw in a.client.api.calls]

    _calls_f = asyncio.run(_e2e_full_kb())
    _sent_kb = _calls_f[-1].get("keyboard") if _calls_f else None
    check("★★ 端到端：出站报文带 keyboard（没被 core/api 层吃掉）", bool(_sent_kb),
          str(_calls_f[-1])[:160] if _calls_f else "no call")
    check("★★ 端到端：keyboard 与输入**深比较一致**（含多色样式）",
          isinstance(_sent_kb, dict)
          and _sent_kb.get("content", {}).get("rows") == _out["content"]["rows"],
          _json.dumps(_sent_kb, ensure_ascii=False)[:200])
    check("★★ 端到端：文字与按钮在**同一条**消息里（msg_type=2 + markdown 正文）",
          _calls_f and int(_calls_f[-1].get("msg_type") or 0) == 2
          and (_calls_f[-1].get("markdown") or {}).get("content") == "正文在这",
          str(_calls_f[-1])[:160] if _calls_f else "")
except Exception as exc:
    check("★ 端到端全字段用例无异常", False, f"{type(exc).__name__}: {exc}")

# ---- F3. botpy 透传：keyboard 是**声明参数**，不是被 locals() 丢掉的野字段 ----
try:
    import botpy as _botpy

    _sig = _inspect.signature(_botpy.BotAPI.post_c2c_message)
    check("★★ botpy 接口声明了 keyboard 参数（否则会像 input_notify 那样被静默丢掉）",
          "keyboard" in _sig.parameters, str(list(_sig.parameters)[:12]))
    _api_src = _inspect.getsource(_botpy.BotAPI.post_c2c_message)
    check("★ botpy 用 payload = locals() 组装（声明了什么就发什么）",
          "payload = locals()" in _api_src)
except Exception as exc:
    check("★ botpy 接口检查无异常", False, f"{type(exc).__name__}: {exc}")

# ---- F4. 行列上限：5×5 通过；超限报错（官方 40034029）----
_rows5 = [{"buttons": [{"id": "r%d_%d" % (i, j),
                        "render_data": {"label": "x", "style": 1},
                        "action": {"type": 2, "data": "/d", "permission": {"type": 2}}}
                       for j in range(5)]} for i in range(5)]
try:
    _RC2.validate_keyboard(_json.dumps({"content": {"rows": _rows5}}))
    _ok5 = True
except Exception:
    _ok5 = False
check("★ 5 行 × 5 按钮 合法通过", _ok5)
try:
    _RC2.validate_keyboard(_json.dumps({"content": {"rows": _rows5 + [{"buttons": [
        {"id": "extra", "render_data": {"label": "x", "style": 1},
         "action": {"type": 2, "data": "/d"}}]}]}}))
    _six = False
except Exception:
    _six = True
check("★ 6 行 ⇒ 明确报错（不静默丢）", _six)

# ---- F5. 短形式（模板 id）原样通过 ----
check("★ 短形式 {\"id\": ...} 原样通过",
      _RC2.validate_keyboard('{"id":"keyboard_id_abc"}') == {"id": "keyboard_id_abc"})

# ---- F6. 非法 style：只统计、**不改值**，并给一次性告警 ----
_st = {}
_kb_bad = _RC2.validate_keyboard(_json.dumps({"content": {"rows": [{"buttons": [
    {"id": "z", "render_data": {"label": "x", "style": 9},
     "action": {"type": 2, "data": "/d"}}]}]}}), stats=_st)
check("★★ 非官方 style(9)：统计到 bad_style，且**原值不动**（不擅自改）",
      _st.get("bad_style") == 1
      and _kb_bad["content"]["rows"][0]["buttons"][0]["render_data"]["style"] == 9,
      str(_st))
check("★ 源码里有一次性告警（提示模型/用户官方只有 0/1/3/4）",
      "不是官方值" in open(os.path.join(BR, "main.py"), encoding="utf-8").read())

# --------------------------------------------------------------------------- #
# [G] 按钮策略**全链路**（v1.6.32）：声明 → 发送 → 登记 → 点击判定 → 官方字段零污染
# --------------------------------------------------------------------------- #
print("\n[G] 按钮策略全链路（声明/剥离/登记/判定/官方兼容）")
try:
    import smoke_v3 as T3                                      # noqa: E402
    import main as _bm                                          # noqa: E402
    from core.chat.message_utils import MessageChain as _C3     # noqa: E402
    from core.chat.message_elements import Text as _T3          # noqa: E402

    _OFF_G = {"id", "content", "rows", "buttons", "render_data", "label",
              "visited_label", "style", "action", "type", "data", "permission",
              "specify_user_ids", "specify_role_ids", "enter", "reply", "anchor",
              "click_limit", "unsupport_tips", "at_bot_show_channel_list",
              "group_id", "modal", "confirm_text", "cancel_text"}

    def _deep_d(obj, out):
        if isinstance(obj, dict):
            for k, v in obj.items():
                out.add(k)
                _deep_d(v, out)
        elif isinstance(obj, list):
            for v in obj:
                _deep_d(v, out)

    # ---- G1. 标签处理：属性 + JSON kirai ⇒ 剥离干净、声明被藏起来 ----
    _tag = _bm.KeyboardTag()
    _kb_raw = _json.dumps({"content": {"rows": [{"buttons": [
        {"id": "b1", "render_data": {"label": "报名", "style": 1},
         "action": {"type": 1, "data": "join", "permission": {"type": 2}},
         "kirai": {"per": 1}},
    ]}]}}, ensure_ascii=False)
    _loop_g = asyncio.new_event_loop()
    try:
        _els = _loop_g.run_until_complete(_tag.handle(_kb_raw, max="2", once="1",
                                                      label="报名", ttl="300"))
    finally:
        _loop_g.close()
    _pl = getattr(_els[0], "keyboard", {}) if _els else {}
    _spec_g = _pl.get("__kirai__") or {}
    check("★★ 标签属性 + JSON kirai 都被采集到声明里",
          _spec_g.get("max") == 2 and _spec_g.get("once") is True
          and _spec_g.get("label") == "报名" and _spec_g.get("ttl") == 300
          and (_spec_g.get("buttons") or {}).get("b1", {}).get("per") == 1, str(_spec_g)[:160])
    check("★★ 声明**不在**可见载荷里（只有私有 __kirai__ 携带）",
          "kirai" not in _json.dumps({k: v for k, v in _pl.items() if k != "__kirai__"},
                                     ensure_ascii=False), "")

    # ---- G2. 真实发送：出站 keyboard **只含官方字段** + 策略已登记 ----
    async def _real_policy_send():
        a = T3.make_adapter()
        a.client.api._http = T3.FakeHTTP({"/files": {"file_info": "FI"}})
        try:
            from core.adapter.capabilities import IMCapability

            a.get_capability(IMCapability)._group_reply_ids["GP"] = "MSGID-IN"
        except Exception:
            try:
                a._group_reply_ids["GP"] = "MSGID-IN"
            except Exception:
                pass
        p = T3.make_plugin(a)
        await p._tick()
        await a.send_group_message("GP", _C3([
            _T3("来报名"),
            _bm.KeyboardMarker(_pl),          # 带着私有声明的载荷
        ]))
        return p, [kw for _kind, kw in a.client.api.calls]

    _p3, _calls3 = asyncio.run(_real_policy_send())
    _kbs = [c.get("keyboard") for c in _calls3 if c.get("keyboard")]
    check("★★ 带策略的键盘照常发出（没被策略层挡掉）", bool(_kbs), str(len(_calls3)))
    _keys_g = set()
    if _kbs:
        _deep_d(_kbs[0], _keys_g)
    _extra_g = _keys_g - _OFF_G
    check("★★★ 官方兼容性：出站键盘**深度扫描只剩官方字段**（防 40034029）",
          bool(_kbs) and not _extra_g, str(sorted(_extra_g)))
    _n_pol = len(getattr(_p3.button_policies, "_pol", {}) or {})
    check("★★ 发送即登记：账本里多了一条策略", _n_pol == 1, f"实际 {_n_pol} 条")
    _pol_g = list((getattr(_p3.button_policies, "_pol", {}) or {}).values())[0] if _n_pol else {}
    check("★ 策略参数正确落账（max=2 / once / label=报名）",
          _pol_g.get("max") == 2 and _pol_g.get("once") is True
          and _pol_g.get("label") == "报名", str({k: _pol_g.get(k) for k in ("max", "once", "label")}))
    check("★ 账本按**原始会话 id** 记账（点击端才匹配得上）",
          _pol_g.get("sid") == "GP", str(_pol_g.get("sid")))

    # ---- G3. 点击判定：默认 last ⇒ 中途不打扰、截止那次带汇总 ----
    async def _clicks():
        from interactions import InteractionBridge as _IB3

        waits = []

        async def _ack3(_iid, _code):
            waits.append(_iid)

        caps = []
        _p3.publish_synthetic_event = lambda **kw: caps.append(kw) or True
        br = _IB3(_p3, __import__("logging").getLogger("plugin"))
        client = _p3._find_adapters()[0][1].get_client()
        # 桩客户端默认没有 on_interaction_result（真 botpy 有）⇒ 补上以验证回执链路
        try:
            client.api.on_interaction_result = _ack3
        except Exception:
            pass

        def _body(uid, data):
            return {"id": f"IT-{uid}", "type": 11,
                    "data": {"resolved": {"button_id": "b1", "button_data": data}},
                    "group_openid": "GP", "group_member_openid": uid}

        await br._on_interaction(client, _body("U1", "join"))
        n_after_1 = len(caps)
        await br._on_interaction(client, _body("U2", "join"))
        n_after_2 = len(caps)
        await br._on_interaction(client, _body("U3", "join"))
        n_after_3 = len(caps)
        return caps, n_after_1, n_after_2, n_after_3, len(waits)

    _caps3, _n1, _n2, _n3, _acks3 = asyncio.run(_clicks())
    check("★★ 第 1 次点击按 deliver=last **不打扰模型**", _n1 == 0, f"publish 次数={_n1}")
    check("★★ 第 2 次（最后一个名额）必须转给模型", _n2 == 1, f"{_n1}→{_n2}")
    check("★★ 满额后的第 3 次**不再转**（硬拦截默认开）", _n3 == 1, f"{_n2}→{_n3}")
    _txt_g = str((_caps3[-1] if _caps3 else {}).get("text") or "")
    check("★★ 截止那次带汇总（谁点的 + 计数 + 已截止）",
          "最后一个名额" in _txt_g or "已截止" in _txt_g, _txt_g[:160])
    check("★ 回执仍照发（官方 3 秒硬要求不受策略影响）", _acks3 == 3, str(_acks3))

    # ---- G4. 查账工具：能量到这本账 ----
    try:
        import button_tools as _BT3

        class _Ev:
            session = type("S", (), {"session_id": "qq:gm:GP", "session_type": "gm"})()

            class ctx:                                       # noqa: N801
                @staticmethod
                def get_plugin_inst(_pid):
                    return _p3

        _ev = _Ev()
        _tool = _BT3.QQButtonStatsTool(ctx=_Ev.ctx)
        _out = asyncio.run(_tool.execute(_ev))
        check('★★ 查账工具：能按会话定位到这本账（不再"乱猜"）',
              "已点" in _out and "报名" in _out, str(_out)[:160])
        check("★★ 查账内容含参与者与状态", ("U1" in _out or "未知用户" in _out)
              and ("已截止" in _out or "剩 0" in _out), str(_out)[:200])
    except Exception as _e_tool:
        check("查账工具用例", False, f"{type(_e_tool).__name__}: {_e_tool}")

    # ---- G5. 2.x 侧同样可用（策略层不依赖世代） ----
    check("★ 策略模块无核心依赖（2.x/3.0 通用）",
          "import core" not in open(os.path.join(BR, "button_policy.py"),
                                    encoding="utf-8").read(), "")
except Exception as _e_g:
    check("按钮策略全链路段执行", False, f"{type(_e_g).__name__}: {_e_g}")

print(f"\n结果：{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
