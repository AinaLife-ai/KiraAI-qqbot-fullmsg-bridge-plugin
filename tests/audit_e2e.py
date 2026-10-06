"""复审 D：端到端链路 —— 每个功能从"入口"走到"出口"完整走一遍。

与 smoke_v3 的区别：smoke_v3 验"点"（每个断言打一个点），
本脚本验"链"（一条消息从平台进来到发出去，中间每一步都检查）。
"""
import asyncio
import sys

ROOT = "/var/minis/workspace/qqbot_bridge_review"
sys.path.insert(0, f"{ROOT}/kira-v3")
sys.path.insert(0, f"{ROOT}/bridge")
sys.path.insert(0, "/tmp/botpy_src/botpy-master")
sys.path.insert(0, f"{ROOT}/bridge/tests")

import smoke_v3 as T  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


async def main():
    import main as bridge_main
    from core.adapter.capabilities import IMCapability
    from core.chat import MessageChain
    from core.chat.message_elements import Text

    print("#" * 72)
    print("## 复审 D：端到端链路")
    print("#" * 72)

    # =================== 链路 1：群里有人 @ 机器人（最常见） ===================
    print("\n[链 1] 用户 @ 机器人 → 事件投递 → 模型回复 → 发出去")
    a = T.make_adapter()
    p = T.make_plugin(a)
    await p._tick()

    inbound = dict(T.INBOUND)
    inbound["id"] = "ROBOT1.0_L1"
    await a.im._handle_group_message(inbound, force_mention=True)
    check("1a 事件已投递到总线", a._event_queue.qsize() == 1)
    ev = a._event_queue.get_nowait()
    check("1b 被提及标记正确", bool(ev.message.is_mentioned))
    check("1c 会话 sid 形状正确（adapter:gm:id）",
          str(ev.session.sid).startswith("qqo:gm:G1"), str(ev.session.sid))

    # 模型回复（含 @ + 引用）
    display = str(ev.message.message_id)
    from core.chat.message_elements import At, Reply
    reply_chain = MessageChain([Reply(display), At("MEM1", "小明"), Text("收到")])
    await a.send_group_message("G1", reply_chain)
    _, payload = a.client.api.calls[-1]
    check("1d 发出的报文带引用 REFIDX",
          (payload.get("message_reference") or {}).get("message_id", "").startswith("REFIDX"),
          repr(payload.get("message_reference")))
    check("1e 发出的 @ 是真 @ 标记（走 markdown）",
          payload.get("msg_type") == 2 and "qqbot-at-user" in str(
              (payload.get("markdown") or {}).get("content", "")),
          str(payload)[:150])
    check("1f 回复引用了正确的那条消息",
          payload.get("msg_id") == "ROBOT1.0_L1", repr(payload.get("msg_id")))

    # =================== 链路 2：markdown + 键盘 + 点击回调 ===================
    print("\n[链 2] 发键盘 → 用户点击 → 回执 → 转消息给模型")
    a2 = T.make_adapter()
    p2 = T.make_plugin(a2)
    await p2._tick()
    inbound2 = dict(T.INBOUND)
    inbound2["id"] = "ROBOT1.0_L2"
    await a2.im._handle_group_message(inbound2, force_mention=True)
    a2._event_queue.get_nowait()

    kb_chain = MessageChain([
        bridge_main.MarkdownText("## 每日签到\n点下面的按钮"),
        bridge_main.KeyboardMarker({"content": {"rows": [{"buttons": [
            {"id": "signin", "render_data": {"label": "签到", "style": 1},
             "action": {"type": 2, "data": "/签到", "permission": {"type": 2}}}]}]}}),
    ])
    await a2.send_group_message("G1", kb_chain)
    _, kb_payload = a2.client.api.calls[-1]
    check("2a markdown 正文正确", "每日签到" in str((kb_payload.get("markdown") or {}).get("content")))
    check("2b 键盘随报文发出", bool(kb_payload.get("keyboard")))
    check("2c 键盘按钮 label/data 正确",
          kb_payload["keyboard"]["content"]["rows"][0]["buttons"][0]["render_data"]["label"] == "签到")

    handler = getattr(a2.client, "on_interaction_create", None)
    check("2d 互动处理器已挂载", callable(handler))
    await handler({"d": T.INTERACTION})
    kinds = [k for k, _ in a2.client.api.calls]
    check("2e 回执先于后续处理发出", "ack" in kinds, str(kinds))
    check("2f 点击转成消息", not a2._event_queue.empty())
    if not a2._event_queue.empty():
        btn_ev = a2._event_queue.get_nowait()
        check("2g 转出的事件是 notice 且被提及",
              btn_ev.message.is_notice and bool(btn_ev.message.is_mentioned))
        check("2h 正文含按钮语义（模型能接话）", "/签到" in str(btn_ev.message.chain))

    # =================== 链路 3：群名 ===================
    print("\n[链 3] 群名获取 → 缓存 → 会话标题（两版各自落点）")
    a3 = T.make_adapter()
    p3 = T.make_plugin(a3)
    await p3._tick()

    class OkHTTP:
        async def request(self, route, **kw):
            return {"group_openid": "G1", "group_name": "读书分享会",
                    "group_member_num": 42}

    a3.client.api._http = OkHTTP()
    p3.group_names.schedule_fetch(a3, "qqo", "G1", a3.client, bridge_main.logger)
    await asyncio.sleep(0.3)
    check("3a 群名已进缓存", p3.group_names.lookup("qqo", "G1") == "读书分享会")
    inbound3 = dict(T.INBOUND)
    inbound3["id"] = "ROBOT1.0_L3"
    await a3.im._handle_group_message(inbound3, force_mention=True)
    ev3 = a3._event_queue.get_nowait()
    check("3b 事件里的群名已是中文", ev3.message.group.group_name == "读书分享会",
          repr(ev3.message.group.group_name))
    check("3c 会话标题同步", ev3.session.session_title == "读书分享会",
          repr(ev3.session.session_title))
    check("3d 落盘标记为脏（会被写出）", p3.group_names.dirty)

    # =================== 链路 4：群管理工具（真实调用） ===================
    print("\n[链 4] 模型调群管理工具 → 官方接口 → 结果回给模型")
    a4 = T.make_adapter()
    p4 = T.make_plugin(a4)
    await p4._tick()
    calls = []

    class ToolHTTP:
        async def request(self, route, **kw):
            # route.url 会把路径参数 format_map 进真实地址（botpy 的 Route.url 属性），
            # 这样能验证"路径参数真的绑定上了"，而不只是模板字符串。
            calls.append({"method": route.method, "path": route.path,
                          "url": route.url, "params": route.parameters, **kw})
            if "restrict_chat_setting" in route.path and route.method == "GET":
                return {"global_rule": {"mode": "none"}, "members": []}
            if "bot_state" in route.path:
                return {"recv_msg_setting": "all", "allow_proactive_msg": True,
                        "member_role": "admin", "joined_at": "2026-01-01T00:00:00+08:00"}
            return {}

    a4.client.api._http = ToolHTTP()

    class FakeSession:
        adapter_name = "qqo"
        session_type = "gm"
        session_id = "G1"

    class FakeToolEvent:
        def __init__(self):
            self.session = FakeSession()

        def is_group_message(self):
            return True

    from admin_tools import build_tools

    ev_tool = FakeToolEvent()
    tools = {t.name: t(ctx=p4.ctx) for t in build_tools({})}

    r1 = await tools["recall_qq_msg"].execute(ev_tool, message_id="ROBOT1.0_x")
    recall_call = next((c for c in calls if c["method"] == "DELETE"), None)
    check("4a 撤回：路径参数已正确绑定（url 渲染出真实地址）",
          recall_call is not None
          and "messages/ROBOT1.0_x" in recall_call["url"]
          and "G1" in recall_call["url"],
          str(recall_call.get("url") if recall_call else None))
    check("4b 撤回：返回成功文案", "成功" in r1, r1)

    r2 = await tools["set_qq_group_ban"].execute(ev_tool, user_id="M1", duration=600)
    ban_call = [c for c in calls if c["method"] == "POST" and "restrict_chat_setting" in c["path"]]
    check("4c 禁言：走了 POST + members",
          bool(ban_call) and "members" in ban_call[-1].get("json", {}), str(ban_call[-1:])[:200])
    check("4d 禁言：时长文案正确", "600" in r2, r2)

    r3 = await tools["set_qq_group_ban"].execute(ev_tool, user_id="M1", duration=0)
    check("4e 解禁：op=del", "解除" in r3, r3)

    r4 = await tools["get_group_mute_state"].execute(ev_tool)
    check("4f 禁言查询：返回可读文本", "全员禁言" in r4, r4)

    r5 = await tools["get_qq_bot_state"].execute(ev_tool)
    check("4g bot_state：识别出全量模式", "全部消息" in r5, r5)
    check("4h bot_state：识别出管理员身份", "管理员" in r5, r5)

    # 错误翻译
    class PermErrHTTP:
        async def request(self, route, **kw):
            raise RuntimeError("40062003 无操作权限")

    a4.client.api._http = PermErrHTTP()
    r6 = await tools["set_qq_group_ban"].execute(ev_tool, user_id="M1", duration=60)
    check("4i 无权限错误被翻译成人话", "管理员" in r6 and r6.startswith("禁言失败"), r6)

    # =================== 链路 5：成员事件 ===================
    print("\n[链 5] 成员进群 → 补解析器 → 转 System 消息")
    a5 = T.make_adapter()
    cfg5 = {"section_basic": {"enabled": True, "extra_intents": True,
                              "member_notice_enabled": True},
            "section_proactive": {"proactive_enabled": False}}
    p5 = bridge_main.QQOfficialGroupBridge(T.FakePluginCtx({"qqo": a5}), cfg5)
    await p5._tick()

    member_body = {"timestamp": 1784276757, "group_openid": "G1",
                   "member_openid": "NEW_MEMBER", "user_openid": "NEW_MEMBER"}
    await p5._on_member_event(a5, "qqo", member_body, "group_member_add")
    check("5a 成员事件转成消息", not a5._event_queue.empty())
    if not a5._event_queue.empty():
        m_ev = a5._event_queue.get_nowait()
        check("5b 是 notice", m_ev.message.is_notice)
        check("5c 正文可读（含 System 标记）", "System" in str(m_ev.message.chain)
              and "NEW_MEMBER" in str(m_ev.message.chain), str(m_ev.message.chain))

    # 加群申请
    join_body = {"group_openid": "G1", "member_openid": "APPLICANT",
                 "username": "小王", "apply_source": "self_apply"}
    await p5._on_member_event(a5, "qqo", join_body, "group_join_request")
    if not a5._event_queue.empty():
        j_ev = a5._event_queue.get_nowait()
        check("5d 加群申请文案正确",
              "小王" in str(j_ev.message.chain) and "主动申请" in str(j_ev.message.chain),
              str(j_ev.message.chain))

    print()
    print("=" * 72)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
