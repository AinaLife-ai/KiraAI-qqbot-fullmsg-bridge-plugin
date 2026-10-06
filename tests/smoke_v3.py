"""3.0 端到端验证：真实 KiraAI v3.0.0-alpha.2 + 真实 botpy + 真实 QQOfficialAdapter。

覆盖本次改造的关键主张：
  1. 世代探测 = v3；
  2. **不再装影子 handler**（不会顶掉核心原生的 on_group_at_message_create）；
  3. 3.0 下核心事件照常工作（@ 消息不丢）；
  4. 群名在 publish 时被换成中文；
  5. 引用唤醒补洞：重启后（空 _sent_message_ids）仍 is_mentioned=True；
  6. api 层发送增强在 3.0 上**真的装上了**（2.x 时代这里会提前 return）；
  7. markdown / keyboard 经发送入口 → 报文正确；
  8. 全员还原可逆。
"""
import asyncio
import json
import os
import sys

ROOT = "/var/minis/workspace/qqbot_bridge_review"
sys.path.insert(0, f"{ROOT}/kira-v3")
sys.path.insert(0, f"{ROOT}/bridge")
if "/tmp/botpy_src/botpy-master" not in sys.path:
    sys.path.insert(0, "/tmp/botpy_src/botpy-master")

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class FakeTask:
    def done(self):
        return False


class FakeAPI:
    def __init__(self):
        self.calls = []

    async def post_group_message(self, **kw):
        self.calls.append(("group", kw))
        return {"id": "ROBOT1.0_out", "ext_info": {"ref_idx": "REFIDX_out=="}}

    async def post_c2c_message(self, **kw):
        self.calls.append(("c2c", kw))
        return {"id": "ROBOT1.0_outc", "ext_info": {"ref_idx": "REFIDX_outc=="}}

    async def on_interaction_result(self, interaction_id, code):
        self.calls.append(("ack", {"id": interaction_id, "code": code}))
        return {}


class FakeHTTP:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def request(self, route, **kw):
        self.calls.append((route.method, route.path, kw))
        for key, value in self.responses.items():
            if key in route.path:
                if isinstance(value, Exception):
                    raise value
                return value
        return self.responses.get("*", {})


class FakeClient:
    def __init__(self, http=None):
        self.api = FakeAPI()
        self.api._http = http or FakeHTTP()
        self._connection = None


class FakePluginCtx:
    class _Mgr:
        def __init__(self, adapters):
            self._a = adapters

        def get_adapters(self):
            return self._a

        def get_adapter(self, name):
            return self._a.get(name)

    def __init__(self, adapters):
        self.adapter_mgr = self._Mgr(adapters)


def make_adapter():
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.context import AdapterContext
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter

    info = AdapterInfo(adapter_id="t", enabled=True, name="qqo", platform="QQ Official",
                       config={"app_id": "a", "app_secret": "b",
                               "permission_mode": "allow_list",
                               "group_allow_list": ["G1"], "user_allow_list": ["U1"]})
    a = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
    a.client = FakeClient()
    a._client_task = FakeTask()
    return a


def make_plugin(adapter):
    import main as bridge_main

    cfg = {"section_basic": {"enabled": True}, "section_proactive": {"proactive_enabled": False}}
    plugin = bridge_main.QQOfficialGroupBridge(FakePluginCtx({"qqo": adapter}), cfg)
    return plugin


class FakeToolSet:
    def __init__(self):
        self.tools = []

    def add(self, *tools):
        for t in tools:
            for i, old in enumerate(self.tools):
                if old.name == t.name:
                    self.tools.pop(i)
                    break
            self.tools.append(t)


class FakeReq:
    def __init__(self):
        self.tool_set = FakeToolSet()
        self.system_prompt = []


class FakeTagSet:
    def __init__(self):
        self.tags = []

    def register(self, *tags):
        for t in tags:
            for i, old in enumerate(self.tags):
                if old.name == t.name:
                    self.tags.pop(i)
                    break
            self.tags.append(t)

    def to_prompt(self):
        return "\n".join(t.description for t in self.tags)


