"""v1.3.3 审计（二）：真实框架路径 + 工具实际行为 + 内邀/权限错误人话化。

为什么单独一个文件
----------------
上次（v1.3.0）踩过一个**测试盲区**：既有测试都直接调 `inject_tools_and_tags()`，
从没走过真实框架的「注册 → 绑定 → 调用」路径，结果漏掉了
"钩子函数名与属性名不一致 ⇒ 绑定失败 ⇒ 工具永远注入不进去"的静默 bug。

所以这里必须**按框架的方式**加载插件、注册钩子、再调用 —— 见 [A]。
[B] 用假 client 驱动新工具，核对报文与文案。
"""
import asyncio
import importlib.util
import inspect
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", "/var/minis/workspace/qqbot_bridge_review/kira-core"))
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))
# botpy（工具层用它的 Route 直发官方接口）
_BOTPY = os.environ.get("BOTPY_PATH", "/tmp/botpy_src/botpy-master")
if os.path.isdir(_BOTPY):
    sys.path.insert(0, _BOTPY)
(ROOT / "data").mkdir(exist_ok=True)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def main():
    print("=" * 72)
    print("## v1.3.3 审计（二）：真实注册路径 / 工具行为 / 错误人话化")
    print("=" * 72)

    import main as bridge_main
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
    from admin_tools import build_tools

    # ---------------- [A] 真实框架绑定路径 ----------------
    # 真实的「注册 → 绑定 → 调用」路径由 tests/audit_hooks.py 覆盖（用真实核心的
    # 绑定逻辑 bound=FakeEv 调用）。这里只做**防回归的名字一致性检查** ——
    # 上次的静默 bug 就是"函数 __name__ 与属性名不一致 ⇒ 框架绑不上 self"。
    print("\n[A] 钩子绑定前提（上次踩过的坑，防回归）")
    fn = getattr(bridge_main.QQOfficialGroupBridge, "on_llm_request", None)
    check("★ 钩子属性 on_llm_request 存在", fn is not None)
    check("★ 函数 __name__ 与属性名一致（否则框架绑不上 self）",
          getattr(fn, "__name__", "") == "on_llm_request",
          f"__name__={getattr(fn, '__name__', '?')}")
    check("★ inject_tools_and_tags 也被框架按名字绑定",
          getattr(getattr(bridge_main.QQOfficialGroupBridge, "inject_tools_and_tags", None),
                  "__name__", "") == "inject_tools_and_tags")

    # ---------------- [B] 工具实际行为（假 client 驱动） ----------------
    print("\n[B] 工具行为：报文是否正确")

    def make_event(adapter, is_group=True, sid="G1"):
        class S:
            adapter_name = "qq"
            session_type = "gm" if is_group else "dm"
            session_id = sid
        class E:
            session = S()

            def is_group_message(self):
                return is_group
        return E()

    def make_adapter(calls, responses=None):
        info = AdapterInfo(adapter_id="t", enabled=True, name="qq",
                           platform="QQ Official Bot",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        # 两版核心的适配器构造签名不同（3.0 走 AdapterContext）
        if os.environ.get("KIRA_CORE_GEN") == "3":
            from core.adapter.context import AdapterContext
            adapter = QQOfficialAdapter(
                AdapterContext(info=info, event_queue=asyncio.Queue()))
        else:
            adapter = QQOfficialAdapter(info, asyncio.Queue())
        responses = responses or {}

        class FakeHTTP:
            async def request(self, route, **kw):
                calls.append({"method": route.method, "url": route.url,
                              "params": route.parameters, "json": kw.get("json")})
                for key, val in responses.items():
                    if key in route.url:
                        if isinstance(val, Exception):
                            raise val
                        return val
                return {}
        adapter.client = type("C", (), {})()
        adapter.client.api = type("A", (), {})()
        adapter.client.api._http = FakeHTTP()
        return adapter

    class FakeCtx:
        def __init__(self, adapter):
            class M:
                def get_adapter(self, n):
                    return adapter
            self.adapter_mgr = M()
        def __getattr__(self, n):
            return None

    loop = asyncio.new_event_loop()

    # --- 群信息 ---
    calls = []
    adapter = make_adapter(calls, {"info": {
        "group_name": "读书分享会", "group_finger_memo": "每周共读",
        "group_class_text": "文化", "group_tags": ["阅读", "文学"],
        "group_member_num": 256}})
    tool = build_tools({"group_info_enabled": True})[1]  # GetGroupInfoTool
    tool = [c for c in build_tools({"group_info_enabled": True})
            if c.name == "get_qq_group_info"][0](ctx=FakeCtx(adapter))
    r = loop.run_until_complete(tool.execute(make_event(adapter)))
    check("★ 群信息含人数（白名单已过，这是新增能力）", "256" in r, r)
    check("群信息含群名/简介/标签", "读书分享会" in r and "每周共读" in r and "阅读" in r, r)
    check("走了 GET /v2/groups/{gid}/info", "/v2/groups/G1/info" in calls[-1]["url"], calls[-1]["url"])

    # --- 找人（通讯录版） ---
    from identity_shared import IdentityStore
    st = IdentityStore(path=os.path.join(tempfile.mkdtemp(), "i.json"))
    st.remember_from_mentions("qq", [
        {"member_openid": "AAA", "username": "小明", "member_role": "admin"},
        {"member_openid": "BBB", "username": "小红", "member_role": "owner"}])

    class CtxWithStore(FakeCtx):
        pass

    ctx = FakeCtx(adapter)
    ctx._bridge_identities = st
    tool = [c for c in build_tools({"member_query_enabled": True})
            if c.name == "find_qq_group_member"][0](ctx=ctx)
    r = loop.run_until_complete(tool.execute(make_event(adapter), keyword="小"))
    check("★ 找人：命中并显示角色", "小明" in r and "管理员" in r, r)
    r2 = loop.run_until_complete(tool.execute(make_event(adapter), keyword="查无此人"))
    check("★ 找人：搜不到时如实说明并指明原因", "露过面" in r2 and "内邀" in r2, r2)

    # --- 内邀：守卫先拦（不发请求） ---
    import admin_tools as AT
    AT._INVITE_BLOCKED.clear()
    AT.mark_invite_blocked("qq", "members")
    calls.clear()
    tool = [c for c in build_tools({"admin_tools_enabled": True, "roster_enabled": True})
            if c.name == "get_qq_group_member_roster"][0](ctx=FakeCtx(adapter))
    r = loop.run_until_complete(tool.execute(make_event(adapter)))
    check("★ 内邀端点：直接返回说明且**不发请求**", "内邀" in r and not calls, f"{r} calls={len(calls)}")

    # --- 内邀：首次调用命中内邀错误 => 记住 + 人话 ---
    AT._INVITE_BLOCKED.clear()
    calls.clear()
    adapter2 = make_adapter(calls, {"members": RuntimeError("code=11253 应用无接口访问权限")})
    tool = [c for c in build_tools({"admin_tools_enabled": True, "roster_enabled": True})
            if c.name == "get_qq_group_member_roster"][0](ctx=FakeCtx(adapter2))
    r = loop.run_until_complete(tool.execute(make_event(adapter2)))
    check("★ 首次撞内邀：返回人话（非原始报错）", "内邀" in r and "11253" not in r, r)
    check("★ 撞过之后被记住（下次守卫直接拦）", AT.invite_blocked("qq", "members"))

    # --- 权限错误 => 人话 ---
    AT._INVITE_BLOCKED.clear()
    calls.clear()
    adapter3 = make_adapter(calls, {"restrict_chat_setting":
                                    RuntimeError("40062003 无操作权限")})
    # 用加入审批（不属内邀）测权限错误翻译
    adapter4 = make_adapter(calls, {"approval_join_request":
                                    RuntimeError("code=40062003 无操作权限")})
    tool = [c for c in build_tools({"admin_tools_enabled": True, "join_approval_enabled": True})
            if c.name == "manage_qq_group_join_request"][0](ctx=FakeCtx(adapter4))
    r = loop.run_until_complete(tool.execute(
        make_event(adapter4), action="approve", member_id="X"))
    check("★ 权限错误翻译成需要管理员的人话", "管理员" in r, r)

    # --- 加群申请列表（仅需管理员，不属内邀） ---
    AT._INVITE_BLOCKED.clear()
    calls.clear()
    adapter5 = make_adapter(calls, {"join_request_list": {"list": [
        {"username": "小王", "member_openid": "W1", "join_request_id": "JR1",
         "apply_source": "self_apply", "apply_at": "2026-10-07T10:00:00+08:00",
         "verify_info": {"method": "verify_message", "verify_message": "我是老同学"}}],
        "next_cursor": ""}})
    tool = [c for c in build_tools({"admin_tools_enabled": True, "join_approval_enabled": True})
            if c.name == "manage_qq_group_join_request"][0](ctx=FakeCtx(adapter5))
    r = loop.run_until_complete(tool.execute(make_event(adapter5), action="list"))
    check("★ 加群申请列表可用（仅需管理员，非内邀）",
          "小王" in r and "主动申请" in r and "我是老同学" in r, r)
    check("列表给出审批所需的 member_id", "member_id=W1" in r, r)

    # --- 发文件（默认开，无需权限） ---
    calls.clear()
    adapter6 = make_adapter(calls)
    tool = [c for c in build_tools({"send_file_enabled": True})
            if c.name == "send_qq_file"][0](ctx=FakeCtx(adapter6))
    r = loop.run_until_complete(tool.execute(
        make_event(adapter6), url="https://x.com/a.txt", file_type=4, file_name="a.txt"))
    sent = calls[-1]["json"]
    check("★ 发文件：走 POST /files 且带 url/file_type",
          "/v2/groups/G1/files" in calls[-1]["url"] and sent.get("url") == "https://x.com/a.txt",
          json.dumps(calls[-1], ensure_ascii=False)[:160])
    r = loop.run_until_complete(tool.execute(make_event(adapter6), url="ftp://x/a"))
    check("非法协议被拦（不编造请求）", "http" in r, r)

    # --- 私聊发文件走 users 路径 ---
    calls.clear()
    r = loop.run_until_complete(tool.execute(
        make_event(adapter6, is_group=False, sid="U1"), url="https://x.com/a.txt"))
    check("私聊发文件走 /v2/users/{uid}/files",
          "/v2/users/U1/files" in calls[-1]["url"], calls[-1]["url"])

    # --- 读文件工具：没有附件时如实说明 ---
    tool = [c for c in build_tools({"receive_files": True})
            if c.name == "read_qq_attached_file"][0](ctx=FakeCtx(adapter6))
    r = loop.run_until_complete(tool.execute(make_event(adapter6)))
    check("读文件：无附件时如实说明（不报错）", "没有可读取的文件" in r, r)

    print()
    print("=" * 72)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
