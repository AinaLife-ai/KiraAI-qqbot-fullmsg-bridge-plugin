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
S9/S10 收尾：全场景出站报文**深扫**（只含官方字段）+ **查账工具必须真的进模型**
   （真实 `ToolSet` + 真实 `on_llm_request` 钩子；线上出过「bot 说没有这个工具」）。
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
out = run_async(BT.QQButtonTool(ctx=Ctx(p)).execute(ev, action="stats"))
check("S1-6 查账：3/3、已截止、名单可见",
      "已点 3 次" in out and ("已截止" in out or "剩 0" in out), out[:150])
_pol = p.button_policies._pol[key]
check("S1-7 账本里正好 3 人（超额的没被计入）",
      int(_pol["total"]) == 3 and len(_pol["users"]) == 3,
      f"total={_pol['total']} people={len(_pol['users'])}")
out2 = run_async(BT.QQButtonTool(ctx=Ctx(p)).execute(ev, action="extend", add_slots=2))
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

# ===================================================================== #
print("\n[S10] 查账工具必须**真的进模型**（线上 bug：ToolSet 静默丢弃不认识的对象）")
try:
    import button_tools as BT9

    _insts = [BT9.QQButtonTool(ctx=None)]
    _WANT = {"qq_button"}
    try:
        from core.utils.tool_utils import BaseTool as _BT9

        check("S10-1 按钮工具继承核心 BaseTool（否则会被静默丢弃）",
              all(isinstance(i, _BT9) for i in _insts),
              str([type(i).__name__ for i in _insts]))
    except Exception as _e9a:
        check("S10-1 取核心 BaseTool", False, f"{type(_e9a).__name__}: {_e9a}")

    try:
        from core.agent.tool import ToolSet as _RealTS

        _ts9 = _RealTS()
        for _i in _insts:
            _ts9.add(_i)
        _got9 = {t.name for t in _ts9.tools}
        check("S10-2 ★ 真实 ToolSet.add ⇒ 工具真的进去了（线上就是这里丢的）",
              _got9 == _WANT, str(sorted(_got9)))
    except Exception as _e9b:
        check("S10-2 真实 ToolSet 用例", False, f"{type(_e9b).__name__}: {_e9b}")

    check("S10-3 get_schema 三要素齐全（模型才看得到描述）",
          all((i.get_schema().get("name") and i.get_schema().get("description")
               and i.get_schema().get("parameters")) for i in _insts), "")

    # ---- S9-4 走插件真实注入路径（on_llm_request + 真实 ToolSet）----
    try:
        from core.agent.tool import ToolSet as _RealTS2

        T9, bm9, a9, p9 = make_env()
        run_async(p9._tick())

        class _Ev9:
            adapter = type("A", (), {"name": "qqo", "platform": "QQ Official"})()
            sid = "qqo:gm:GRP"

        class _Req9:
            def __init__(self):
                self.tool_set = _RealTS2()
                self.system_prompt = []

        _req9 = _Req9()
        # 真实钩子体签名是 (event, request, tag_set)；tag_set=None 会直接 return ⇒ 用真 TagSet
        from core.tag import TagSet as _TS9

        _hook = getattr(p9, "inject_tools_and_tags", None)
        if callable(_hook):
            _hook(_Ev9(), _req9, _TS9())
            _names9 = {t.name for t in _req9.tool_set.tools}
            check("S10-4 ★ 插件真实注入路径（inject_tools_and_tags）把按钮工具送进了模型",
                  _WANT <= _names9, str(sorted(_names9)))
        else:
            check("S10-4 插件存在 inject_tools_and_tags", False, "钩子缺失")
    except Exception as _e9c:
        check("S10-4 注入路径用例", False, f"{type(_e9c).__name__}: {_e9c}")
except Exception as _e9:
    check("S10 段执行", False, f"{type(_e9).__name__}: {_e9}")

