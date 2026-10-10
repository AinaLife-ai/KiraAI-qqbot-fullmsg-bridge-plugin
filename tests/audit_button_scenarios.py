"""★ 真实场景端到端：按钮策略（v1.6.34）—— 不是单测函数，而是**走完整链路**。

每个场景都走真实代码：`<keyboard>` 标签 → 真实发送（api_send）→ 真实互动回调
（InteractionBridge）→ 策略判定 → 是否转达模型 → 真实查账/管理工具。

场景清单
--------
S1 群聊「报名」限 3 人：前 3 次都转、第 3 次带"最后一个名额"汇总、第 4 人不转；
   查账看到 3/3 与名单；加名额后又能点。
S2 限时（ttl）：到点后点击不转；巡检发一条「已截止」给模型（带名单）；不重复发。
S3 每人一次（once）：同一人第二次不转（硬）；软模式下转达并标注「重复」。
S4 一排两按钮：`scope=each` ⇒ "取消"被点不吃"报名"的名额。
S5 连点保护（cooldown）：5 秒内连点只算一次；过了冷却又能点。
S6 私聊按钮：判定正常；截止通知按**单聊**投递（不是群）。
S7 图 + 正文 + 键盘 + 策略：拆两条、策略只登记一次、媒体那条不带策略、载荷零私有键。
S8 重启：账本落盘 → 新进程加载 → 计数/名单/状态都在，继续点仍然正确。
S9 全场景出站报文**深扫**：只含官方字段。
"""
import asyncio
import json as _json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR  # noqa: E402

ROOT = _BR()
GEN = os.environ.get("KIRA_CORE_GEN", "3")
sys.path.insert(0, str(_CORE_ROOT(GEN)))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(_BOTPY_DIR()))
os.makedirs(f"{ROOT}/data", exist_ok=True)
open(f"{ROOT}/data/log.log", "a").close()

_PASS, _FAIL = [0], []


def check(name, cond, detail=""):
    if cond:
        _PASS[0] += 1
        print("  ok   " + name)
    else:
        _FAIL.append(name)
        print("  FAIL " + name + (f"  {detail}" if detail else ""))


OFFICIAL = {"id", "content", "rows", "buttons", "render_data", "label", "visited_label",
            "style", "action", "type", "data", "permission", "specify_user_ids",
            "specify_role_ids", "enter", "reply", "anchor", "click_limit",
            "unsupport_tips", "at_bot_show_channel_list", "group_id", "modal",
            "confirm_text", "cancel_text"}


