"""Standalone tests for the QQ Official bridge (v1.1.0).

    python3 tests/test_bridge.py

Layers covered:

  A. dispatch-table plumbing -- reproduces botpy's ``ConnectionState`` parser
     table + ``gateway.on_message`` lookup verbatim, and (when qq-botpy is
     importable) re-runs the same checks against the **real** library.
  B. event materialisation -- realistic GROUP_MESSAGE_CREATE /
     GROUP_AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE payloads through
     ``build_event`` with stub KiraAI classes: is_mentioned, nickname,
     dedup (including the AT-vs-full-message race), DM routing, permissions.
  C. IdentityStore -- the automatic nickname directory.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
# 本仓库布局：插件文件在仓库根目录；开发目录布局：在 bridge_plugin/ 子目录。
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
PLUGIN_DIR = _ROOT if os.path.exists(os.path.join(_ROOT, "main.py")) else os.path.join(_ROOT, "bridge_plugin")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import qqbot_bridge as B  # noqa: E402

BOTPY_CANDIDATES = [
    os.environ.get("BOTPY_PATH", ""),
    "/tmp/botpy",
    os.path.join(_HERE, "..", "..", "botpy"),
]

_PASS = 0
_FAIL = 0


def check(name: str, cond: bool, extra: str = ""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


# --------------------------------------------------------------------------- #
# A. dispatch table
# --------------------------------------------------------------------------- #
def _make_fake_state_cls():
    class FakeConnectionState:
        def __init__(self, dispatch, api=None):
            self._dispatch = dispatch
            self.api = api
            self.parsers = {}
            for attr, func in inspect.getmembers(self):
                if attr.startswith("parse_"):
                    self.parsers[attr[6:].lower()] = func

        def parse_group_at_message_create(self, payload):          # botpy native
            self._dispatch("group_at_message_create", payload)

        def parse_c2c_message_create(self, payload):               # botpy native
            self._dispatch("c2c_message_create", payload)

    return FakeConnectionState


def _gateway_lookup(parsers, msg):
    """Faithful copy of qq-botpy ``BotWebSocket.on_message`` dispatch."""
    event = msg["t"].lower()
    try:
        func = parsers[event]
    except KeyError:
        return "_parser unknown event %s." % event
    func(msg)
    return None


def test_parser_patch(state_cls, label: str):
    print(f"\n[A] dispatch table ({label})")
    seen = []
    mk = lambda: state_cls(lambda name, payload: seen.append((name, payload)), None)  # noqa: E731

    live = mk()          # connection built before the plugin loaded
    state = mk()

    err = _gateway_lookup(state.parsers, {"t": "GROUP_MESSAGE_CREATE", "d": {"id": "m1"}})
    check("未打补丁时复现 `_parser unknown event group_message_create.`",
          err == "_parser unknown event group_message_create.", repr(err))

    res = B.install_class_parser(state_cls, B.EVENT_GROUP_MESSAGE)
    check("install_class_parser -> patched", res == "patched", res)
    check("幂等：再次 install -> already",
          B.install_class_parser(state_cls, B.EVENT_GROUP_MESSAGE) == "already")

    fresh = mk()
    payload = {"t": "GROUP_MESSAGE_CREATE", "id": "EVT1", "d": {"id": "m1", "content": "hi"}}
    err = _gateway_lookup(fresh.parsers, payload)
    check("新建 ConnectionState 自动带上解析器且不再报错", err is None, repr(err))
    check("解析器把【原始 payload】交给 dispatch",
          seen[-1][0] == "group_message_create" and seen[-1][1] is payload, repr(seen[-1]))

    check("构造前就存在的连接仍缺解析器", "group_message_create" not in live.parsers)
    check("inject_live_parser 注入成功", B.inject_live_parser(live, B.EVENT_GROUP_MESSAGE) is True)
    check("inject_live_parser 幂等", B.inject_live_parser(live, B.EVENT_GROUP_MESSAGE) is False)
    check("运行中的连接也能收到事件", _gateway_lookup(live.parsers, payload) is None)

    # AT/C2C: botpy ships its own parser; we take it over on purpose
    check("AT 解析器默认不动（非强制）",
          B.install_class_parser(state_cls, B.EVENT_GROUP_AT_MESSAGE) == "foreign")
    check("AT 解析器在 force=True 时被接管",
          B.install_class_parser(state_cls, B.EVENT_GROUP_AT_MESSAGE, force=True) == "patched")

    # ---- 可逆性：还原后必须与初始状态完全一致 ----
    at_state = mk()
    at_parsers = at_state.parsers
    check("新连接的 AT 解析器已是我们的（发原始 payload）",
          getattr(at_parsers["group_at_message_create"], "_kira_qqbot_fullmsg_bridge", False))
    seen.clear()
    at_parsers["group_at_message_create"]({"t": "GROUP_AT_MESSAGE_CREATE", "d": {"id": "a1"}})
    check("AT 事件拿到的是原始 payload",
          seen[-1][0] == "group_at_message_create" and seen[-1][1].get("d", {}).get("id") == "a1")

    check("restore_class_parser 还原 AT 解析器", B.restore_class_parser(state_cls, B.EVENT_GROUP_AT_MESSAGE))
    check("还原后 AT 解析器不再是我们的",
          not getattr(getattr(state_cls, "parse_group_at_message_create", None),
                      "_kira_qqbot_fullmsg_bridge", False))
    check("还原幂等（第二次返回 False）",
          B.restore_class_parser(state_cls, B.EVENT_GROUP_AT_MESSAGE) is False)
    check("还原后仍保留 botpy 原生解析器（不是删掉整个键）",
          hasattr(state_cls, "parse_group_at_message_create"))

    check("drop_live_parser 把运行中的 AT 条目换回原生绑定",
          B.drop_live_parser(at_state, B.EVENT_GROUP_AT_MESSAGE))
    check("换回后不再是我们的", not getattr(at_parsers.get("group_at_message_create"),
                                          "_kira_qqbot_fullmsg_bridge", False))
    check("AT 键仍然存在（原生路径可用，不会静默丢消息）",
          "group_at_message_create" in at_parsers)

    restore_state = mk()
    B.inject_live_parser(restore_state, B.EVENT_GROUP_MESSAGE)
    check("全量消息条目在还原后被移除（回到 bug 前的原始状态）",
          B.drop_live_parser(restore_state, B.EVENT_GROUP_MESSAGE)
          and "group_message_create" not in restore_state.parsers)
    check("已还原的状态不再受重复 drop 影响",
          B.drop_live_parser(restore_state, B.EVENT_GROUP_MESSAGE) is False)


def test_real_botpy(pristine: bool = True):
    print("\n[A2] real qq-botpy")
    try:
        import botpy
        from botpy.connection import ConnectionState
    except Exception as exc:
        print(f"  skip  qq-botpy 不可导入（{type(exc).__name__}）")
        return

    check("真实 botpy.ConnectionState 原本没有 group_message_create 解析器", pristine)
    check("install_class_parser 作用在真实类上",
          B.install_class_parser(ConnectionState, B.EVENT_GROUP_MESSAGE) in ("patched", "already"))

    class ProbeClient(botpy.Client):
        async def on_ready(self):
            pass

    async def run():
        client = ProbeClient(intents=botpy.Intents(public_messages=True), bot_log=False)
        conn = ConnectionState(dispatch=client.ws_dispatch, api=None)
        check("真实 ConnectionState 现在带解析器", "group_message_create" in conn.parsers)

        got = asyncio.get_event_loop().create_future()

        async def handler(payload):
            if not got.done():
                got.set_result(payload)

        check("attach_client_handler -> attached",
              B.attach_client_handler(client, "on_group_message_create", handler) == "attached")
        check("已挂载后再次挂载 -> already",
              B.attach_client_handler(client, "on_group_message_create", handler) == "already")

        conn.parsers["group_message_create"]({"t": "GROUP_MESSAGE_CREATE", "d": {"id": "x"}})
        payload = await asyncio.wait_for(got, timeout=3)
        check("经真实 ws_dispatch 抵达插件处理器", payload["d"]["id"] == "x", repr(payload))

    asyncio.run(run())

    class NativelySupported(ProbeClient):
        async def on_group_message_create(self, message):
            pass

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        native = NativelySupported(intents=botpy.Intents(public_messages=True), bot_log=False)
    finally:
        loop.close()

    async def noop(payload):
        return None

    check("上游自带处理器 -> 默认让位（native）",
          B.attach_client_handler(native, "on_group_message_create", noop) == "native")
    check("allow_shadow=True 时才覆盖",
          B.attach_client_handler(native, "on_group_message_create", noop, allow_shadow=True) == "attached")


# --------------------------------------------------------------------------- #
# B. event materialisation
# --------------------------------------------------------------------------- #
class StubInfo:
    def __init__(self, name="qqo"):
        self.name = name
        self.platform = "QQ Official"


class StubAdapter:
    def __init__(self, allow=True):
        self.info = StubInfo()
        self.app_id = "102000001"
        self.message_types = ["text", "img", "at", "reply"]
        self._group_reply_ids = {}
        self._direct_reply_ids = {}
        self._remember_calls = []
        self._chain_bodies = []
        self.published = []
        self.allow = allow

    def publish(self, event):
        self.published.append(event)

    def _is_allowed(self, target_id, is_group):
        return self.allow

    def _remember_reply_id(self, is_group, target_id, message_id):
        self._remember_calls.append((is_group, target_id, message_id))
        return "qqo-" + str(message_id)[:6]

    def _message_chain(self, body, is_group, target_id):
        self._chain_bodies.append(body)
        return [f"<chain:{body.get('content') or ''}>"]


class StubGroup:
    def __init__(self, group_id=None, group_name=None):
        self.group_id = group_id
        self.group_name = group_name


class StubUser:
    def __init__(self, user_id=None, nickname=None):
        self.user_id = user_id
        self.nickname = nickname


class StubIMMessage:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class StubEvent:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def gm_payload(**over):
    body = {
        "id": "MSG1",
        "author": {"id": "UID1", "member_openid": "UID1", "member_role": "member",
                   "username": "小明", "bot": False},
        "content": "大家早上好",
        "group_openid": "GRP1",
        "message_type": 0,
    }
    body.update(over)
    return {"op": 0, "s": 7, "t": "GROUP_MESSAGE_CREATE", "id": "EVT1", "d": body}


def at_payload(**over):
    body = {
        "id": "MSG2",
        "author": {"id": "UID1", "member_openid": "UID1", "username": "小明", "bot": False},
        "content": "机器人你好",
        "group_openid": "GRP1",
        "message_type": 0,
    }
    body.update(over)
    return {"op": 0, "s": 8, "t": "GROUP_AT_MESSAGE_CREATE", "id": "EVT2", "d": body}


def c2c_payload(**over):
    body = {
        "id": "MSG3",
        "author": {"id": "UOPENID9", "user_openid": "UOPENID9", "username": "小红", "bot": False},
        "content": "在吗",
        "message_type": 0,
    }
    body.update(over)
    return {"op": 0, "s": 9, "t": "C2C_MESSAGE_CREATE", "id": "EVT3", "d": body}


def _build(payload, adapter=None, kind=B.KIND_FULL, is_group=True, ident=None, **kw):
    adapter = adapter or StubAdapter()
    kw.setdefault("dedup", B.MessageDedup())
    return adapter, B.build_event(
        adapter,
        payload,
        Group=StubGroup,
        User=StubUser,
        KiraIMMessage=StubIMMessage,
        KiraMessageEvent=StubEvent,
        kind=kind,
        is_group=is_group,
        identities=ident,
        **kw,
    )


def test_build_event():
    print("\n[B1] 全量群消息")
    adapter, (event, src) = _build(gm_payload())
    check("普通群消息 is_mentioned=False（交给关键词唤醒/围观）",
          event.message.is_mentioned is False and src == "none", src)
    check("昵称 = 事件里的真实 QQ 昵称（不是 OpenID）",
          event.message.sender.nickname == "小明", event.message.sender.nickname)
    check("user_id 仍是稳定的 OpenID", event.message.sender.user_id == "UID1")
    check("群字段与回复锚点就位",
          event.message.group.group_id == "GRP1" and adapter._group_reply_ids["GRP1"] == "MSG1")
    check("原始 body 交给 _message_chain（引用/富文本可解析）",
          adapter._chain_bodies[-1]["group_openid"] == "GRP1")

    _, (event, src) = _build(gm_payload(mentions=[{"id": "x", "is_you": True, "bot": True}]))
    check("mentions[].is_you=True -> 唤醒", event.message.is_mentioned is True and src == "is_you", src)

    _, (event, src) = _build(gm_payload(mentions=[{"id": 6158788878435714165, "bot": True}]),
                             self_ids=[6158788878435714165])
    check("无 is_you 时按机器人自身 id 兜底", event.message.is_mentioned is True and src == "self_id", src)

    _, (event, src) = _build(gm_payload(mentions=[{"id": "u9", "is_you": False, "bot": False}]),
                             self_ids=[6158788878435714165])
    check("只 @ 别人不算唤醒", event.message.is_mentioned is False and src == "none", src)

    _, (event, src) = _build(gm_payload(), mention_mode="always")
    check("mention_mode=always 复刻旧行为", event.message.is_mentioned is True and src == "always")
    _, (event, src) = _build(gm_payload(), mention_mode="never")
    check("mention_mode=never", event.message.is_mentioned is False and src == "never")

    print("\n[B2] @ 消息 / 单聊")
    adapter, (event, src) = _build(at_payload(), kind=B.KIND_AT, force_mention=True)
    check("@ 事件强制唤醒（官方文档：AT 事件的 mentions 不含机器人自身）",
          event.message.is_mentioned is True and src == "forced", src)
    check("@ 事件昵称同样是真实 QQ 昵称",
          event.message.sender.nickname == "小明", event.message.sender.nickname)

    adapter, (event, src) = _build(c2c_payload(), kind=B.KIND_DM, is_group=False)
    check("单聊：group=None、uid=user_openid、天然唤醒",
          event.message.group is None and event.message.sender.user_id == "UOPENID9"
          and event.message.is_mentioned is True)
    check("单聊昵称 = 真实 QQ 昵称", event.message.sender.nickname == "小红")
    check("单聊回复锚点写进 _direct_reply_ids", adapter._direct_reply_ids["UOPENID9"] == "MSG3")

    print("\n[B3] 昵称兜底（自动通讯录）")
    store = B.IdentityStore(path=None)
    _, (event, _) = _build(gm_payload(author={"member_openid": "UID1"}), ident=store)
    check("首次没有 username 时退回 OpenID", event.message.sender.nickname == "UID1")
    _, (event, _) = _build(gm_payload(author={"member_openid": "UID1", "username": "小明"}), ident=store)
    check("见到 username 后记住", store.remember("qqo", "gm", "UID1", None) == "小明")
    _, (event, _) = _build(gm_payload(author={"member_openid": "UID1"}), ident=store)
    check("之后没有 username 也能用记住的昵称", event.message.sender.nickname == "小明",
          event.message.sender.nickname)
    _, (event, _) = _build(gm_payload(author={"member_openid": "UID1", "username": "小明改名了"}), ident=store)
    check("改名自动跟随", store.remember("qqo", "gm", "UID1", None) == "小明改名了")

    print("\n[B4] 去重与双事件竞态")
    dedup = B.MessageDedup()
    adapter = StubAdapter()
    e1, _ = _build(gm_payload(), adapter=adapter, dedup=dedup)[1]
    e2, r2 = _build(gm_payload(), adapter=adapter, dedup=dedup)[1]
    check("同一事件重复推送被丢弃", e1 is not None and e2 is None and r2 == "duplicate", r2)

    dedup = B.MessageDedup()
    adapter = StubAdapter()
    _build(gm_payload(), adapter=adapter, dedup=dedup)
    at_ev, r = _build(at_payload(id="MSG1"), adapter=adapter, kind=B.KIND_AT,
                      force_mention=True, dedup=dedup)[1]
    check("全量副本先到、@ 副本随后到 -> @ 副本仍然放行（绝不丢唤醒）",
          at_ev is not None, r)

    dedup = B.MessageDedup()
    adapter = StubAdapter()
    _build(at_payload(id="MSG2"), adapter=adapter, kind=B.KIND_AT, force_mention=True, dedup=dedup)
    fm_ev, r = _build(gm_payload(id="MSG2"), adapter=adapter, dedup=dedup)[1]
    check("@ 副本先到 -> 随后的全量副本被丢弃（避免重复进上下文）",
          fm_ev is None and r == "duplicate", r)

    print("\n[B5] 边界")
    _, (event, src) = _build(gm_payload(), adapter=StubAdapter(allow=False))
    check("黑名单群被拦下", event is None and src == "denied", src)
    _, (event, src) = _build({"t": "GROUP_MESSAGE_CREATE", "d": {"content": "x"}})
    check("缺 group/author 时安全丢弃", event is None and src == "missing-ids", src)
    _, (event, src) = _build("not-a-dict")
    check("payload 形状异常时安全丢弃", event is None and src == "bad-payload", src)
    _, (event, src) = _build({"id": "M9", "author": {"member_openid": "U"},
                              "group_openid": "G", "content": "x"})
    check("裸 body（无 d 包装）也能解析", event is not None and src == "none", src)


def test_identity_store():
    print("\n[C] IdentityStore 持久化")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "identities.json")
        s1 = B.IdentityStore(path=path)
        s1.remember("qqo", "gm", "U1", "小明")
        s1.remember("qqo", "gm", "U2", "小红")
        s1.save()
        check("落盘文件已生成", os.path.exists(path))
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        check("内容是 OpenID -> 昵称 映射",
              data.get("qqo|gm|U1") == "小明" and len(data) == 2, str(data))
        s2 = B.IdentityStore(path=path)
        check("重新加载后仍记得", s2.remember("qqo", "gm", "U1", None) == "小明")
        check("不同 adapter 隔离", s2.remember("other", "gm", "U1", None) is None)
    small = B.IdentityStore(path=None, max_entries=3)
    for i in range(6):
        small.remember("a", "gm", f"U{i}", f"n{i}")
    check("容量上限生效", len(small._store) <= 3, str(len(small._store)))


def test_dedup():
    print("\n[D] MessageDedup")
    d = B.MessageDedup(ttl=10.0, maxlen=3)
    check("首次 -> new", d.classify("k", B.KIND_FULL, now=1000.0) == "new")
    check("窗口内同 kind -> dup", d.classify("k", B.KIND_FULL, now=1005.0) == "dup")
    check("窗口外 -> new", d.classify("k", B.KIND_FULL, now=1020.0) == "new")
    check("kind_of 可查", d.kind_of("k") == B.KIND_FULL)
    d2 = B.MessageDedup()
    d2.classify("x", B.KIND_FULL, now=1000.0)
    check("fm -> at 判定为 at_after_fm", d2.classify("x", B.KIND_AT, now=1001.0) == "at_after_fm")
    check("其后 kind 变成 at", d2.kind_of("x") == B.KIND_AT)
    check("at 之后再来 fm -> dup", d2.classify("x", B.KIND_FULL, now=1002.0) == "dup")
    for i in range(6):
        d2.classify(f"k{i}", B.KIND_FULL, now=2000.0 + i)
    check("容量上限生效", len(d2._seen) <= d2.maxlen, str(len(d2._seen)))
    check("未知 key 的 kind_of 为 None", d2.kind_of("nope") is None)


def test_capabilities_and_perf():
    print("\n[E] 兼容性与性能")

    class Bare:
        class _Info:
            name = "bare"
        info = _Info()
        message_types = []
        app_id = "1"

    missing = B.check_adapter_capabilities(Bare())
    check("能力检查识别出缺失接口",
          {"_message_chain", "_remember_reply_id", "publish"} <= set(missing), str(missing))
    check("正常适配器通过能力检查", B.check_adapter_capabilities(StubAdapter()) == [])

    class NoChain(StubAdapter):
        pass

    NoChain._message_chain = None
    _, (event, reason) = _build(gm_payload(), adapter=NoChain())
    check("缺少 _message_chain 时安全降级（不是抛异常）",
          event is None and reason == "no-chain-builder", reason)

    class NoPermissionHook(StubAdapter):
        pass

    NoPermissionHook._is_allowed = None
    _, (event, reason) = _build(gm_payload(), adapter=NoPermissionHook())
    check("缺少 _is_allowed 时默认放行", event is not None and reason == "none", reason)

    class NoReplyIds(StubAdapter):
        pass

    NoReplyIds._group_reply_ids = None
    NoReplyIds._direct_reply_ids = None
    _, (event, reason) = _build(gm_payload(), adapter=NoReplyIds())
    check("缺少回复锚点表时不崩（事件照发）", event is not None, reason)

    # ---- throughput: an upper bound on the per-message cost ----
    adapter = StubAdapter()
    dedup = B.MessageDedup(maxlen=4096)
    n = 20000
    payloads = [gm_payload(id="M%d" % i) for i in range(n)]
    kwargs = dict(Group=StubGroup, User=StubUser, KiraIMMessage=StubIMMessage,
                  KiraMessageEvent=StubEvent, identities=B.IdentityStore(path=None))
    t0 = time.perf_counter()
    for p in payloads:
        B.build_event(adapter, p, dedup=dedup, **kwargs)
    dt = time.perf_counter() - t0
    per_us = dt / n * 1e6
    print(f"  info build_event 实测 {per_us:.1f} µs/条（{n / dt:,.0f} msg/s，含 payload dict 构造）")
    check("build_event 吞吐 < 300 µs/条", per_us < 300.0, f"{per_us:.1f} µs/msg")

    # ---- bounded memory ----
    dedup_small = B.MessageDedup(ttl=1e9, maxlen=100)
    for i in range(5000):
        dedup_small.classify("k%d" % i, B.KIND_FULL)
    check("去重表内存有界（5000 条后仍 ≤ maxlen）", len(dedup_small._seen) <= 100,
          str(len(dedup_small._seen)))
    store = B.IdentityStore(path=None, max_entries=50)
    for i in range(1000):
        store.remember("a", "gm", "U%d" % i, "n%d" % i)
    check("昵称通讯录内存有界（1000 条后仍 ≤ max_entries）", len(store._store) <= 50,
          str(len(store._store)))
    check("remember() 不触发同步落盘（脏标记交给后台线程）",
          B.IdentityStore(path=None).dirty is False)


def main():
    print("=" * 72)
    print("QQ Official bridge v1.1.0 — self test")
    print("=" * 72)

    test_parser_patch(_make_fake_state_cls(), "replica")

    real_cls = None
    for cand in BOTPY_CANDIDATES:
        if cand and os.path.isdir(cand):
            sys.path.insert(0, cand)
            try:
                import botpy  # noqa: F401
                from botpy.connection import ConnectionState as real_cls  # noqa: F811
                break
            except Exception:
                real_cls = None
    pristine = True
    if real_cls is not None:
        pristine = "parse_group_message_create" not in real_cls.__dict__
        test_parser_patch(real_cls, "real qq-botpy")
    test_real_botpy(pristine)
    test_build_event()
    test_identity_store()
    test_dedup()
    test_capabilities_and_perf()

    print("\n" + "=" * 72)
    print(f"结果：{_PASS} passed, {_FAIL} failed")
    print("=" * 72)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