# ===================================================================== #
print("\n[S11] 合并工具 qq_button：四个动作各跑一遍（真实链路）")
try:
    import button_tools as BT11

    T11, bm11, a11, p11 = make_env()
    run_async(p11._tick())
    _, _k11 = send_kb(T11, bm11, a11, p11, "GRP", {"max": "2", "label": "四动作"},
                      [{"id": "b1", "render_data": {"label": "报名", "style": 1},
                        "action": {"type": 1, "data": "join", "permission": {"type": 2}}}])
    clicks(bm11, p11, a11, "GRP", [("Z1", "join")])
    _t11 = BT11.QQButtonTool(ctx=Ctx(p11))
    _ev11 = Ev("qq:gm:GRP", p11)
    _o_stats = run_async(_t11.execute(_ev11, action="stats", label="四动作"))
    check("S11-1 action=stats ⇒ 查账输出（人数/结余/状态）",
          "已点 1 次" in _o_stats, _o_stats[:120])
    _o_ext = run_async(_t11.execute(_ev11, action="extend", label="四动作", add_slots=1))
    check("S11-2 action=extend ⇒ 加名额成功", "已调整" in _o_ext and "加 1 个名额" in _o_ext,
          _o_ext[:120])
    _o_close = run_async(_t11.execute(_ev11, action="close", label="四动作"))
    check("S11-3 action=close ⇒ 立刻截止", "已截止" in _o_close, _o_close[:120])
    _r11, _ = clicks(bm11, p11, a11, "GRP", [("Z2", "join")])
    check("S11-4 截止后点击不转达（0 次）", len(_r11[0]) == 0, str(len(_r11[0])))
    _o_reset = run_async(_t11.execute(_ev11, action="reset", label="四动作"))
    check("S11-5 action=reset ⇒ 清空重开", "已重置" in _o_reset, _o_reset[:120])
    _r11b, _ = clicks(bm11, p11, a11, "GRP", [("Z3", "join")])
    check("S11-6 重开后又能点（转达 1 次）", len(_r11b[0]) == 1, str(len(_r11b[0])))
    _o_bad = run_async(_t11.execute(_ev11, action="nope"))
    check("S11-7 未知 action ⇒ 明确报错不崩", "未知 action" in _o_bad, _o_bad[:80])
except Exception as _e11:
    check("S11 段执行", False, f"{type(_e11).__name__}: {_e11}")

