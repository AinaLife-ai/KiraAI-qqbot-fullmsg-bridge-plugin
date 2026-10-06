"""最终验证：内置聊天插件（kira-ai）+ Z 版 + bridge 的共存，以及双核心真实生效。

跑法：
    python3 tests/audit_chat_compat.py --core 2      # 用 kira-core
    python3 tests/audit_chat_compat.py --core 3      # 用 kira-v3
（不带参数则读环境变量 KIRA_CORE）

覆盖：
  A. 内置 kira-ai 聊天插件 + bridge 同时注册 llm_request 钩子（真实调用两者）
  B. 标签共存：内置 text/at/reply/img… 与 bridge 的 markdown/keyboard 同在一个 TagSet
  C. 工具共存：bridge 的 4 个群管理工具进 ToolSet
  D. Z 版（Default Chat Z）的补丁目标与 bridge 不重叠
  E. ★ 真实生效：2.x 走「解析器补丁 + 事件接管」，3.0 走「api 层 + publish 包装」，
     都必须真的把 markdown/keyboard 发出去
"""
import argparse
import asyncio
import importlib.util
import os
import pathlib
import sys

ROOT = pathlib.Path("/var/minis/workspace/qqbot_bridge_review")
BRIDGE = ROOT / "bridge"
(BRIDGE / "data").mkdir(parents=True, exist_ok=True)

ap = argparse.ArgumentParser()
ap.add_argument("--core", default=os.environ.get("KIRA_CORE_GEN", "3"))
args, _ = ap.parse_known_args()
CORE = ROOT / ("kira-core" if args.core == "2" else "kira-v3")
GEN = "2" if args.core == "2" else "3"

sys.path.insert(0, str(CORE))
sys.path.insert(0, str(BRIDGE))
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


