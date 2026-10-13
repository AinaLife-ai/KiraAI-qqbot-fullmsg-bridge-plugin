"""★ 实机验证：按钮**到期/满额/手动截止后，再点真的不再触发**（不是"重新触发"）。

用真实链路（真策略引擎 + 真 InteractionBridge + 真发送路径），两代核心各跑一遍。
覆盖：
  ① 名额上限：新人再点 / 已点过的人再点 ⇒ 都不再转给模型；超额记账；查账显示「已截止」
  ② 到期（until 已过）⇒ 不再触发
  ③ 手动 close ⇒ 不再触发
  ④ **同一会话同一 button id 有两条账**（旧的开着、新的已满）⇒ 仍不触发（不回退到旧账）
  ⑤ extend / reset ⇒ **应当恢复**触发（证明不是永久锁死）
"""
"""实机验证：按钮到上限 / 过期 / 手动截止之后，再点是否**真的不再触发**。

用真实链路：真 KeyboardTag/真 api_send/真 InteractionBridge/真策略引擎，
核心用 rc1（必要时换 CORE_DIR 对比）。
"""
import asyncio
import os
import sys
import time

CORE = os.environ.get("CORE_DIR", "/var/minis/shared/kira30rc1")
sys.path.insert(0, CORE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import botpy_parent           # noqa: E402
sys.path.insert(0, str(botpy_parent()))
from _env import bridge_root as _BR
sys.path.insert(0, str(_BR()))
os.makedirs("{BR}/data", exist_ok=True)
open("{BR}/data/log.log", "a").close()

import smoke_v3 as T                    # noqa: E402
import main as bm                       # noqa: E402
from interactions import InteractionBridge as IB      # noqa: E402
from core.chat.message_utils import MessageChain as Chain   # noqa: E402
from core.chat.message_elements import Text as CText        # noqa: E402

GID = "D593FBB0F18264477911ECDCBF336D6F"
PASS, FAIL = [0], []


def run(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def chk(name, ok, extra=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f"   [{extra}]" if (extra and not ok) else ""))
    if ok:
        PASS[0] += 1
    else:
        FAIL.append(name)


async def main():
    a = T.make_adapter()
    p = T.make_plugin(a)
    await p._tick()
    cl = a.get_client()

    async def ack(_i, _c):
        return None
    try:
        cl.api.on_interaction_result = ack
    except Exception:
        pass

    delivered = []
    p.publish_synthetic_event = lambda **kw: delivered.append(kw) or True
    br = IB(p, __import__("logging").getLogger("plugin"))

    async def send_kb(spec, label, bid="b1"):
        kb, sp = bm.button_policy.split_keyboard_declaration(
            {"content": {"rows": [{"buttons": [
                {"id": bid, "render_data": {"label": "报名", "style": 1},
                 "action": {"type": 1, "data": "join", "permission": {"type": 2}}}]}]}},
            dict(spec, label=label))
        mk = bm.KeyboardMarker(kb)
        mk.kirai_spec = sp
        await a.send_group_message(GID, Chain([CText("来报名"), mk]))

    async def click(uid, tag, bid="b1"):
        await br._on_interaction(cl, {
            "id": f"IT-{tag}", "type": 11,
            "data": {"resolved": {"button_id": bid, "button_data": "join"}},
            "group_openid": GID, "group_member_openid": uid})

    def pol_of(label):
        for v in p.button_policies._pol.values():
            if v.get("label") == label:
                return v
        return None

    # ---------- ① 上限：满额后再点 ----------
    print("\n[1] 名额上限（max=2）")
    await send_kb({"max": "2"}, "上限A", bid="m1")
    await click("U1", "a1", bid="m1")
    await click("U2", "a2", bid="m1")
    _n_full = len(delivered)
    await click("U3", "a3", bid="m1")          # 第 3 人：应**不触发**
    await click("U4", "a4", bid="m1")          # 第 4 人：应**不触发**
    await click("U1", "a5", bid="m1")          # 已点过的人再点：也不该触发
    chk("满额后 3 次点击**都不再转给模型**", len(delivered) == _n_full,
        f"{_n_full} → {len(delivered)}")
    _p = pol_of("上限A")
    chk("超额被记账（overflow=3）", int(_p.get("overflow") or 0) == 3, str(_p.get("overflow")))
    _buckets = _p.get("btn") or {}
    chk("策略/逐按钮桶已标记截止",
        bool(_p.get("closed")) or any(bool(b.get("closed")) for b in _buckets.values()),
        f"pol={_p.get('closed')} buckets={ {k: v.get('closed') for k, v in _buckets.items()} }")
    # ★ 关键：查账要把它显示成"已截止"（而不是永远进行中）
    _st = p.button_policies.stats(_p)
    _txt = bm.button_policy.render_stats_text(_st, name_of=lambda u: None)
    chk("查账文本显示「已截止」且不再「进行中」",
        "已截止" in _txt and "进行中" not in _txt, _txt[:120])

    # ---------- ② 过期（ttl/until）后再点 ----------
    print("\n[2] 到期（until 已过）")
    await send_kb({"max": "9", "until": "23:59"}, "到期B", bid="e1")
    await click("V1", "b1", bid="e1")
    _n2 = len(delivered)
    _p2 = pol_of("到期B")
    _p2["until"] = time.time() - 1              # 手动把截止时间推到现在之前
    await click("V2", "b2", bid="e1")
    await click("V3", "b3", bid="e1")
    chk("到期后再点**不再触发**", len(delivered) == _n2, f"{_n2} → {len(delivered)}")

    # ---------- ③ 手动 close 后再点 ----------
    print("\n[3] 手动截止（工具 close）")
    await send_kb({"max": "9"}, "手截C", bid="k1")
    await click("W1", "c1", bid="k1")
    _n3 = len(delivered)
    p.button_policies.close(pol_of("手截C")["key"], "manual")
    await click("W2", "c2", bid="k1")
    chk("手动截止后再点**不再触发**", len(delivered) == _n3, f"{_n3} → {len(delivered)}")

    # ---------- ④ ★ 关键：同会话同按钮 id，**旧账还开着、新账已满** ----------
    print("\n[4] ★ 两条账同一个 button id（旧的开着、新的已满）")
    await send_kb({"max": "1"}, "两条D-旧", bid="d1")     # 先发的（旧）
    await send_kb({"max": "1"}, "两条D-新", bid="d1")     # 后发的（新，界面上就是它）
    await click("X1", "d1", bid="d1")                    # 点满"新"那条
    _n4 = len(delivered)
    await click("X2", "d2", bid="d1")                    # 界面上的按钮已经满了 ⇒ 应**不触发**
    chk("点满最新那条后，再点仍**不触发**（不该回退到旧账）",
        len(delivered) == _n4, f"{_n4} → {len(delivered)}；旧账 total={(pol_of('两条D-旧') or {}).get('total')}")

    # ---------- ⑤ 加名额 / 重开 之后**应当**恢复触发 ----------
    print("\n[5] extend / reset 之后应当恢复")
    _n5 = len(delivered)
    p.button_policies.extend(pol_of("上限A")["key"], max_add=1)
    await click("U5", "a6", bid="m1")
    chk("extend 加名额后**恢复触发**（说明前面不是永久锁死）", len(delivered) == _n5 + 1,
        f"{_n5} → {len(delivered)}")
    p.button_policies.reset(pol_of("手截C")["key"])
    _n6 = len(delivered)
    await click("W3", "c3", bid="k1")
    chk("reset 重开后**恢复触发**", len(delivered) == _n6 + 1, f"{_n6} → {len(delivered)}")

    # ---------------- [6] 系统截止通知：不得被当成「某个成员」 ----------------
    print("\n[6] 系统截止通知（真实日志发现的问题）")
    # 本段用**干净的桩环境**（前面的段落改过各自的对象）
    a6 = T.make_adapter()
    p6 = T.make_plugin(a6)
    await p6._tick()

    class _SYSSender:
        user_id = "system"
        nickname = "未知用户(system)"

    class _SYSMsg:
        sender = _SYSSender()
        chain = []
        message_id = "system"

    class _SYSEv:
        message = _SYSMsg()
        adapter = type("A", (), {"name": "qqo", "platform": "QQ Official"})()
        session = type("S", (), {"session_type": "gm", "session_id": "qq:gm:GRP"})()

    _before = dict(getattr(p6.identities, "_store", {}) or {})
    p6._learn_peer_from_event(_SYSEv())
    _after = dict(getattr(p6.identities, "_store", {}) or {})
    chk("系统来源（sender_id=system / 合成消息）**不进通讯录**",
        not p6.identities.lookup_any("system") and len(_after) == len(_before),
        f"lookup={p6.identities.lookup_any('system')!r}")

    # 6.2 真发一条"系统截止通知"，看它落在事件里长什么样
    _evs = []
    _orig = a.publish
    a6.publish = lambda ev: _evs.append(ev) or True
    try:
        p6.publish_synthetic_event(target_id="GRP", sender_id="system", is_group=True,
                                  text="[按钮截止] 「系统通知测试」已到截止时间：共 1 次点击")
    finally:
        a6.publish = _orig
    _ev = _evs[-1] if _evs else None
    _txt = str(getattr(getattr(_ev, "message", None), "message_str", "") or "")
    _nick = str(getattr(getattr(getattr(_ev, "message", None), "sender", None), "nickname", "") or "")
    chk("截止通知正文**不含**「未知用户(system)」这类后缀",
        _ev is not None and "未知用户(" not in _txt and "）" not in _txt.split("：")[-1][:40],
        _txt[:120])
    chk("截止通知的昵称是「系统」（不是某个成员）", _nick == "系统", repr(_nick))

    # 6.3 历史脏数据清理：占位名 / 哨兵 uid 删掉；真人真名保留
    p6.identities.remember("qqo", "gm", "system", "未知用户(system)")
    p6.identities.remember("qqo", "gm", "REAL-UID-X", "周武")
    p6._purge_junk_identities()
    chk("历史脏数据（占位名/哨兵）被清理，真人真名保留",
        p6.identities.lookup_any("system") is None
        and p6.identities.lookup_any("REAL-UID-X") == "周武",
        f"system={p6.identities.lookup_any('system')!r} real={p6.identities.lookup_any('REAL-UID-X')!r}")

    print(f"\n结果：{PASS[0]} passed, {len(FAIL)} failed" + (f" ｜ 失败：{FAIL}" if FAIL else ""))
    return 1 if FAIL else 0


sys.exit(run(main()))