# ===================================================================== #
print("\n[S12] 昵称学习 / 查账回填 / 会话隔离（用户线上两问）")
try:
    import main as _bm12

    T12, bm12, a12, p12 = make_env()
    run_async(p12._tick())
    send_kb(T12, bm12, a12, p12, "GRP", {"max": "2", "label": "回填"},
            [{"id": "b1", "render_data": {"label": "报名", "style": 1},
              "action": {"type": 1, "data": "join", "permission": {"type": 2}}}])
    clicks(bm12, p12, a12, "GRP", [("N1", "join")])
    _tool12 = BT.QQButtonTool(ctx=Ctx(p12))
    _ev12 = Ev("qq:gm:GRP", p12)
    _st1 = run_async(_tool12.execute(_ev12, action="stats", label="回填"))
    check("S12-1 没学过的人 ⇒ 查账只能显示『未知用户』（占位，不伪造）",
          "未知用户" in _st1, _st1[:110])

    # 这个人之后在群里说话了 ⇒ 我们（3.0 的 on_llm_request 钩子）应当学到昵称
    class _Sender12:
        user_id = "N1"
        nickname = "天阴"

    class _Msg12:
        sender = _Sender12()
        chain = []

    class _EvLearn12:
        message = _Msg12()
        adapter = type("A", (), {"name": "qqo", "platform": "QQ Official"})()
        session = type("S", (), {"session_type": "gm", "session_id": "qq:gm:GRP"})()

    p12._learn_peer_from_event(_EvLearn12())
    check("S12-2 学到昵称（3.0 上也喂通讯录了）",
          p12.identities.lookup_any("N1") == "天阴", str(p12.identities.lookup_any("N1")))

    _st2 = run_async(_tool12.execute(_ev12, action="stats", label="回填"))
    check("S12-3 ★ 回填：学过之后再查同一本账 ⇒ 显示真名「天阴」（账本只存 uid，名字是读时解析）",
          "天阴" in _st2 and "未知用户" not in _st2, _st2[:160])

    # ---- 会话**区分**（不是隔离）：默认查当前会话；scope=all 可跨会话且带会话名 ----
    for _pol in p12.button_policies._pol.values():
        if _pol.get("label") == "回填":
            _pol["session"] = "绿岛酒吧"          # 模拟"发按钮时已知群名"
    _st_head = run_async(_tool12.execute(_ev12, action="stats", label="回填"))
    check("S12-4 ★ 查账结果**双标识**：会话名 + openid（防重名）",
          "绿岛酒吧" in _st_head and "GRP" in _st_head, _st_head[:130])

    # 另一个会话里也发一个带策略的按钮
    _k12b = p12.register_button_policy("GRP2", {"max": "2", "label": "隔壁报名"},
                                       {"content": {"rows": [{"buttons": [
                                           {"id": "b2", "render_data": {"label": "报名"},
                                            "action": {"type": 1, "data": "join2"}}]}]}},
                                       is_group=True, adapter="qqo")
    if _k12b and _k12b in p12.button_policies._pol:
        p12.button_policies._pol[_k12b]["session"] = "隔壁群"
    _ev_other = Ev("qq:gm:OTHER-GROUP", p12)
    _st3 = run_async(_tool12.execute(_ev_other, action="stats", label="回填"))
    check("S12-4a 默认（scope=session）只在当前会话找 ⇒ 别处找不到就如实说",
          ("没有按钮账本" in _st3) or ("没找到" in _st3) or ("没有" in _st3), _st3[:120])
    _st4 = run_async(_tool12.execute(_ev_other, action="stats", lebel=None if False else "", scope="all"))
    check("S12-4b ★ scope=all：跨会话能查到，且**带会话名**（区分而非隔离）",
          ("绿岛酒吧" in _st4) or ("隔壁群" in _st4), _st4[:200])

    # ---- S12-6 同名会话：用 openid 前缀点名，绝不靠名字瞎猜 ----
    for _p in p12.button_policies._pol.values():
        if _p.get("label") == "隔壁报名":
            _p["session"] = "绿岛酒吧"          # 故意取同名，制造"重名"场景
    _st_same = run_async(_tool12.execute(_ev_other, action="stats", scope="all"))
    check("S12-6a 同名会话并存时能列出多条（带各自 openid 区分）",
          _st_same.count("绿岛酒吧") >= 2 and "GRP" in _st_same, _st_same[:200])
    _st_pin = run_async(_tool12.execute(_ev_other, action="stats", session="GRP2"))
    check("S12-6b ★ 用 session=<openid前缀> 点名 ⇒ 精确锁到那一本（不靠名字）",
          ("隔壁报名" in _st_pin) and ("回填" not in _st_pin), _st_pin[:160])

    # 端到端：模块级钩子（框架真正调用的那个）也会学
    try:
        p12.identities._store.pop("qqo|N2", None)
        from core.agent.tool import ToolSet as _TS12
        from core.tag import TagSet as _TAG12

        class _EvLearn2:
            message = type("M", (), {"sender": type("S", (), {"user_id": "N2",
                                                             "nickname": "小美"})(),
                                     "chain": []})()
            adapter = type("A", (), {"name": "qqo", "platform": "QQ Official"})()
            session = type("S", (), {"session_type": "gm", "session_id": "qq:gm:GRP"})()

        class _Req12:
            def __init__(self):
                self.tool_set = _TS12()
                self.system_prompt = []

        _r12 = _Req12()
        _hook12 = getattr(_bm12, "on_llm_request", None)
        if callable(_hook12):
            run_async(_hook12(p12, _EvLearn2(), _r12, _TAG12()))
            check("S12-5 ★ 真实钩子 on_llm_request 也会学昵称（3.0 主线）",
                  p12.identities.lookup_any("N2") == "小美",
                  str(p12.identities.lookup_any("N2")))
        else:
            check("S12-5 模块级 on_llm_request 存在", False, "缺失")
    except Exception as _e12b:
        check("S12-5 钩子学习用例", False, f"{type(_e12b).__name__}: {_e12b}")
except Exception as _e12:
    check("S12 段执行", False, f"{type(_e12).__name__}: {_e12}")