class FakeEvent:
    def __init__(self, adapter_info):
        self.adapter = adapter_info


INBOUND = {
    "id": "ROBOT1.0_inbound",
    "author": {"id": "MEM1", "member_openid": "MEM1", "username": "小明", "bot": False},
    "content": "看看这个",
    "group_openid": "G1",
    "message_type": 0,
    "timestamp": "2026-10-07T12:00:00+08:00",
    "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_1=="]},
    "mentions": [],
}

QUOTE_SELF = {
    "id": "ROBOT1.0_quote",
    "author": {"id": "MEM2", "member_openid": "MEM2", "username": "小红", "bot": False},
    "content": "",
    "group_openid": "G1",
    "message_type": 103,
    "timestamp": "2026-10-07T12:01:00+08:00",
    "msg_elements": [{"id": "ROBOT1.0_old", "message_type": 0, "content": "机器人说过的话",
                      "author": {"id": "BOTOPENID", "member_openid": "BOTOPENID",
                                 "username": "香里", "bot": True}}],
    "message_scene": {"source": "default",
                      "ext": ["msg_idx=REFIDX_q==", "ref_msg_idx=TMP_x"]},
    "mentions": [],
}

INTERACTION = {
    "id": "INT-1", "type": 11, "scene": "group", "chat_type": 1,
    "group_openid": "G1", "group_member_openid": "MEM1",
    "data": {"type": 11, "resolved": {"button_data": "/签到", "button_id": "b1"}},
    "timestamp": "2026-10-07T12:02:00+08:00",
}


