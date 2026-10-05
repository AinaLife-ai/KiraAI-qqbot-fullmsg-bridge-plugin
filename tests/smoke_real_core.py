"""End-to-end smoke test against the REAL KiraAI core + REAL qq-botpy (v1.1.0).

    KIRA_CORE=/path/to/kira_fw BOTPY_PATH=/path/to/botpy python3 tests/smoke_real_core.py

Wires the bridge to a genuine ``QQOfficialAdapter`` (never ``start()``-ed, so no
network), pushes raw gateway payloads through botpy's real ``ConnectionState``
parser table -> real ``Client.ws_dispatch``, and asserts real
``KiraMessageEvent`` objects land on the adapter's event bus -- including the
nickname the LLM will actually see.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import time
import traceback

KIRA_CORE = os.environ.get("KIRA_CORE", "/var/minis/shared/kira_fw")
BOTPY_PATH = os.environ.get("BOTPY_PATH", "/tmp/botpy")
_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PLUGIN_DIR = _ROOT if os.path.exists(os.path.join(_ROOT, "main.py")) else os.path.join(_ROOT, "bridge_plugin")

for p in (PLUGIN_DIR, KIRA_CORE, BOTPY_PATH):
    if p and os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

# KiraAI 的日志管理器会在 cwd 下建 data/log.log，先把目录准备好
try:
    os.makedirs(os.path.join(os.getcwd(), "data"), exist_ok=True)
except Exception:
    pass

_PASS = 0
_FAIL = 0
GRACE = 1.0


def check(name, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {name}")
    else:
        _FAIL += 1
        print(f"  FAIL {name} {extra}")


def gm(msg_id="MSG1", content="大家早上好", mentions=None, uid="UID1", username="小明",
       attachments=None, **extra):
    return {"op": 0, "s": 7, "t": "GROUP_MESSAGE_CREATE", "id": "EVT1", "d": {
        "id": msg_id,
        "author": {"id": uid, "member_openid": uid, "member_role": "member",
                   "username": username, "bot": False},
        "content": content, "group_openid": "GRP_OPENID_1", "message_type": 0,
        **({"mentions": mentions} if mentions is not None else {}),
        **({"attachments": attachments} if attachments is not None else {}),
        **extra,
    }}


def atm(msg_id="MSG2", content="机器人 你好", uid="UID1", username="小明"):
    return {"op": 0, "s": 8, "t": "GROUP_AT_MESSAGE_CREATE", "id": "EVT2", "d": {
        "id": msg_id,
        "author": {"id": uid, "member_openid": uid, "username": username, "bot": False},
        "content": content, "group_openid": "GRP_OPENID_1", "message_type": 0,
        "mentions": [],  # 官方文档：AT 事件的 mentions 不含机器人自身
    }}


def c2c(msg_id="MSG3", content="在吗", uid="UOPENID9", username="小红"):
    return {"op": 0, "s": 9, "t": "C2C_MESSAGE_CREATE", "id": "EVT3", "d": {
        "id": msg_id,
        "author": {"id": uid, "user_openid": uid, "username": username, "bot": False},
        "content": content, "message_type": 0,
    }}


async def main():
    print("=" * 72)
    try:
        _v = json.loads((pathlib.Path(PLUGIN_DIR) / "manifest.json").read_text(encoding="utf-8"))["version"]
    except Exception:
        _v = "?"
    print(f"QQ Official bridge v{_v} — smoke test (real KiraAI core + real botpy)")
    print("=" * 72)

    import importlib.util

    try:
        import botpy  # noqa: F401
        from botpy.connection import ConnectionSession

        from core.adapter.adapter_info import AdapterInfo
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
    except Exception as exc:
        print(f"  skip  需要真实 KiraAI core 与 qq-botpy（{type(exc).__name__}: {exc}）")
        print("  hint  KIRA_CORE=/path/to/kira_fw BOTPY_PATH=/path/to/botpy python3 tests/smoke_real_core.py")
        return 0

    spec = importlib.util.spec_from_file_location(
        "qqbot_bridge_plugin_main", os.path.join(PLUGIN_DIR, "main.py")
    )
    plugin_main = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin_main
    spec.loader.exec_module(plugin_main)

    info = AdapterInfo(
        enabled=True, adapter_id="a1", name="qqo", platform="QQ Official",
        config={"app_id": "102000001", "app_secret": "secret", "permission_mode": "deny_list"},
    )
    bus = asyncio.Queue()
    adapter = QQOfficialAdapter(info, bus)
    adapter.app_id = "102000001"

    from core.adapter.src.qq_official.qq_official import _QQOfficialClient

    client = _QQOfficialClient(adapter)
    client._connection = ConnectionSession(
        max_async=1, connect=lambda session: None, dispatch=client.ws_dispatch, api=None,
    )
    adapter.client = client

    class _FakeRobot:      # 模拟 READY 之后 botpy 挂上的 client.robot
        id = 6158788878435714165
        name = "香里"

    client._connection.state.robot = _FakeRobot()
    parsers = client._connection.parser

    check("真实 botpy 解析表里没有 group_message_create（报错来源）",
          "group_message_create" not in parsers)

    class FakeAdapterMgr:
        def get_adapters(self):
            return {"qqo": adapter}

    class FakeCtx:
        adapter_mgr = FakeAdapterMgr()

    cfg = {
        "section_basic": {
            "enabled": True, "mention_mode": "auto", "dedup_ttl": 180,
            "unify_at_messages": True, "unify_direct_messages": True,
            "at_grace_seconds": GRACE, "remember_nicknames": False,
        },
        "section_proactive": {"proactive_enabled": False},
    }
    plugin = plugin_main.QQOfficialGroupBridge(FakeCtx(), cfg)
    await plugin.initialize()
    await asyncio.sleep(0.05)

    check("运行中连接已补上 group_message_create 解析器", "group_message_create" in parsers)
    check("@ 事件解析器已被接管（发原始 payload）",
          getattr(parsers.get("group_at_message_create"), "_kira_qqbot_fullmsg_bridge", False))
    check("单聊解析器已被接管",
          getattr(parsers.get("c2c_message_create"), "_kira_qqbot_fullmsg_bridge", False))
    check("客户端已挂载全部处理器",
          all(callable(getattr(client, a, None)) for a in
              ("on_group_message_create", "on_group_at_message_create", "on_c2c_message_create")))

    async def drain(timeout=0.3):
        await asyncio.sleep(timeout)
        out = []
        while not bus.empty():
            out.append(bus.get_nowait())
        return out

    # ---- 1. plain (non-@) group message: held for the grace window, then delivered
    parsers["group_message_create"](gm())
    check("全量消息在等待窗口内不立即投递", bus.empty())
    evs = await drain(GRACE + 0.3)
    check("等待窗口过后正常投递", len(evs) == 1, str(len(evs)))
    if evs:
        m = evs[0].message
        check("is_mentioned=False（可走关键词唤醒/围观）", m.is_mentioned is False)
        check("昵称 = 真实 QQ 昵称（不是 32 位 OpenID）", m.sender.nickname == "小明", m.sender.nickname)
        check("OpenID 仍作为稳定 user_id 保留", m.sender.user_id == "UID1")
        check("会话 sid 形如 qqo:gm:<群>", str(evs[0].session.sid).startswith("qqo:gm:"))
        check("消息链由真实 _message_chain 从 dict 构造",
              any(getattr(e, "text", "") == "大家早上好" for e in m.chain), repr(m.chain))
        check("回复锚点已回填", adapter._group_reply_ids.get("GRP_OPENID_1") == "MSG1")
        check("链类型是 MessageChain（插件直接访问 .message_list）",
              hasattr(m.chain, "message_list"), type(m.chain).__name__)

        # 默认聊天插件（builtin_plugins/chat）的契约：它只读这几个字段
        check("默认聊天插件契约：str(session) == sid（它用 str(event.session) 取缓冲）",
              str(evs[0].session) == str(evs[0].session.sid))
        check("默认聊天插件契约：非唤醒消息 is_mentioned=False（会走它的 buffer/discard 分支）",
              m.is_mentioned is False)
        check("默认聊天插件契约：chain 里有 Text（它的唤醒词匹配依据）",
              any(isinstance(getattr(e, "text", None), str) for e in m.chain))

    # ---- 2. same msg_id re-pushed -> dropped
    parsers["group_message_create"](gm())
    check("同一 msg_id 重推被去重", len(await drain(GRACE + 0.3)) == 0)

    # ---- 3. the race: full copy first, @ copy right after -> only the @ copy survives
    parsers["group_message_create"](gm(msg_id="RACE1", content="有人吗"))
    await asyncio.sleep(0.2)
    parsers["group_at_message_create"](atm(msg_id="RACE1", content="有人吗"))
    evs = await drain(GRACE + 0.6)
    check("全量+@ 双副本只投递一条", len(evs) == 1, str(len(evs)))
    if evs:
        check("存活的是 @ 副本且 is_mentioned=True", evs[0].message.is_mentioned is True)
        check("@ 副本昵称同样是真实 QQ 昵称", evs[0].message.sender.nickname == "小明")

    # ---- 4. @ message alone
    parsers["group_at_message_create"](atm(msg_id="ATONLY"))
    evs = await drain(0.4)
    check("单独的 @ 事件正常投递", len(evs) == 1)
    if evs:
        check("@ 事件强制唤醒（官方文档：mentions 不含机器人自身）",
              evs[0].message.is_mentioned is True)

    # ---- 5. single chat (C2C)
    parsers["c2c_message_create"](c2c())
    evs = await drain(0.4)
    check("单聊事件正常投递", len(evs) == 1)
    if evs:
        m = evs[0].message
        check("单聊 group=None、uid=user_openid、天然唤醒",
              m.group is None and m.sender.user_id == "UOPENID9" and m.is_mentioned is True)
        check("单聊昵称 = 真实 QQ 昵称（原生实现这里是 OpenID）",
              m.sender.nickname == "小红", m.sender.nickname)

    # ---- 6. full-mode @ detection via mentions[].is_you
    parsers["group_message_create"](gm(
        msg_id="YOU1", content="机器人 你也在啊",
        mentions=[{"id": "6158788878435714165", "is_you": True, "bot": True, "username": "Bot"}],
    ))
    evs = await drain(GRACE + 0.4)
    check("mentions[].is_you=True 被判为唤醒",
          len(evs) == 1 and evs[0].message.is_mentioned is True)

    # ---- 7. 事件循环存活性：高密度投递时 ticker 仍要正常跳动 ----
    ticks = {"n": 0}

    async def ticker():
        while True:
            await asyncio.sleep(0.01)
            ticks["n"] += 1

    tick_task = asyncio.create_task(ticker())
    for i in range(60):
        parsers["group_message_create"](gm(msg_id="LOAD%d" % i, content="压测 %d" % i))
    for i in range(20):
        parsers["group_at_message_create"](atm(msg_id="LOADAT%d" % i))
    for i in range(20):
        parsers["c2c_message_create"](c2c(msg_id="LOADDM%d" % i))
    await asyncio.sleep(1.0)
    tick_task.cancel()
    check("高密度投递时 10ms ticker 仍正常跳动（事件循环未阻塞）",
          ticks["n"] >= 80, "10s内跳动 %d 次" % ticks["n"])
    delivered = await drain(GRACE + 0.8)
    check("压测后事件仍被全部投递（去重后 100 条）", len(delivered) == 100, str(len(delivered)))

    await plugin.terminate()
    check("terminate() 正常收尾", True)

    # ---- 8. 关闭插件 -> 补丁完整还原，原生路径不能坏 ----
    cfg_off = dict(cfg)
    cfg_off["section_basic"] = dict(cfg["section_basic"], enabled=False)
    plugin_off = plugin_main.QQOfficialGroupBridge(FakeCtx(), cfg_off)
    await plugin_off.initialize()
    await asyncio.sleep(0.05)

    check("关闭后 group_message_create 解析器被移除（回到未打补丁状态）",
          "group_message_create" not in parsers)
    check("关闭后 AT 解析器还原为 botpy 原生（不是删键）",
          "group_at_message_create" in parsers
          and not getattr(parsers["group_at_message_create"], "_kira_qqbot_fullmsg_bridge", False))
    check("关闭后实例处理器已卸载",
          all(a not in vars(client) for a in
              ("on_group_message_create", "on_group_at_message_create", "on_c2c_message_create")))

    while not bus.empty():
        bus.get_nowait()
    parsers["group_at_message_create"](atm(msg_id="NATIVE1"))
    evs = await drain(0.4)
    check("还原后 @ 消息仍走框架原生路径（没有静默丢消息）", len(evs) == 1, str(len(evs)))
    if evs:
        check("原生路径下 is_mentioned=True（框架自身行为）", evs[0].message.is_mentioned is True)

    await plugin_off.terminate()
    check("关闭态实例正常收尾", True)

    # ---- 9. 默认路径（at_grace=0）：跨事件重复绝不丢唤醒，且可观测 ----
    cfg0 = dict(cfg)
    cfg0["section_basic"] = dict(cfg["section_basic"], at_grace_seconds=0)
    # 给 client 挂一个假的 api，用来观察 message_reference 是否被注入
    sent_calls = []

    class _RecorderAPI:
        async def post_group_message(self, **kw):
            sent_calls.append(dict(kw))
            return {"id": "ROBOT1.0_sent", "ext_info": {"ref_idx": "REFIDX_sent=="}}

        async def post_c2c_message(self, **kw):
            sent_calls.append(dict(kw))
            return {"id": "ROBOT1.0_sent2", "ext_info": {"ref_idx": "REFIDX_sent2=="}}

    client.api = _RecorderAPI()

    plugin0 = plugin_main.QQOfficialGroupBridge(FakeCtx(), cfg0)
    await plugin0.initialize()
    await asyncio.sleep(0.05)

    while not bus.empty():
        bus.get_nowait()

    parsers["group_message_create"](gm(msg_id="PAIR1", content="跨事件重复测试"))
    evs = await drain(0.25)
    check("grace=0 时全量消息立即投递（零等待）", len(evs) == 1, str(len(evs)))

    t0 = time.time()
    parsers["group_at_message_create"](atm(msg_id="PAIR1", content="跨事件重复测试"))
    evs2 = []
    while time.time() - t0 < 1.0 and not evs2:      # 真正测「到达延迟」而不是固定 sleep
        await asyncio.sleep(0.01)
        while not bus.empty():
            evs2.append(bus.get_nowait())
    elapsed = time.time() - t0
    check("万一真的出现跨事件重复：@ 副本照常放行（绝不丢唤醒）",
          len(evs2) == 1 and evs2[0].message.is_mentioned is True, str(len(evs2)))
    check("零等待（<0.2s），不需要任何人工延时", elapsed < 0.2, f"{elapsed:.3f}s")
    check("跨事件重复被计数，便于观测（官方文档称不会发生，正常应为 0）",
          plugin0._cross_pairs == 1, str(plugin0._cross_pairs))

    # ---- 10. @ 富文本 → KiraAI 标准 At 元素（保留 pid，防改名冒充） ----
    from core.chat.message_elements import At as RealAt

    def chain_repr(chain):
        return "".join(getattr(e, "text", None) or getattr(e, "repr", None) or str(e) for e in chain)

    while not bus.empty():
        bus.get_nowait()
    parsers["group_message_create"](gm(
        msg_id="ATSELF1", content="<@0A0B9F323E6AA18BF08B6901A3B2DEFC> 妹",
        mentions=[{"id": "0A0B9F323E6AA18BF08B6901A3B2DEFC", "is_you": True,
                   "bot": True, "username": "香里"}],
    ))
    evs = await drain(0.4)
    check("@ 富文本事件正常投递", len(evs) == 1, str(len(evs)))
    if evs:
        chain = evs[0].message.chain
        ats = [e for e in chain if isinstance(e, RealAt)]
        check("拆成 KiraAI 标准 At 元素（★ 保留 pid，不只给昵称）",
              len(ats) == 1 and ats[0].pid == "0A0B9F323E6AA18BF08B6901A3B2DEFC", chain_repr(chain))
        check("At 渲染成 [At 昵称(pid)]（名字给人看、pid 做身份）",
              ats and ats[0].repr == "[At 香里（你）(0A0B9F323E6AA18BF08B6901A3B2DEFC)]",
              ats[0].repr if ats else "")
        check("自己的 At 带「（你）」后缀（同名冒充也分得清）",
              ats and ats[0].nickname and "（你）" in ats[0].nickname)
        check("识别为「叫自己」→ is_mentioned=True", evs[0].message.is_mentioned is True)
        check("其余文本原样保留", chain_repr(chain).endswith(" 妹"), chain_repr(chain))
        check("★ 拆完 @ 后链仍是 MessageChain（S 版会直接访问 chain.message_list）",
              hasattr(chain, "message_list"), type(chain).__name__)

    while not bus.empty():
        bus.get_nowait()
    # mentions 为空：已经认识的"自己"依然认得出来
    parsers["group_message_create"](gm(
        msg_id="ATSELF2", content="<@0A0B9F323E6AA18BF08B6901A3B2DEFC> 在吗", mentions=[]))
    evs = await drain(0.4)
    check("mentions 为空也能认出自己被 @（不依赖平台给 is_you）",
          len(evs) == 1 and evs[0].message.is_mentioned is True, str(len(evs)))
    if evs:
        ats = [e for e in evs[0].message.chain if isinstance(e, RealAt)]
        check("mentions 为空时同样拆成带 pid 的 At", len(ats) == 1 and ats[0].pid.startswith("0A0B"), "")

    while not bus.empty():
        bus.get_nowait()
    # 认识了自己之后，另一个"查不到的 @"不会被误认成自己
    parsers["group_message_create"](gm(
        msg_id="ATSELF4", content="<@FFFF0000111122223333444455556666> 早", mentions=[]))
    evs = await drain(0.4)
    if evs:
        ats = [e for e in evs[0].message.chain if isinstance(e, RealAt)]
        check("未知的 @ 也保留 pid、只是没有名字（无法编造身份）",
              len(ats) == 1 and ats[0].pid == "FFFF0000111122223333444455556666"
              and ats[0].nickname is None, chain_repr(evs[0].message.chain))
        check("未知的 @ 不会误唤醒", evs[0].message.is_mentioned is False)

    while not bus.empty():
        bus.get_nowait()
    parsers["group_message_create"](gm(
        msg_id="ATSELF3", content="<@1234567890ABCDEF> 早", mentions=[
            {"id": "1234567890ABCDEF", "username": "小明", "is_you": False}]))
    evs = await drain(0.4)
    if evs:
        ats = [e for e in evs[0].message.chain if isinstance(e, RealAt)]
        check("别人的 @ 也带上昵称（但 pid 仍是对方的）",
              len(ats) == 1 and ats[0].nickname == "小明"
              and ats[0].pid == "1234567890ABCDEF", chain_repr(evs[0].message.chain))
        check("只 @ 别人时不算唤醒（仍走围观/关键词）", evs[0].message.is_mentioned is False)

    # ---- 11. 引用消息 + @ 他人（对齐框架） ----
    from core.chat.message_elements import Reply as RealReply

    while not bus.empty():
        bus.get_nowait()
    # 引用一条"里面 @ 了机器人"的历史消息，本条消息本身没 @ 机器人
    quote = {
        "id": "QUOTE1",
        "author": {"id": "UID1", "member_openid": "UID1", "username": "小明", "bot": False},
        "content": " ",
        "group_openid": "GRP_OPENID_1",
        "message_type": 103,
        "msg_elements": [{
            "msg_idx": "REFIDX_abc==",
            "author": {"id": "UID2", "member_openid": "UID2", "username": "小红", "bot": False},
            "message_type": 0,
            "content": "<@0A0B9F323E6AA18BF08B6901A3B2DEFC> 我艾特过你",
        }],
        "message_scene": {"source": "default",
                          "ext": ["msg_idx=REFIDX_x==", "ref_msg_idx=REFIDX_abc=="]},
    }
    parsers["group_message_create"]({"op": 0, "s": 30, "t": "GROUP_MESSAGE_CREATE", "id": "EVQ", "d": quote})
    evs = await drain(0.4)
    check("引用消息正常投递", len(evs) == 1, str(len(evs)))
    if evs:
        chain = evs[0].message.chain
        replies = [e for e in chain if isinstance(e, RealReply)]
        check("★ 引用被解析成 Reply 元素（原生实现丢 msg_elements，是拿不到的）",
              len(replies) == 1, chain_repr(chain))
        if replies:
            sub = list(replies[0].chain or [])
            check("引用内容被带出来（LLM 能看到被引用的原话）",
                  any("我艾特过你" in (getattr(x, "text", "") or "") for x in sub), chain_repr(sub))
            check("引用内容里的 @ 也拆成标准 At",
                  any(isinstance(x, RealAt) and x.pid == "0A0B9F323E6AA18BF08B6901A3B2DEFC" for x in sub),
                  chain_repr(sub))
        check("★ 引用里的「自己的 @」不算现在在叫我（不误唤醒）",
              evs[0].message.is_mentioned is False)

    while not bus.empty():
        bus.get_nowait()
    # 一条消息同时 @ 两个人（都不是机器人）
    parsers["group_message_create"](gm(
        msg_id="ATOTHERS", content="<@AAAA1111BBBB2222> 你看 <@CCCC3333DDDD4444>",
        mentions=[{"id": "AAAA1111BBBB2222", "username": "小红", "is_you": False},
                  {"id": "CCCC3333DDDD4444", "username": "小刚", "is_you": False}]))
    evs = await drain(0.4)
    if evs:
        ats = [e for e in evs[0].message.chain if isinstance(e, RealAt)]
        check("@ 他人：两个都拆成标准 At，且各自 pid / 昵称正确",
              len(ats) == 2 and {a.pid for a in ats} == {"AAAA1111BBBB2222", "CCCC3333DDDD4444"}
              and [a.nickname for a in ats] == ["小红", "小刚"], chain_repr(evs[0].message.chain))
        check("@ 他人不算唤醒（只有真被 @ 才唤醒）", evs[0].message.is_mentioned is False)

    # ---- 12. 被引用回复 = 被提及（对齐 OneBot） ----
    while not bus.empty():
        bus.get_nowait()
    quoted_bot = {
        "id": "QRBOT1",
        "author": {"id": "UID1", "member_openid": "UID1", "username": "小明", "bot": False},
        "content": " ",
        "group_openid": "GRP_OPENID_1",
        "message_type": 103,
        "msg_elements": [{
            "msg_idx": "REFIDX_bot==",
            "author": {"id": "0A0B9F323E6AA18BF08B6901A3B2DEFC", "username": "香里", "bot": True},
            "message_type": 0,
            "content": "香里之前说过的话",
        }],
        "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_z==", "ref_msg_idx=REFIDX_bot=="]},
    }
    parsers["group_message_create"]({"op": 0, "s": 40, "t": "GROUP_MESSAGE_CREATE", "id": "EVQ2", "d": quoted_bot})
    evs = await drain(0.4)
    check("引用机器人的消息 -> is_mentioned=True（对齐 OneBot 语义）",
          len(evs) == 1 and evs[0].message.is_mentioned is True, str(len(evs)))
    if evs:
        check("引用内容仍照常带出来",
              any("香里之前说过的话" in (getattr(x, "text", "") or "")
                  for r in evs[0].message.chain if isinstance(r, RealReply)
                  for x in (r.chain or [])), chain_repr(evs[0].message.chain))

    while not bus.empty():
        bus.get_nowait()
    quoted_other = dict(quoted_bot)
    quoted_other = {**quoted_bot, "id": "QROTHER1"}
    quoted_other["msg_elements"] = [{
        "msg_idx": "REFIDX_o==",
        "author": {"id": "OTHERID000000000000", "username": "小红", "bot": False},
        "message_type": 0,
        "content": "小红之前说过的话",
    }]
    parsers["group_message_create"]({"op": 0, "s": 41, "t": "GROUP_MESSAGE_CREATE", "id": "EVQ3", "d": quoted_other})
    evs = await drain(0.4)
    check("引用别人的消息不算唤醒", len(evs) == 1 and evs[0].message.is_mentioned is False, str(len(evs)))

    # ---- 13. 机器人也能「引用回复」（message_reference / REFIDX） ----
    while not bus.empty():
        bus.get_nowait()
    # ① 收到一条带 msg_idx 的消息 -> 插件应记住"引用它要用哪个 REFIDX"
    parsers["group_message_create"]({
        "op": 0, "s": 50, "t": "GROUP_MESSAGE_CREATE", "id": "EVREF",
        "d": {"id": "MSG_REF1", "author": {"member_openid": "UID1", "username": "小明"},
              "content": "引用我试试", "group_openid": "GRP_OPENID_1", "message_type": 0,
              "message_scene": {"source": "default",
                                "ext": ["msg_idx=REFIDX_quoted==", "auth_token=x"]}}})
    evs = await drain(0.4)
    check("带 msg_idx 的消息正常投递", len(evs) == 1, str(len(evs)))
    if evs:
        display = str(evs[0].message.message_id)
        sid = str(evs[0].session.sid)
        check("插件记住了它的 REFIDX",
          plugin_main.ref_store_for(adapter).get((sid, display)) == "REFIDX_quoted==",
              str(dict(plugin_main.ref_store_for(adapter))))
        from core.chat.message_elements import Reply as _RealReply, Text as _RealText
        chain = [_RealReply(display), _RealText("引用测试")]
        check("链里有显式 Reply 时能解析出 REFIDX",
              plugin0._quote_ref_for(adapter, "GRP_OPENID_1", chain, True) == "REFIDX_quoted==")
        check("没有显式 Reply 时不返回引用（避免每条都挂引用）",
              plugin0._quote_ref_for(adapter, "GRP_OPENID_1", [_RealText("普通回复")], True) is None)

    # ② api 层注入：设置 contextvar 后调用，应带上 message_reference
    sent_calls.clear()
    token = plugin_main._QUOTE_REF.set("REFIDX_quoted==")
    try:
        await client.api.post_group_message(group_openid="GRP_OPENID_1", msg_type=0, content="hi")
    finally:
        plugin_main._QUOTE_REF.reset(token)
    check("★ 带上 message_reference（官方要求的 REFIDX）",
          sent_calls and sent_calls[0].get("message_reference") == {"message_id": "REFIDX_quoted=="},
          str(sent_calls[:1]))

    sent_calls.clear()
    token = plugin_main._QUOTE_REF.set("REFIDX_sent==")
    try:
        await client.api.post_group_message(group_openid="GRP_OPENID_1", msg_type=0, content="hi2")
    finally:
        plugin_main._QUOTE_REF.reset(token)
    check("机器人自己发的消息也记下了 ref_idx（以后能引用自己发过的消息）",
          any(k[0].endswith("GRP_OPENID_1") and k[1].startswith("qqo-")
              for k in plugin_main.ref_store_for(adapter)),
          str(list(plugin_main.ref_store_for(adapter))[:3]))

    sent_calls.clear()
    await client.api.post_group_message(group_openid="GRP_OPENID_1", msg_type=0, content="noquote")
    check("没有引用意图时不注入 message_reference",
          sent_calls and "message_reference" not in sent_calls[0], str(sent_calls[:1]))

    # ---- 14. 发出的 @ 是真 @（<qqbot-at-user id="..." />） ----
    from core.chat.message_elements import At as _At2, Text as _Text2
    encoded = adapter._text_content([_At2("9CD54739CC9BAA46B93243088802DC72", "周武"), _Text2("哥ww")])
    check("★ 发出的 @ 是标记而不是纯文本 @昵称（默认 legacy 形态）",
          encoded == '<@9CD54739CC9BAA46B93243088802DC72>哥ww', encoded)
    encoded2 = adapter._text_content([_Text2("hi"), _At2("all", "全体成员")])
    check("pid=all 退化成文本（平台不支持 @全体）",
          "qqbot-at-user" not in encoded2, encoded2)

    # ---- 15. 富内容归一化（真实适配器渲染） ----
    while not bus.empty():
        bus.get_nowait()
    # ① 语音 + 平台自带 ASR → 直接用文字
    parsers["group_message_create"](gm(msg_id="VOICE1", content="", attachments=[
        {"content_type": "voice", "url": "http://x/a.silk",
         "voice_wav_url": "http://x/a.wav", "asr_refer_text": "这是语音内容"}]))
    evs = await drain(0.4)
    if evs:
        text = chain_repr(evs[0].message.chain)
        check("★ 语音用平台自带 ASR（不再跑本地 STT）", "[语音: 这是语音内容]" in text, text)
        check("不再生成音频元素（避免重复识别）",
              not any(type(e).__name__ == "Record" for e in evs[0].message.chain), text)

    while not bus.empty():
        bus.get_nowait()
    # ② 结构化卡片
    parsers["group_message_create"](gm(msg_id="ARK1", content="", message_type=3, ark_data={
        "ark_name": "图文卡片", "ark_type": "feed", "fields": {"title": "某个帖子", "desc": "看看这个"}}))
    evs = await drain(0.4)
    if evs:
        text = chain_repr(evs[0].message.chain)
        check("★ 结构化卡片不再变成 [Unsupported message]",
              "卡片" in text and "某个帖子" in text and "Unsupported" not in text, text)

    while not bus.empty():
        bus.get_nowait()
    # ③ 表情标记
    import base64 as _b64
    _ext = _b64.b64encode(json.dumps({"text": "微笑"}).encode()).decode()
    parsers["group_message_create"](gm(msg_id="FACE1", content=f'<faceType=6, faceId="0", ext="{_ext}"> 你好'))
    evs = await drain(0.4)
    if evs:
        text = chain_repr(evs[0].message.chain)
        check("★ 表情标记解码成可读文字", "[表情: 微笑]" in text and "faceType" not in text, text)

    # ---- 17. 引用一条语音：被引用的语音也要变成可读文字 ----
    while not bus.empty():
        bus.get_nowait()
    quote_voice = {
        "id": "QV1",
        "author": {"id": "UID1", "member_openid": "UID1", "username": "小明", "bot": False},
        "content": " ",
        "group_openid": "GRP_OPENID_1",
        "message_type": 103,
        "msg_elements": [{
            "msg_idx": "REFIDX_v==",
            "author": {"id": "UID2", "member_openid": "UID2", "username": "小红", "bot": False},
            "message_type": 0,
            "content": " ",
            "attachments": [{"content_type": "voice", "asr_refer_text": "这是一段被引用的语音"}],
        }],
        "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_w==", "ref_msg_idx=REFIDX_v=="]},
    }
    parsers["group_message_create"]({"op": 0, "s": 60, "t": "GROUP_MESSAGE_CREATE", "id": "EVQV", "d": quote_voice})
    evs = await drain(0.4)
    if evs:
        sub = [x for r in evs[0].message.chain if isinstance(r, RealReply) for x in (r.chain or [])]
        check("★ 引用一条语音：被引用的语音变成可读文字（不再是 File）",
              any("[语音: 这是一段被引用的语音]" in (getattr(x, "text", "") or "") for x in sub),
              chain_repr(sub))

    # ---- 18. 确定性：同一内容重复构造，渲染必须逐字节一致（提示词缓存友好）----
    while not bus.empty():
        bus.get_nowait()
    renders = set()
    det_voice = {"content_type": "voice", "asr_refer_text": "语音内容"}
    det_base = {
        "author": {"id": "UID9", "member_openid": "UID9", "username": "小明", "bot": False},
        "content": '<@A1B2C3D4E5F60718293A4B5C6D7E8F90> 你看 <faceType=6, faceId="0", ext="eyJ0ZXh0IjogIuW+rueskSJ9">',
        "group_openid": "GRP_OPENID_1",
        "message_type": 103,
        "mentions": [{"id": "A1B2C3D4E5F60718293A4B5C6D7E8F90", "username": "香里", "is_you": True}],
        "attachments": [det_voice],
        "msg_elements": [{"author": {"id": "UID8", "username": "小红"}, "content": " ",
                          "attachments": [dict(det_voice)]}],
        "message_scene": {"source": "default", "ext": ["msg_idx=REFIDX_det=="]},
    }
    for i in range(30):
        body_i = dict(det_base)
        body_i["id"] = f"R{i}"
        parsers["group_message_create"]({"op": 0, "s": 200 + i, "t": "GROUP_MESSAGE_CREATE",
                                        "id": f"EVR{i}", "d": body_i})
        for ev in await drain(0.15):
            renders.add(chain_repr(ev.message.chain))
    check("★ 同一内容 30 次 → 渲染逐字节一致（不吃掉提示词缓存）", len(renders) == 1, len(renders))

    # 状态无关性：灌 50 条别的消息后重放同一条，渲染必须仍与首次一致
    first_render = sorted(renders)[0] if renders else ""
    for i in range(50):
        other = dict(det_base)
        other["id"] = f"NOISE{i}"
        other["content"] = f"无关消息 {i}"
        other["author"] = {"id": f"UN{i}", "member_openid": f"UN{i}",
                           "username": f"路人{i}", "bot": False}
        parsers["group_message_create"]({"op": 0, "s": 400 + i, "t": "GROUP_MESSAGE_CREATE",
                                        "id": f"EVN{i}", "d": other})
        await drain(0.05)
    replay = dict(det_base)
    replay["id"] = "REPLAY1"
    parsers["group_message_create"]({"op": 0, "s": 700, "t": "GROUP_MESSAGE_CREATE", "id": "EVRE", "d": replay})
    replayed = {chain_repr(ev.message.chain) for ev in await drain(0.3)}
    check("★ 状态无关：50 条杂音后重放同一条，渲染与首次完全一致（注入内容是纯函数）",
          replayed == {first_render}, f"{len(replayed)} 种")

    # ---- 19. 引用索引是「适配器级」共享的：热重载换实例也要能查到 ----
    from core.chat.message_elements import Reply as _SR, Text as _ST
    plugin_extra = plugin_main.QQOfficialGroupBridge(FakeCtx(), cfg0)
    plugin_extra._remember_ref(adapter, "qq:gm:GRP_OPENID_1", "qqo-shared", "REFIDX_shared==")
    shared_chain = [_SR(message_id="qqo-shared", chain=[_ST("x")]), _ST("reply")]
    found_shared = plugin0._quote_ref_for(adapter, "GRP_OPENID_1", shared_chain, True)
    check("★ 实例 A 记录的引用，实例 B 也能查到（适配器级共享，防热重载丢索引）",
          found_shared == "REFIDX_shared==", found_shared)

    # ---- 20. 二次加载（热重载）：新实例必须顶掉旧补丁层，行为仍正确 ----
    plugin_again = plugin_main.QQOfficialGroupBridge(FakeCtx(), cfg0)
    await plugin_again.initialize()
    out_again = adapter._text_content([_ST('<qqbot-at-user id="9CD54739CC9BAA46B93243088802DC72" />哥ww')])
    check("★ 二次加载后，正文里的标记仍被归一化成 legacy 形态",
          str(out_again).startswith("<@9CD54739CC9BAA46B93243088802DC72>"), str(out_again)[:50])
    check("补丁层记录的仍是最初的原始实现",
          getattr(adapter._text_content, "_kira_bridge_orig", None) is not None, None)
    await plugin_again.terminate()

    # ---- 21. 含 @ 标记的正文必须走 markdown（纯文本没有 @ 能力）----
    import types

    class _FakeApi:
        def __init__(self):
            self.calls = []

        async def post_group_message(self, **kw):
            self.calls.append(kw)
            return {"id": "SENT1", "ext_info": {"ref_idx": "REFIDX_sent=="}}

        async def post_c2c_message(self, **kw):
            self.calls.append(kw)
            return {"id": "SENT2"}

    fake_api = _FakeApi()
    plugin_md = plugin_main.QQOfficialGroupBridge(FakeCtx(), cfg0)
    adapter.client = types.SimpleNamespace(api=fake_api)
    plugin_md._patch_send_path(adapter, "QQ Official", adapter.client)
    _at_id = "9CD54739CC9BAA46B93243088802DC72"
    import asyncio as _aio

    await fake_api.post_group_message(group_openid="G1", msg_type=0,
                                      content=f"<@{_at_id}>哥ww", msg_id="M1", msg_seq=1)
    md_call = fake_api.calls[-1]
    check("★ 含 @ 标记 → 自动改走 markdown（msg_type=2）",
          md_call.get("msg_type") == 2 and md_call.get("markdown", {}).get("content") == f"<@{_at_id}>哥ww",
          {k: md_call.get(k) for k in ("msg_type", "content", "markdown")})

    await fake_api.post_group_message(group_openid="G1", msg_type=0, content="普通消息", msg_id="M2", msg_seq=1)
    plain_call = fake_api.calls[-1]
    check("不含 @ 的正文不受影响（仍走纯文本）",
          plain_call.get("msg_type") == 0 and plain_call.get("markdown") is None,
          {k: plain_call.get(k) for k in ("msg_type", "content")})

    await plugin0.terminate()
    check("grace=0 实例正常收尾", True)

    print("\n" + "=" * 72)
    print(f"结果：{_PASS} passed, {_FAIL} failed")
    print("=" * 72)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