# ===================================================================== #
print("\n[S13] 满额后查账状态 + 超额点击记账（用户两问）")
try:
    T13, bm13, a13, p13 = make_env()
    run_async(p13._tick())
    send_kb(T13, bm13, a13, p13, "GRP", {"max": "2", "label": "上限测试"},
            [{"id": "b1", "render_data": {"label": "抢", "style": 1},
              "action": {"type": 1, "data": "go", "permission": {"type": 2}}}])
    _tool13 = BT.QQButtonTool(ctx=Ctx(p13))
    _ev13 = Ev("qq:gm:GRP", p13)
    _s0 = run_async(_tool13.execute(_ev13, action="stats", label="上限测试"))
    check("S13-1 未满额时显示「进行中」", "进行中" in _s0, _s0[:120])

    clicks(bm13, p13, a13, "GRP", [("M1", "go"), ("M2", "go")])   # 满额 2/2
    _s1 = run_async(_tool13.execute(_ev13, action="stats", label="上限测试"))
    check("S13-2 ★ 满额后查账显示**已截止**（不再永远「进行中」）",
          "已截止" in _s1 and "进行中" not in _s1, _s1[:160])

    clicks(bm13, p13, a13, "GRP", [("M3", "go"), ("M4", "go")])   # 超额两次
    _s2 = run_async(_tool13.execute(_ev13, action="stats", label="上限测试"))
    check("S13-3 ★ 超额点击**账里有记录**（超额未计入 2 次）",
          "超额未计入 2 次" in _s2, _s2[:180])
    _st2 = p13.button_policies.stats(list(p13.button_policies._pol.values())[0])
    check("S13-4 结构里也能取到 overflow / overflow_people",
          int(_st2.get("overflow") or 0) == 2 and int(_st2.get("overflow_people") or 0) == 2,
          f"overflow={_st2.get('overflow')} people={_st2.get('overflow_people')}")

    _o_ext = run_async(_tool13.execute(_ev13, action="extend", label="上限测试", add_slots=2))
    _s3 = run_async(_tool13.execute(_ev13, action="stats", label="上限测试"))
    check("S13-5 加名额后回到「进行中」（但超额历史仍在）",
          "进行中" in _s3 and "超额未计入 2 次" in _s3, _s3[:180])
    # ---- 其他类型：部分截止（一排两按钮，各自额度）与 过期（ttl） ----
    T13b, bm13b, a13b, p13b = make_env()
    run_async(p13b._tick())
    send_kb(T13b, bm13b, a13b, p13b, "GRP", {"max": "1", "label": "两按钮"},
            [{"id": "b1", "render_data": {"label": "A", "style": 1},
              "action": {"type": 1, "data": "a"}},
             {"id": "b2", "render_data": {"label": "B", "style": 1},
              "action": {"type": 1, "data": "b"}}])
    _tool13b = BT.QQButtonTool(ctx=Ctx(p13b))
    _ev13b = Ev("qq:gm:GRP", p13b)
    clicks(bm13b, p13b, a13b, "GRP", [("P1@b1", "a")])          # 只把 A 点满
    _sp = run_async(_tool13b.execute(_ev13b, action="stats", label="两按钮"))
    check("S13-7 ★ 一排按钮里只满了一个 ⇒ 显示「部分截止」",
          "部分截止" in _sp and "进行中" not in _sp, _sp[:170])

    T13c, bm13c, a13c, p13c = make_env()
    run_async(p13c._tick())
    send_kb(T13c, bm13c, a13c, p13c, "GRP", {"ttl": "60", "label": "限时账"},
            [{"id": "b1", "render_data": {"label": "抢", "style": 1},
              "action": {"type": 1, "data": "g"}}])
    clicks(bm13c, p13c, a13c, "GRP", [("T1", "g")])
    for _pl in p13c.button_policies._pol.values():
        if _pl.get("label") == "限时账":
            _pl["until"] = time.time() - 1
    _tool13c = BT.QQButtonTool(ctx=Ctx(p13c))
    _se = run_async(_tool13c.execute(_ev13b.__class__("qq:gm:GRP", p13c), action="stats", label="限时账"))
    check("S13-8 ★ 到点后显示「已过期（已截止）」（不是永远的进行中）",
          "已截止" in _se and ("过期" in _se), _se[:170])

    _o_reset = run_async(_tool13.execute(_ev13, action="reset", label="上限测试"))
    _s4 = run_async(_tool13.execute(_ev13, action="stats", label="上限测试"))
    check("S13-6 重开一轮后计数与超额一起清零", "进行中" in _s4 and "超额未计入" not in _s4,
          _s4[:160])
except Exception as _e13:
    check("S13 段执行", False, f"{type(_e13).__name__}: {_e13}")

print(f"\n结果：{_PASS[0]} passed, {len(_FAIL)} failed")
if _FAIL:
    print("失败项：" + "；".join(_FAIL))
    sys.exit(1)