def load_bridge():
    name = "plugins.qqbot-fullmsg-bridge.main"
    spec = importlib.util.spec_from_file_location(name, str(BRIDGE / "main.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_adapter():
    from core.adapter.adapter_info import AdapterInfo

    config = {"app_id": "a", "app_secret": "b", "permission_mode": "allow_list",
              "group_allow_list": ["G1"], "user_allow_list": ["U1"]}
    info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                       platform="QQ Official", config=config)
    if GEN == "3":
        from core.adapter.context import AdapterContext
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter

        return QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter

    return QQOfficialAdapter(info, asyncio.Queue())


class FakeAPI:
    def __init__(self):
        self.calls = []

    async def post_group_message(self, **kw):
        self.calls.append(("group", kw))
        return {"id": "OUT1", "ext_info": {"ref_idx": "REFIDX_out=="}}

    async def post_c2c_message(self, **kw):
        self.calls.append(("c2c", kw))
        return {"id": "OUTC", "ext_info": {"ref_idx": "REFIDX_outc=="}}

    async def on_interaction_result(self, iid, code):
        self.calls.append(("ack", {"id": iid, "code": code}))
        return {}


class FakeHTTP:
    async def request(self, route, **kw):
        if "info" in route.path:
            return {"group_openid": "G1", "group_name": "测试群"}
        return {}


class FakeClient:
    def __init__(self):
        self.api = FakeAPI()
        self.api._http = FakeHTTP()
        self._connection = None


class FakeTask:
    def done(self):
        return False


class FakeMgr:
    def __init__(self, a):
        self._a = {"qqo": a}

    def get_adapters(self):
        return self._a

    def get_adapter(self, n):
        return self._a.get(n)


class FakeCtx:
    def __init__(self, a):
        self.adapter_mgr = FakeMgr(a)


class FakeToolSet:
    def __init__(self):
        self.tools = []

    def add(self, *ts):
        for t in ts:
            for i, old in enumerate(self.tools):
                if old.name == t.name:
                    self.tools.pop(i)
                    break
            self.tools.append(t)

    def remove(self, *names):
        self.tools = [t for t in self.tools if t.name not in names]


class FakeReq:
    def __init__(self):
        self.tool_set = FakeToolSet()
        self.system_prompt = []


class FakeEv:
    def __init__(self):
        self.adapter = type("A", (), {"name": "qqo", "platform": "QQ Official"})()
        self.sid = "qqo:gm:G1"


def main():
    print("=" * 74)
    print(f"## 聊天插件共存 + 真实生效验证  [核心 = {CORE.name} (KiraAI {GEN}.x)]")
    print("=" * 74)

    bridge = load_bridge()
    from core.tag import TagSet
    from core.chat import MessageChain
    from core.chat.message_elements import Text

    adapter = make_adapter()
    adapter.client = FakeClient()
    adapter._client_task = FakeTask()
    plugin = bridge.QQOfficialGroupBridge(FakeCtx(adapter), {})

    # ---------------- A. 内置聊天插件 + bridge 的钩子共存 ----------------
    print("\n[A] 内置 kira-ai 聊天插件 + bridge 的 llm_request 钩子共存")
    builtin_mod = None
    try:
        import types as _t

        pkg_dir = CORE / "core/plugin/builtin_plugins/kira-ai"
        pkg_name = "plugins.kira_ai_builtin"
        pkg = _t.ModuleType(pkg_name)
        pkg.__path__ = [str(pkg_dir)]
        pkg.__package__ = pkg_name
        sys.modules[pkg_name] = pkg
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.main", str(pkg_dir / "main.py"))
        builtin_mod = importlib.util.module_from_spec(spec)
        builtin_mod.__package__ = pkg_name
        sys.modules[f"{pkg_name}.main"] = builtin_mod
        spec.loader.exec_module(builtin_mod)
        check("内置聊天插件可加载", True)
    except Exception as exc:
        check("内置聊天插件可加载", False, f"{type(exc).__name__}: {exc}")

    ts = TagSet()
    req = FakeReq()
    ev = FakeEv()
    plugin.inject_tools_and_tags(ev, req, ts)

    builtin_tags = []
    if builtin_mod is not None:
        cls = None
        for v in vars(builtin_mod).values():
            if isinstance(v, type) and v.__name__ in ("DefaultPlugin", "KiraAIPlugin"):
                cls = v
        if cls is not None:
            try:
                # 内置插件的钩子签名 (event, _, tag_set)，需要 event.message_types
                # 与 ctx.get_session_capabilities(sid)
                class _Ev2(FakeEv):
                    supported_elements = ["text", "at", "reply", "img", "emoji"]
                    message_types = supported_elements

                class _Ctx2(FakeCtx):
                    def get_session_capabilities(self, sid):
                        return {}

                binst = cls(ctx=_Ctx2(adapter), cfg={})
                res = binst.inject_builtin_tags(_Ev2(), object(), ts)
                if asyncio.iscoroutine(res):
                    asyncio.new_event_loop().run_until_complete(res)
                builtin_tags = sorted(t.name for t in ts.get_all())
                check("内置聊天插件的标签也进了同一个 TagSet", len(builtin_tags) > 0,
                      str(builtin_tags))
            except Exception as exc:
                check("内置聊天插件的标签也进了同一个 TagSet", False,
                      f"{type(exc).__name__}: {exc}")

    all_tags = sorted(t.name for t in ts.get_all())
    print(f"    合并后的标签集: {all_tags}")
    check("★ bridge 的 markdown/keyboard 与内置标签共存",
          {"markdown", "keyboard"} <= set(all_tags), str(all_tags))
    if builtin_tags:
        check("★ 内置标签未被 bridge 挤掉",
              {"text", "at", "reply"} <= set(all_tags), str(all_tags))

    prompt = ts.to_prompt()
    check("提示词里同时含内置标签与 bridge 标签",
          "text" in prompt and "markdown" in prompt)

    # ---------------- B. 工具共存 ----------------
    print("\n[B] 工具共存")
    tools = sorted(t.name for t in req.tool_set.tools)
    check("bridge 的无需权限工具已注入（默认配置）",
          {"recall_qq_msg", "get_qq_group_info", "find_qq_group_member",
           "read_qq_attached_file",
           "get_qq_bot_state"} <= set(tools), str(tools))
    check("★ 需管理员权限的工具默认不注入（v1.3.3 分组）",
          not ({"set_qq_group_ban", "get_group_mute_state",
                "manage_qq_group_join_request", "kick_qq_group_member",
                "get_qq_group_member_roster",
                "manage_qq_group_blacklist"} & set(tools)), str(tools))

    # ---------------- C. Z 版补丁目标 ----------------
    print("\n[C] Z 版（Default Chat Z）补丁目标与 bridge 无重叠")
    z = ROOT / "compat_zchat" / "main.py"
    check("Z 版存在", z.exists())
    if z.exists():
        zsrc = z.read_text(encoding="utf-8")
        med = (ROOT / "compat_zchat" / "media_recognize.py").read_text(encoding="utf-8")
        check("Z 版不 patch adapter.send_group_message / client.api",
              "send_group_message" not in zsrc and "post_group_message" not in zsrc)
        check("Z 版只 patch 框架 desc_img（与 bridge 层不重叠）",
              'setattr(mod, "desc_img"' in med)
        check("Z 版不碰 tag_set（不注册/清理标签）",
              "tag_set.register" not in zsrc)
        check("Z 版只移除它自己的 manage_ignore（不动别家工具）",
              '_filter_tools(req.tool_set, ["manage_ignore"], "exact")' in zsrc)

    # ---------------- D. ★ 真实生效 ----------------
    print("\n[D] ★ 真实生效（这条链路必须真的把消息发出去）")
    asyncio.new_event_loop().run_until_complete(plugin._tick(report=False))
    prof = plugin.profiles.get("qqo")
    check(f"世代探测正确（{GEN}.x）", prof is not None and (
        (GEN == "2" and prof.is_v2) or (GEN == "3" and prof.is_v3)), repr(prof))

    # 发一条「markdown + keyboard」
    chain = MessageChain([
        bridge.MarkdownText("## 标题\n- 项一\n- 项二"),
        bridge.KeyboardMarker({"content": {"rows": [{"buttons": [
            {"id": "b1", "render_data": {"label": "签到", "style": 1},
             "action": {"type": 2, "data": "/签到", "permission": {"type": 2}}}]}]}}),
    ])
    res = asyncio.new_event_loop().run_until_complete(
        adapter.send_group_message("G1", chain))
    _, payload = adapter.client.api.calls[-1]
    check("★ markdown 真的走出去了（msg_type=2 + markdown.content）",
          payload.get("msg_type") == 2 and "标题" in str(
              (payload.get("markdown") or {}).get("content", "")),
          str(payload)[:160])
    check("★ keyboard 真的随报文发出", bool(payload.get("keyboard")), str(payload)[:120])
    check("★ content 被清空（官方要求互斥）", payload.get("content") is None)

    # 引用注入：必须由**链里的 Reply 元素**触发
    # （发送入口会按链重设 contextvar —— 这是刻意的：防止手动设的 ref 泄漏到别的消息）
    from core.chat.message_elements import Reply
    ref_store_for = bridge.ref_store_for

    sid = "qqo:gm:G1"
    ref_store_for(adapter)[(sid, "qqo-display1")] = "REFIDX_quoted=="
    asyncio.new_event_loop().run_until_complete(
        adapter.send_group_message("G1", MessageChain([Reply("qqo-display1"), Text("引用测试")])))
    _, p2 = adapter.client.api.calls[-1]
    check("★ 引用真的注入（链里有 Reply 时带上 message_reference）",
          (p2.get("message_reference") or {}).get("message_id") == "REFIDX_quoted==",
          repr(p2.get("message_reference")))

    # 反向：没有 Reply 元素时**不得**注入（否则每条消息都会变成引用上一条）
    asyncio.new_event_loop().run_until_complete(
        adapter.send_group_message("G1", MessageChain([Text("普通回复，不该带引用")])))
    _, p3 = adapter.client.api.calls[-1]
    check("★ 反向验证：无 Reply 时不注入引用（避免刷屏）",
          not p3.get("message_reference"), repr(p3.get("message_reference")))

    # 世代专属能力
    if GEN == "2":
        print("\n[D2] 2.x 专属：解析器补丁 + 事件接管")
        cls = plugin._connection_state_cls()
        parsers_ok = cls is not None and hasattr(cls, "parse_group_message_create")
        check("★ 2.x 全量群消息解析器已补上（修 _parser unknown event）", parsers_ok)
    else:
        print("\n[D3] 3.0 专属：api 层补丁 + publish 包装")
        check("★ api 层补丁已装（3.0 上 _send_message 不存在也能装）",
              getattr(adapter.client.api.post_group_message, "_kira_bridge_send", False))
        plugin.group_names.remember("qqo", "G1", "读书分享会")
        check("★ publish 包装已装（群名会进会话标题）",
              getattr(adapter.publish, "_kira_bridge_publish", False))

    # ---------------- E. 群名 ----------------
    print("\n[E] 群名")
    plugin.group_names.remember("qqo", "G1", "读书分享会")
    if GEN == "3":
        from core.chat.message_utils import KiraIMMessage, KiraMessageEvent
        from core.chat import Group, User

        e = KiraMessageEvent(
            adapter=adapter.info,
            message_types=["text"],
            message=KiraIMMessage(timestamp=1, sender=User(user_id="U1", nickname="x"),
                                  group=Group(group_id="G1", group_name="G1"),
                                  message_id="m1", self_id="s",
                                  chain=MessageChain([Text("hi")])),
            timestamp=1)
        adapter.publish(e)
        got = adapter._event_queue.get_nowait()
        check("★ 3.0：群名在 publish 时被换成中文（含会话标题）",
              got.message.group.group_name == "读书分享会"
              and got.session.session_title == "读书分享会",
              f"{got.message.group.group_name} / {got.session.session_title}")

    print()
    print("=" * 74)
    print(f"结果（KiraAI {GEN}.x）：{PASS} passed, {FAIL} failed")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
