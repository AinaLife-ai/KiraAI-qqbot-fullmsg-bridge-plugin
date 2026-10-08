"""把「成员事件 / 加群申请」在**模型眼里长什么样**完整跑出来。

用户问：「bot 是怎么看到提醒并审批的呢？她（LLM）眼中是什么样的」
—— 这个文件就是答案，而且可重复执行。

三段链路：
  ① 平台推事件 → 我们渲染成一行 `[System ...]` 文本 → 作为一条 notice 消息进对话
  ② 模型看到有人申请 → 调 `manage_qq_group_join_request(action="list")`
  ③ 模型拿到 member_id → 调 `action="approve"/"decline"`
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", _CORE_ROOT("2")))
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))
_BOTPY = os.environ.get("BOTPY_PATH", _BOTPY_DIR())
if os.path.isdir(_BOTPY):
    sys.path.insert(0, _BOTPY)

PASS = FAIL = 0
SEEN = {}


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def main():
    print("=" * 74)
    print("## 模型（LLM）眼中的「成员事件 / 加群申请」—— 完整链路实测")
    print("=" * 74)

    from group_names import GroupInfoCache
    import qqbot_bridge as B
    import admin_tools as AT
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter

    gn = GroupInfoCache(path=None)
    gn.remember("qqo", "G1", "读书分享会")     # 模拟白名单已过、群名可用

    # ------------------------------------------------------------------ #
    print("\n" + "─" * 74)
    print("① 平台推事件 → 我们渲染成 notice 文本（模型在对话里看到的原文）")
    print("─" * 74)
    cases = [
        (B.EVENT_GROUP_JOIN_REQUEST, {
            "group_openid": "G1", "member_openid": "FE003FAF76C4817251FDC128A16753BB",
            "username": "小明", "apply_source": "self_apply",
            "join_request_id": "AURi8Rr6", "bot": False,
            "verify_info": {"method": "verify_message",
                            "verify_message": "我是群主的老同学"}}),
        (B.EVENT_GROUP_MEMBER_ADD, {"group_openid": "G1", "member_openid": "C3D4E5F6"}),
        (B.EVENT_GROUP_MEMBER_REMOVE, {"group_openid": "G1", "member_openid": "C3D4E5F6"}),
    ]
    for ev_name, body in cases:
        txt = B.describe_member_event(ev_name, body, gn, "qqo", identities=None)
        print(f"    {ev_name:26s} →  {txt}")
        SEEN[ev_name] = txt

    check("★ 加群申请里带上了**申请人昵称**（不是纯 openid）",
          "小明" in SEEN[B.EVENT_GROUP_JOIN_REQUEST],
          SEEN[B.EVENT_GROUP_JOIN_REQUEST])
    check("★ 加群申请里带上了**群名**（不是 openid）",
          "读书分享会" in SEEN[B.EVENT_GROUP_JOIN_REQUEST])
    check("★ 加群申请里带上了**申请来源**（主动/被邀请）",
          "主动申请" in SEEN[B.EVENT_GROUP_JOIN_REQUEST])
    check("★ 前缀是 [System ...]（模型能识别这是系统通知而非某人在说话）",
          SEEN[B.EVENT_GROUP_JOIN_REQUEST].startswith("[System"))
    # ★ 用户 2026-10-07：成员进出通知也要**带上 openid**（不只是裸 id 或什么都没有）
    add_txt = SEEN[B.EVENT_GROUP_MEMBER_ADD]
    rm_txt = SEEN[B.EVENT_GROUP_MEMBER_REMOVE]
    check("★ 成员加入通知带 member_openid", "member_openid=C3D4E5F6" in add_txt, add_txt)
    check("★ 成员退出通知带 member_openid", "member_openid=C3D4E5F6" in rm_txt, rm_txt)
    check("★ 成员通知也带群名", "读书分享会" in add_txt and "读书分享会" in rm_txt)
    # 诚实说明：事件体里**没有**验证消息，所以第一条只给昵称+来源
    check("（诚实）加群申请事件体不含验证消息 ⇒ 首条通知里没有它",
          "老同学" not in SEEN[B.EVENT_GROUP_JOIN_REQUEST])

    # ------------------------------------------------------------------ #
    print("\n" + "─" * 74)
    print("② 模型想看详情/做审批 → 调用工具（这是它拿到的返回值）")
    print("─" * 74)
    calls = []

    class FakeHTTP:
        async def request(self, route, **kw):
            calls.append({"m": route.method, "u": route.url, "j": kw.get("json")})
            if "join_request_list" in route.url:
                return {"list": [
                    {"username": "小明", "member_openid": "FE003FAF76C4",
                     "join_request_id": "AURi8Rr6", "apply_source": "self_apply",
                     "apply_at": "2026-10-07T10:00:00+08:00", "risk_tips": "",
                     "verify_info": {"method": "verify_message",
                                     "verify_message": "我是群主的老同学"}},
                    {"username": "某人", "member_openid": "BAD001",
                     "join_request_id": "JR2", "apply_source": "invited",
                     "apply_at": "2026-10-07T10:05:00+08:00",
                     "risk_tips": "warning_tips"}],
                    "next_cursor": ""}
            return {}

    info = AdapterInfo(adapter_id="t", enabled=True, name="qq",
                       platform="QQ Official Bot",
                       config={"app_id": "a", "app_secret": "b",
                               "permission_mode": "deny_list",
                               "group_deny_list": [], "user_deny_list": []})
    if os.environ.get("KIRA_CORE_GEN") == "3":
        from core.adapter.context import AdapterContext
        adapter = QQOfficialAdapter(
            AdapterContext(info=info, event_queue=asyncio.Queue()))
    else:
        adapter = QQOfficialAdapter(info, asyncio.Queue())
    adapter.client = type("C", (), {})()
    adapter.client.api = type("A", (), {})()
    adapter.client.api._http = FakeHTTP()

    class Ctx:
        class M:
            def get_adapter(self, n):
                return adapter
        adapter_mgr = M()

        def __getattr__(self, n):
            return None

    class E:
        class session:
            adapter_name = "qq"
            session_type = "gm"
            session_id = "G1"

        def is_group_message(self):
            return True

    tool = [t for t in AT.build_tools(
        {"admin_tools_enabled": True, "join_approval_enabled": True})
        if t.name == "manage_qq_group_join_request"][0](ctx=Ctx())
    loop = asyncio.new_event_loop()

    listed = loop.run_until_complete(tool.execute(E(), action="list"))
    print("    action=list 的返回值：")
    for line in listed.splitlines():
        print("      " + line)
    check("★ 列表里给出**申请人昵称**", "小明" in listed)
    check("★ 列表里给出**审批所需的 member_id**（模型不用自己猜）",
          "member_id=FE003FAF76C4" in listed)
    check("★ 列表里给出**验证消息**（模型可据此判断放不放行）",
          "我是群主的老同学" in listed)
    check("★ 风险提示被标出来（⚠）", "⚠" in listed)

    # ------------------------------------------------------------------ #
    print("\n" + "─" * 74)
    print("③ 模型做审批 → 实际发出的请求")
    print("─" * 74)
    approved = loop.run_until_complete(tool.execute(
        E(), action="approve", member_id="FE003FAF76C4", join_request_id="AURi8Rr6"))
    print(f"    approve → 模型看到：{approved!r}")
    print(f"    实际发出：{calls[-1]['m']} {calls[-1]['u']}")
    print(f"             body={calls[-1]['j']}")
    check("★ approve 打到官方审批端点",
          "/approval_join_request/FE003FAF76C4" in calls[-1]["u"])
    check("★ 带上 op=approve 与 join_request_id",
          calls[-1]["j"].get("op") == "approve"
          and calls[-1]["j"].get("join_request_id") == "AURi8Rr6")

    calls.clear()
    declined = loop.run_until_complete(tool.execute(
        E(), action="decline", member_id="BAD001",
        reason="来路不明", also_blacklist=True))
    print(f"\n    decline → 模型看到：{declined!r}")
    print(f"    实际发出：{calls[-1]['j']}")
    check("★ decline 可带拒绝理由", calls[-1]["j"].get("reject_reason") == "来路不明")
    check("★ decline 可同时拉黑",
          calls[-1]["j"].get("add_to_member_blacklist") is True)

    # ------------------------------------------------------------------ #
    print("\n" + "─" * 74)
    print("④ 权限门槛（这是 v1.3.6 更正的点）")
    print("─" * 74)
    print("    · 收到「有人申请加群」这个**事件** → 需要机器人是群管理员")
    print("      （官方原文：只有当机器人是群管理员时才可以收到此事件）")
    print("    · 但**审批本身**只需群管理员、不属内邀 ⇒ 一般打开就能用")
    print("    · 机器人不是管理员时：事件不会来，工具调用会被平台拒绝")
    print("      （40062003 → 我们会翻译成「需要机器人是群管理员」的人话）")

    print()
    print("=" * 74)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