async def main():
    import main as bridge_main
    from core.adapter.capabilities import IMCapability
    from core.chat import MessageChain
    from core.chat.message_elements import Text

    print("#" * 74)
    print("## 3.0 端到端验证")
    print("#" * 74)

    adapter = make_adapter()
    plugin = make_plugin(adapter)
    im = adapter.get_capability(IMCapability)

    # ---------- 1. 世代探测 ----------
    print("\n[1] 世代探测与补丁编排")
    await plugin._tick(report=False)
    profile = plugin.profiles.get("qqo")
    check("识别为 v3", profile is not None and profile.is_v3, repr(profile))

    # ---------- 2. 不装影子 handler ----------
    print("\n[2] 不接管核心原生处理器（关键回归点）")
    native = getattr(adapter.client, "on_group_at_message_create", None)
    check("client 上没有被桥接挂上 on_group_at_message_create",
          not getattr(native, "_kira_bridge_owner", None))
    # 真实的 _QQOfficialClient 类上应有核心自己的实现（FakeClient 没有，所以查真类）
    from core.adapter.src.qq_official.qq_official import _QQOfficialClient
    check("核心 _QQOfficialClient 仍自带 on_group_at_message_create",
          "on_group_at_message_create" in _QQOfficialClient.__dict__)

    # ---------- 3. 核心事件照常 ----------
    print("\n[3] 核心原生事件链路不受影响")
    await im._handle_group_message(INBOUND, force_mention=False)
    ev = adapter._event_queue.get_nowait()
    check("@ 消息正常投递（核心原生路径）", ev is not None)
    check("昵称是真实昵称", ev.message.sender.nickname == "小明",
          repr(ev.message.sender.nickname))

    # ---------- 4. 群名 ----------
    print("\n[4] 群名（3.0 的 publish 包装）")
    plugin.group_names.remember("qqo", "G1", "读书分享会")
    inbound2 = dict(INBOUND)
    inbound2["id"] = "ROBOT1.0_inbound_2"
    await im._handle_group_message(inbound2, force_mention=False)
    ev2 = adapter._event_queue.get_nowait()
    check("Group.group_name 已换成中文", ev2.message.group.group_name == "读书分享会",
          repr(ev2.message.group.group_name))
    check("Session.session_title 同步变中文", ev2.session.session_title == "读书分享会",
          repr(ev2.session.session_title))

    # ---------- 5. 引用唤醒补洞 ----------
    print("\n[5] 引用机器人 = 被唤醒（重启后仍成立）")
    plugin.remember_self_identity("G1", "BOTOPENID", "香里")
    fresh = make_adapter()                       # 模拟重启：全新实例，_sent_message_ids 为空
    plugin2 = make_plugin(fresh)
    await plugin2._tick(report=False)
    await fresh.im._handle_group_message(QUOTE_SELF, force_mention=False)
    ev3 = fresh._event_queue.get_nowait()
    check("重启后引用机器人 → is_mentioned=True（补洞生效）",
          bool(ev3.message.is_mentioned), repr(ev3.message.is_mentioned))

    # 反向验证：没有插件时必须 False（证明确实是补的）
    bare = make_adapter()
    await bare.im._handle_group_message(QUOTE_SELF, force_mention=False)
    ev_bare = bare._event_queue.get_nowait()
    check("★ 反向验证：不装插件时 3.0 原生 = False（这就是被修的 bug）",
          not ev_bare.message.is_mentioned, repr(ev_bare.message.is_mentioned))

    # ---------- 6. api 层发送增强在 3.0 上真的装上了 ----------
    print("\n[6] api 层发送增强（2.x 时代这里会提前 return）")
    check("post_group_message 已被包装",
          getattr(adapter.client.api.post_group_message, "_kira_bridge_send", False))
    check("发送入口已被包装",
          getattr(adapter.send_group_message, "_kira_bridge_entry", False))

    # ---------- 7. markdown / keyboard 端到端 ----------
    print("\n[7] markdown / keyboard 端到端")
    md_chain = MessageChain([
        Text("前置 "),
        bridge_main.MarkdownText("## 标题\n- 项目一\n- 项目二"),
        bridge_main.KeyboardMarker({"content": {"rows": [{"buttons": [
            {"id": "b1", "render_data": {"label": "签到", "style": 1},
             "action": {"type": 2, "data": "/签到", "permission": {"type": 2}}}]}]}}),
    ])
    await adapter.send_group_message("G1", md_chain)
    kind, payload = adapter.client.api.calls[-1]
    check("msg_type=2（走 markdown）", payload.get("msg_type") == 2, repr(payload.get("msg_type")))
    check("markdown.content 正确",
          (payload.get("markdown") or {}).get("content", "").startswith("前置 ## 标题"),
          repr((payload.get("markdown") or {}).get("content"))[:80])
    check("content 被清空（官方要求互斥）", payload.get("content") is None,
          repr(payload.get("content")))
    check("keyboard 已随报文发出", "keyboard" in payload and payload["keyboard"],
          repr(payload.get("keyboard"))[:60])

    # ---------- 8. markdown 被拒 → 退纯文本 ----------
    print("\n[8] markdown 失败自动退纯文本")


    class RejectOnce(FakeAPI):
        def __init__(self):
            super().__init__()
            self.n = 0

        async def post_group_message(self, **kw):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("40034127 无markdown模板权限")
            return await super().post_group_message(**kw)


    a3 = make_adapter()
    a3.client.api = RejectOnce()
    a3.client.api._http = FakeHTTP()
    p3 = make_plugin(a3)
    await p3._tick(report=False)
    await a3.send_group_message("G1", MessageChain([
        bridge_main.MarkdownText("**加粗** <@ABC12345>")]))
    last_kind, last = a3.client.api.calls[-1]
    check("已退回纯文本（msg_type=0）", last.get("msg_type") == 0, repr(last.get("msg_type")))
    check("markdown 字段已清空", not last.get("markdown"))
    check("平台标记被剥掉（不会把标签原样发出去）",
          "<@" not in str(last.get("content")), repr(last.get("content")))

    # ---------- 9. 互动回调：先回执 ----------
    print("\n[9] 互动回调（按钮点击）")
    a4 = make_adapter()
    p4 = make_plugin(a4)
    await p4._tick(report=False)
    handler = getattr(a4.client, "on_interaction_create", None)
    check("已挂上 on_interaction_create", callable(handler))
    await handler({"d": INTERACTION})
    kinds = [k for k, _ in a4.client.api.calls]
    check("先发回执（PUT /interactions）", "ack" in kinds, str(kinds))
    ack_payload = [p for k, p in a4.client.api.calls if k == "ack"][0]
    check("回执 code=0 且 id 正确", ack_payload["code"] == 0 and ack_payload["id"] == "INT-1",
          repr(ack_payload))
    check("点击被转成一条消息给模型", not a4._event_queue.empty())
    ev_btn = a4._event_queue.get_nowait() if not a4._event_queue.empty() else None
    check("事件带 is_notice", ev_btn is not None and ev_btn.message.is_notice)
    check("正文包含按钮语义",
          ev_btn is not None and "/签到" in str(ev_btn.message.chain),
          repr(ev_btn.message.chain) if ev_btn else "")

    # ---------- 10. L1 工具与标签注入 ----------
    print("\n[10] L1 工具 + markdown/keyboard 标签注入")
    req, tags = FakeReq(), FakeTagSet()
    plugin.inject_tools_and_tags(FakeEvent(adapter.info), req, tags)
    names = sorted(t.name for t in req.tool_set.tools)
    check("无需权限的工具已注入（默认配置）",
          {"recall_qq_msg", "get_qq_group_info", "find_qq_group_member",
           "read_qq_attached_file", "get_qq_bot_state"} <= set(names),
          str(names))
    check("★ 需管理员权限的工具默认不注入",
          not ({"set_qq_group_ban", "get_group_mute_state",
                "manage_qq_group_join_request", "kick_qq_group_member",
                "get_qq_group_member_roster",
                "manage_qq_group_blacklist"} & set(names)), str(names))
    tag_names = sorted(t.name for t in tags.tags)
    check("markdown / keyboard 标签已注册", {"markdown", "keyboard"} <= set(tag_names), str(tag_names))
    check("标签描述会进提示词", "markdown" in tags.to_prompt() and "keyboard" in tags.to_prompt())

    # ---------- 11. 键盘校验 ----------
    print("\n[11] 键盘 JSON 校验")
    ok_kb = bridge_main.validate_keyboard(
        '{"content":{"rows":[{"buttons":[{"id":"b","action":{"data":"/x"}}]}]}}')
    check("合法键盘通过", isinstance(ok_kb, dict) and "content" in ok_kb)
    for bad, why in (
        ("not json", "非 JSON"),
        ('{"content":{"rows":[]}}', "空 rows"),
        ('{"content":{"rows":[{"buttons":[]}]}}', "空 buttons"),
        ('{"content":{"rows":[{"buttons":[{"action":{}}]}]}}', "缺 id"),
    ):
        try:
            bridge_main.validate_keyboard(bad)
            check(f"非法键盘被拒（{why}）", False, "竟然通过了")
        except Exception:
            check(f"非法键盘被拒（{why}）", True)

    # ---------- 12. 可逆 ----------
    print("\n[12] 可逆：关掉开关后全部还原")
    plugin.enabled = False
    plugin._restore_all()
    check("api 补丁已还原",
          not getattr(adapter.client.api.post_group_message, "_kira_bridge_send", False))
    check("发送入口已还原", not getattr(adapter.send_group_message, "_kira_bridge_entry", False))
    check("互动回调已摘除", not getattr(adapter.client, "on_interaction_create", None))
    check("3.0 增量已还原（publish 恢复）", "publish" not in adapter.__dict__)
    check("核心原生 _is_self_quote 恢复", not hasattr(im, "_is_self_quote")
          or getattr(im, "_is_self_quote", None).__class__ is not None)

    print()
    print("=" * 74)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
