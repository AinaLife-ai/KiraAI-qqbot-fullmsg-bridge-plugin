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
import os
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


def gm(msg_id="MSG1", content="大家早上好", mentions=None, uid="UID1", username="小明"):
    return {"op": 0, "s": 7, "t": "GROUP_MESSAGE_CREATE", "id": "EVT1", "d": {
        "id": msg_id,
        "author": {"id": uid, "member_openid": uid, "member_role": "member",
                   "username": username, "bot": False},
        "content": content, "group_openid": "GRP_OPENID_1", "message_type": 0,
        **({"mentions": mentions} if mentions is not None else {}),
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
    print("QQ Official bridge v1.1.0 — smoke test (real KiraAI core + real botpy)")
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