def _keys_deep(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _keys_deep(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _keys_deep(v, out)


def run_async(coro):
    """本套件在事件循环里跑 ⇒ 用独立线程执行异步链路。"""
    box, err = [], []

    def _t():
        loop = asyncio.new_event_loop()
        try:
            box.append(loop.run_until_complete(coro))
        except Exception as exc:                       # noqa: BLE001
            err.append(exc)
        finally:
            loop.close()

    th = threading.Thread(target=_t)
    th.start()
    th.join(60)
    if err:
        raise err[0]
    return box[0] if box else None


def make_env():
    """真实适配器 + 插件 + 已挂好的互动回调（含 ack 桩）。"""
    import smoke_v3 as T
    import main as bm

    a = T.make_adapter()
    a.client.api._http = T.FakeHTTP({"/files": {"file_info": "FI"}})
    try:
        from core.adapter.capabilities import IMCapability

        a.get_capability(IMCapability)._group_reply_ids["GRP"] = "MSG-IN"
    except Exception:
        try:
            a._group_reply_ids["GRP"] = "MSG-IN"
        except Exception:
            pass
    p = T.make_plugin(a)
    return T, bm, a, p


class Ctx:
    """让查账工具能找到插件。"""

    def __init__(self, plugin):
        self._p = plugin

    def get_plugin_inst(self, _pid):
        return self._p


class Ev:
    def __init__(self, sid, plugin, is_group=True):
        self.session = type("S", (), {"session_id": sid,
                                      "session_type": "gm" if is_group else "dm"})()
        self.ctx = Ctx(plugin)


def send_kb(T, bm, a, p, sid, attrs, buttons, *, is_group=True):
    """走真实链路发一条带策略的键盘消息；返回 (出站 kwargs 列表, 策略 key)。"""
    kb_json = _json.dumps({"content": {"rows": [{"buttons": buttons}]}}, ensure_ascii=False)
    tag = bm.KeyboardTag()
    els = run_async(tag.handle(kb_json, **attrs))
    calls = []

    async def _go():
        if is_group:
            await a.send_group_message(sid, _C((els[0],)))
        else:
            try:
                await a.send_direct_message(sid, _C((els[0],)))
            except Exception:
                await a.send_private_message(sid, _C((els[0],)))
        return [kw for _kind, kw in a.client.api.calls]

    from core.chat.message_utils import MessageChain as _C

    globals()["_C"] = _C
    calls = run_async(_go())
    keys = list(getattr(p.button_policies, "_pol", {}) or {})
    return calls, (keys[-1] if keys else None)


def clicks(bm, p, a, sid, uid_data, *, is_group=True):
    """连续点击；返回每次之后模型收到的消息文本列表。"""
    from interactions import InteractionBridge

    caps = []
    p.publish_synthetic_event = lambda **kw: caps.append(kw) or True
    client = p._find_adapters()[0][1].get_client()

    async def _ack(_i, _c):
        return None

    try:
        client.api.on_interaction_result = _ack
    except Exception:
        pass
    br = InteractionBridge(p, __import__("logging").getLogger("plugin"))

    async def _go():
        out = []
        for idx, (uid, data) in enumerate(uid_data):
            body = {"id": f"IT-{uid}-{idx}", "type": 11,
                    "data": {"resolved": {"button_id": data.split("@")[-1] if "@" in data else "b1",
                                          "button_data": data.split("@")[0]}},
                    ("group_openid" if is_group else "user_openid"): sid,
                    }
            if is_group:
                body["group_member_openid"] = uid
            await br._on_interaction(client, body)
            out.append([c.get("text") or "" for c in caps])
        return out, caps

    return run_async(_go())


# ===================================================================== #
print("\n[S1] 群聊「报名」限 3 人（真实链路）")
T, bm, a, p = make_env()
run_async(p._tick())
BTN = [{"id": "b1", "render_data": {"label": "报名", "style": 1},
        "action": {"type": 1, "data": "join", "permission": {"type": 2}}}]
calls, key = send_kb(T, bm, a, p, "GRP", {"max": "3", "label": "报名"}, BTN)
keys_seen = set()
for c in calls:
    if c.get("keyboard"):
        _keys_deep(c["keyboard"], keys_seen)
check("S1-1 出站键盘只含官方字段（深扫）", not (keys_seen - OFFICIAL),
      str(sorted(keys_seen - OFFICIAL)))
check("S1-2 策略已登记", key is not None, str(key))
res, caps = clicks(bm, p, a, "GRP", [("U1", "join"), ("U2", "join"), ("U3", "join"),
                                     ("U4", "join"), ("U5", "join")])
check("S1-3 前 3 次都转给模型（默认 deliver=all；每批独立计数）",
      [len(r) for r in res[:3]] == [1, 2, 3],
      f"{[len(r) for r in res[:3]]}")
check("S1-4 第 3 次（最后一个名额）带汇总",
      "最后一个名额" in res[2][-1] or "已截止" in res[2][-1], res[2][-1][:90])
check("S1-5 第 4/5 人超额 ⇒ 不再打扰模型（硬模式）",
      [len(r) for r in res[3:]] == [3, 3], f"{[len(r) for r in res[3:]]}")

import button_tools as BT                                            # noqa: E402

ev = Ev("qq:gm:GRP", p)
out = run_async(BT.QQButtonStatsTool(ctx=Ctx(p)).execute(ev))
check("S1-6 查账：3/3、已截止、名单可见",
      "已点 3 次" in out and ("已截止" in out or "剩 0" in out), out[:150])
_pol = p.button_policies._pol[key]
check("S1-7 账本里正好 3 人（超额的没被计入）",
      int(_pol["total"]) == 3 and len(_pol["users"]) == 3,
      f"total={_pol['total']} people={len(_pol['users'])}")
out2 = run_async(BT.QQButtonExtendTool(ctx=Ctx(p)).execute(ev, add_slots=2))
res2, _ = clicks(bm, p, a, "GRP", [("U4", "join"), ("U5", "join"), ("U6", "join")])
check("S1-8 加 2 个名额后：U4/U5 能点（都转达）、U6 又被拦",
      "已调整" in out2 and [len(r) for r in res2] == [1, 2, 2],
      f"{[len(r) for r in res2]}")

# ===================================================================== #
print("\n[S2] 限时（ttl）：到点后不转 + 巡检发「已截止」")
T2, bm2, a2, p2 = make_env()
run_async(p2._tick())
_, key2 = send_kb(T2, bm2, a2, p2, "GRP", {"ttl": "60", "label": "限时报名"}, BTN)
clicks(bm2, p2, a2, "GRP", [("A1", "join"), ("A2", "join")])
pol2 = p2.button_policies._pol[key2]
pol2["until"] = time.time() - 1                      # 时间推进（等价于等 60 秒）
res3, _ = clicks(bm2, p2, a2, "GRP", [("A3", "join")])
check("S2-1 过期后的点击不转给模型（本次调用 0 次转达）",
      len(res3[0]) == 0, str(len(res3[0])))
nt = p2.policy_close_notices()
check("S2-2 巡检发出「已截止」并带名单",
      len(nt) == 1 and "已到截止时间" in nt[0]["text"] and "2" in nt[0]["text"],
      str(nt)[:160])
check("S2-3 不重复通知", p2.policy_close_notices() == [], "")

# ===================================================================== #
print("\n[S3] 每人一次（once）：硬模式拦、软模式转达")
T3, bm3, a3, p3 = make_env()
run_async(p3._tick())
_, key3 = send_kb(T3, bm3, a3, p3, "GRP", {"once": "1", "label": "限一次"}, BTN)
r_h, _ = clicks(bm3, p3, a3, "GRP", [("B1", "join"), ("B1", "join")])
check("S3-1 硬模式：同一人第二次不转", len(r_h[0]) == 1 and len(r_h[1]) == 1,
      f"{[len(r) for r in r_h]}")
T3b, bm3b, a3b, p3b = make_env()
run_async(p3b._tick())
_, key3b = send_kb(T3b, bm3b, a3b, p3b, "GRP",
                   {"once": "1", "hard": "0", "label": "限一次-软"}, BTN)
r_s, caps_s = clicks(bm3b, p3b, a3b, "GRP", [("B1", "join"), ("B1", "join")])
check('S3-2 软模式：第二次仍转达，并注明「已点过、未计入」',
      len(r_s[1]) == 2 and ("已点过" in r_s[1][-1] or "未计入" in r_s[1][-1]), r_s[1][-1][:100])

# ===================================================================== #
print("\n[S4] 一排两按钮：取消不吃报名的名额（scope=each）")
T4, bm4, a4, p4 = make_env()
run_async(p4._tick())
BTN2 = [{"id": "join", "render_data": {"label": "报名", "style": 1},
         "action": {"type": 1, "data": "go", "permission": {"type": 2}}},
        {"id": "cancel", "render_data": {"label": "取消", "style": 0},
         "action": {"type": 1, "data": "no", "permission": {"type": 2}}}]
_, key4 = send_kb(T4, bm4, a4, p4, "GRP", {"max": "1", "label": "报名/取消"}, BTN2)
r4, _ = clicks(bm4, p4, a4, "GRP", [("C1", "no@cancel"), ("C2", "no@cancel"),
                                    ("C3", "go@join")])
# scope=each ⇒ 每个按钮各自封顶 1：cancel 第 2 次被拦（不转），join 首次照常转
check("S4-1 取消到自己的上限后不再转（不占报名名额）",
      len(r4[0]) == 1 and len(r4[1]) == 1 and len(r4[2]) == 2,
      f"{[len(r) for r in r4]}")
r4b, _ = clicks(bm4, p4, a4, "GRP", [("C4", "go@join")])
check("S4-2 报名自己到上限后拦下第 2 次（0 次转达）",
      len(r4b[0]) == 0, str(len(r4b[0])))
st4 = p4.button_policies.stats(p4.button_policies._pol[key4], limit=3)
check("S4-3 查账给逐按钮明细（cancel 1 次 / join 1 次，各算各的）",
      {b["button"]: b["total"] for b in st4["buttons"]} == {"cancel": 1, "join": 1},
      str(st4["buttons"]))

# ===================================================================== #
print("\n[S5] 连点保护（cooldown=30，真实时间）")
T5, bm5, a5, p5 = make_env()
run_async(p5._tick())
_, key5 = send_kb(T5, bm5, a5, p5, "GRP", {"cooldown": "30", "label": "防连点"}, BTN)
r5, _ = clicks(bm5, p5, a5, "GRP", [("D1", "join")] * 5)
check("S5-1 连点 5 次只算 1 次（其余不转）", len(r5[0]) == 1 and len(r5[-1]) == 1,
      f"{[len(r) for r in r5]}")
check("S5-2 账本 total=1", int(p5.button_policies._pol[key5]["total"]) == 1, "")
pol5 = p5.button_policies._pol[key5]
for _b in (pol5.get("btn") or {}).values():          # scope=each ⇒ 时间戳在桶里
    if "D1" in (_b.get("users") or {}):
        _b["users"]["D1"][2] = time.time() - 31
    pol5.setdefault("users", {}).setdefault("D1", [1, 0, 0])[2] = time.time() - 31
r5b, _ = clicks(bm5, p5, a5, "GRP", [("D1", "join")])
check("S5-3 冷却过后可以再点（转达 1 次）", len(r5b[0]) == 1, str(len(r5b[0])))

# ===================================================================== #
print("\n[S6] 私聊按钮：判定 + 截止通知按单聊投递")
T6, bm6, a6, p6 = make_env()
run_async(p6._tick())
_, key6 = send_kb(T6, bm6, a6, p6, "DM-1", {}, BTN, is_group=False)
pol6 = p6.button_policies._pol[key6]
check("S6-1 策略登记为单聊（is_group=False）", pol6["is_group"] is False, str(pol6["is_group"]))
r6, _ = clicks(bm6, p6, a6, "DM-1", [("E1", "join")], is_group=False)
check("S6-2 私聊点击照常转达", len(r6[0]) == 1, str(len(r6[0])))

# ===================================================================== #
print("\n[S7] 图 + 正文 + 键盘 + 策略：拆两条、策略只登记一次、媒体不带策略")
T7, bm7, a7, p7 = make_env()
run_async(p7._tick())
from PIL import Image as _PIL                                          # noqa: E402

_png = os.path.join(tempfile.gettempdir(), "scen_kb.png")
_PIL.new("RGB", (8, 8), (10, 120, 40)).save(_png)
_els7 = run_async(bm7.KeyboardTag().handle(
    _json.dumps({"content": {"rows": [{"buttons": BTN}]}}, ensure_ascii=False),
    **{"max": "2", "label": "带图报名"}))
from core.chat.message_elements import Image as _CImg, Text as _CTxt   # noqa: E402
from core.chat.message_utils import MessageChain as _CM                # noqa: E402

_calls7 = run_async(a7.send_group_message("GRP", _CM([
    _CTxt("看这个"), _CImg(image=_png, mime="image/png", name="scen_kb.png"), _els7[0]])))
_k7 = set()
for c in [kw for _k, kw in a7.client.api.calls]:
    if c.get("keyboard"):
        _keys_deep(c["keyboard"], _k7)
check("S7-1 出站键盘（含媒体拆分）仍只含官方字段", not (_k7 - OFFICIAL),
      str(sorted(_k7 - OFFICIAL)))
check("S7-2 只登记了一条策略（媒体那条不带策略）",
      len(p7.button_policies._pol) == 1, str(len(p7.button_policies._pol)))

# ===================================================================== #
print("\n[S8] 重启：账本落盘 → 新进程加载 → 继续点仍然正确")
_p = os.path.join(tempfile.mkdtemp(prefix="scen_"), "pol.json")
import button_policy as BP                                             # noqa: E402

st = BP.ButtonPolicyStore(path=_p)
polA = st.register("GRP", {"max": 2, "label": "重启测"}, button_ids=["b1"],
                   is_group=True, adapter="qqo")
st.decide(polA, "U1", button_id="b1")
st.decide(polA, "U2", button_id="b1")
st.save()
st2 = BP.ButtonPolicyStore(path=_p)
got, _clean = st2.resolve(message_id="", sid="GRP", button_id="b1", data="join")
v = st2.decide(got, "U3", button_id="b1")
check("S8-1 重启后计数与名单还在（total=2，第 3 人被拦）",
      got is not None and int(got["total"]) == 2 and v["reason"] == "full",
      f"{got and got['total']} / {v['reason']}")
check("S8-2 重启后 is_group/adapter 也没丢",
      got is not None and got.get("is_group") is True and got.get("adapter") == "qqo", "")

print(f"\n结果：{_PASS[0]} passed, {len(_FAIL)} failed")
if _FAIL:
    print("失败项：" + "；".join(_FAIL))
    sys.exit(1)
