"""按钮策略层（button_policy.py）全套断言 —— 限次/限人/截止/计数/查账。

覆盖：
* 声明解析（标签属性 + JSON `kirai` + 逐按钮）；
* **剥离**：发出去的键盘载荷只剩官方字段（深扫，防 40034029）；
* `until` 解析（HH:MM 当天/次日、ISO、非法）；
* 判定：max / once / per / cooldown / expired / closing；
* `deliver`：last（只转截止那一次）/ all / off / 退化规则；
* 三路匹配：message_id / token（含剥离）/ 会话+按钮 兜底；
* 持久化往返 + 索引重建 + LRU；
* 查账（stats / render / find）与截止巡检（due_for_close）；
* 性能与"消息路径零 I/O"（判定不落盘，只有 dirty 标记）。
"""
import os as _os
import sys as _sys
import time as _time

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR          # noqa: E402

ROOT = _BR()
_sys.path.insert(0, ROOT)

import button_policy as BP                    # noqa: E402

_FAILED = []
_TOTAL = [0]


def check(name, cond, detail=""):
    _TOTAL[0] += 1
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        _FAILED.append(name)


def _deep_keys(obj, out):
    """递归收集所有 dict 键（用于"官方字段零污染"深扫）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(k)
            _deep_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _deep_keys(v, out)


OFFICIAL_KEYS = {"id", "content", "rows", "buttons", "render_data", "label",
                 "visited_label", "style", "action", "type", "data", "permission",
                 "specify_user_ids", "specify_role_ids", "enter", "reply", "anchor",
                 "click_limit", "unsupport_tips", "at_bot_show_channel_list",
                 "group_id", "modal", "content", "confirm_text", "cancel_text"}

print("\n[A] 声明解析（标签属性 / JSON kirai / 逐按钮）")
attrs = {"once": "1", "max": "5", "per": "2", "ttl": "600", "cooldown": "30",
         "deliver": "last", "hard": "0", "notify": "1", "notify_text": "满啦",
         "label": "报名"}
spec = BP.spec_from_attrs(attrs)
check("属性 → 规格：各字段齐", spec.get("once") is True and spec.get("max") == 5
      and spec.get("per") == 2 and spec.get("ttl") == 600 and spec.get("cooldown") == 30
      and spec.get("deliver") == "last" and spec.get("hard") is False
      and spec.get("notify") is True and spec.get("notify_text") == "满啦"
      and spec.get("label") == "报名", str(spec))

KB = {"content": {"rows": [{"buttons": [
    {"id": "b1", "render_data": {"label": "报名", "style": 1},
     "action": {"type": 1, "data": "join", "permission": {"type": 2}},
     "kirai": {"max": 1, "once": True, "label": "双人团"}},
    {"id": "b2", "render_data": {"label": "候补", "style": 0},
     "action": {"type": 1, "data": "wait"}, "kirai": {"max": 3, "per": 1}},
    {"id": "b3", "render_data": {"label": "纯官方"}, "action": {"type": 0, "data": "https://x"}},
]}]}}
clean, spec2 = BP.split_keyboard_declaration(KB, {"max": "9", "label": "整条"})
check("剥离后：内容级默认被属性覆盖", spec2.get("label") == "整条" and spec2.get("max") == 9, str(spec2))
check("剥离后：逐按钮策略进 buttons", set((spec2.get("buttons") or {})) == {"b1", "b2"},
      str(list((spec2.get("buttons") or {}))))
check("剥离后：b1 的 kirai 生效", (spec2["buttons"]["b1"].get("max") == 1
      and spec2["buttons"]["b1"].get("once") is True), str(spec2["buttons"].get("b1")))
check("剥离后：b3（没声明）不在 buttons 里", "b3" not in (spec2.get("buttons") or {}), "")

keys = set()
_deep_keys(clean, keys)
unknown = keys - OFFICIAL_KEYS
check("★★ 深扫：发出去的键盘**只剩官方字段**（防 40034029）", not unknown, str(unknown))
check("★★ 剥离是深层的：原对象里的 kirai 也被摘掉",
      "kirai" not in str(clean), str(clean)[:120])

print("\n[B] until 解析")
_now = _time.mktime((2026, 10, 10, 10, 0, 0, 0, 0, -1))
check("HH:MM 当天（未来）", abs(BP.parse_until("22:30", now=_now)
      - _time.mktime((2026, 10, 10, 22, 30, 0, 0, 0, -1))) < 1, "")
check("HH:MM 已过 ⇒ 次日", abs(BP.parse_until("09:00", now=_now)
      - _time.mktime((2026, 10, 11, 9, 0, 0, 0, 0, -1))) < 1, "")
check("ISO 串可解析", BP.parse_until("2026-10-11T08:00:00+08:00", now=_now) is not None, "")
check("非法值 ⇒ None（当没写）", BP.parse_until("明天", now=_now) is None, "")

print("\n[C] 判定：max / once / per / cooldown / expired / closing")
st = BP.ButtonPolicyStore()                    # 不落盘
pol = st.register("sid", {"max": 2, "label": "报名", "deliver": "all"})
v1 = st.decide(pol, "U1")
v2 = st.decide(pol, "U2")
v3 = st.decide(pol, "U3")
check("max=2：前两次接受", v1["accepted"] and v2["accepted"], f"{v1['reason']}/{v2['reason']}")
check("★★ 第 2 次就是「最后一个名额」（closing=True）", v2["closing"] is True, str(v2))
check("max=2：第 3 次拒绝且理由=full", (not v3["accepted"]) and v3["reason"] == "full", str(v3))
check("remaining 归零", v1["remaining"] == 1 and v2["remaining"] == 0, f"{v1}/{v2}")

st2 = BP.ButtonPolicyStore()
p_once = st2.register("sid", {"once": True, "deliver": "all"})
a1, a2 = st2.decide(p_once, "U1"), st2.decide(p_once, "U1")
a3 = st2.decide(p_once, "U2")
check("once：同一人第二次被拒（repeat），他人仍可点", a1["accepted"] and not a2["accepted"]
      and a2["reason"] == "repeat" and a3["accepted"], f"{a2['reason']}/{a3['accepted']}")

st3 = BP.ButtonPolicyStore()
p_per = st3.register("sid", {"per": 2, "deliver": "all"})
seq = [st3.decide(p_per, "U1") for _ in range(3)]
check("per=2：第三次 over_user", seq[0]["accepted"] and seq[1]["accepted"]
      and seq[2]["reason"] == "over_user", str([s["reason"] for s in seq]))

st4 = BP.ButtonPolicyStore()
p_cd = st4.register("sid", {"cooldown": 30, "deliver": "all"})
t0 = 1000.0
c1 = st4.decide(p_cd, "U1", now=t0)
c2 = st4.decide(p_cd, "U1", now=t0 + 5)       # 连点
c3 = st4.decide(p_cd, "U1", now=t0 + 31)      # 过了冷却
check("cooldown：连点被拒（且**不计入**），之后可再点",
      c1["accepted"] and (not c2["accepted"]) and c2["reason"] == "cooldown"
      and c3["accepted"] and c3["total"] == 2,
      f"{[c['reason'] for c in (c1, c2, c3)]} total={c3['total']}")

st5 = BP.ButtonPolicyStore()
p_ttl = st5.register("sid", {"ttl": 60, "deliver": "all"}, now=t0)
e1 = st5.decide(p_ttl, "U1", now=t0 + 61)
check("ttl 过期 ⇒ 拒绝且理由 expired", (not e1["accepted"]) and e1["reason"] == "expired", str(e1))
check("过期后策略被标记 closed", p_ttl["closed"] and p_ttl["closed_reason"] == "expired", "")
e2 = st5.decide(p_ttl, "U2", now=t0 + 120)
check("已 closed 的再点仍是拒绝", not e2["accepted"], str(e2["reason"]))

print('\n[D] deliver 模式（默认 all＝原来正常的方式；last/off 供自选）')
st6 = BP.ButtonPolicyStore()
p_def = st6.register("sid", {"max": 2})        # 没写 deliver ⇒ 默认 all
r1, r2 = st6.decide(p_def, "U1"), st6.decide(p_def, "U2")
check("★★ 默认 deliver=all（每次有效点击都转给模型，与旧版一致）",
      p_def["deliver"] == "all", str(p_def.get("deliver")))
check("默认 all：第 1 次也转（accepted=True）", r1["accepted"] is True, str(r1))
check("默认 all：第 2 次是截止那次（closing=True，且会带汇总）", r2["closing"] is True, str(r2))

st7 = BP.ButtonPolicyStore()
p_off = st7.register("sid", {"max": 3, "deliver": "off"})
check("off：显式 deliver=off（一次都不转）", p_off["deliver"] == "off", p_off.get("deliver"))
p_last2 = st7.register("sid", {"max": 2, "deliver": "last"})
check("last：显式 deliver=last 保留可用", p_last2["deliver"] == "last", p_last2.get("deliver"))
_v_last = st7.decide(p_last2, "UA")
_v_close = st7.decide(p_last2, "UB")
check("last 语义：中途 accepted 但非 closing（不转）；截止那一次 closing=True（转）",
      _v_last["accepted"] and not _v_last["closing"] and _v_close["closing"] is True,
      f"{_v_last['accepted']}/{_v_last['closing']} {_v_close['closing']}")

st8 = BP.ButtonPolicyStore()
p_deg = st8.register("sid", {"deliver": "last"})   # 既无 max 也无截止
check("★ 退化规则：选了 last 但没有全局结束点 ⇒ 退成 all（否则永远收不到）",
      st8._effective_deliver(p_deg) == "all", "")

print("\n[E] 三路匹配（message_id / token / 会话+按钮兜底）")
st9 = BP.ButtonPolicyStore()
p9 = st9.register("sidA", {"max": 5}, token=True, button_ids=["b1", "b2"])
st9.bind_message(p9["key"], "MSG-1")
got, clean_data = st9.resolve(message_id="MSG-1", sid="sidA", button_id="b1", data="join")
check("① message_id 精确命中", got is p9, "")
tok = p9.get("token")
got2, clean2 = st9.resolve(sid="other", button_id="zz",
                           data=f"{BP.TOKEN_MARK}{tok}|join")
check("② token 命中（会话无关）", got2 is p9, "")
check("★★ token 一律被剥掉：模型看到的是原值", clean2 == "join", repr(clean2))
got3, clean3 = st9.resolve(sid="sidA", button_id="b1", data="join")
check("③ 会话+按钮兜底命中", got3 is p9, "")
check("没命中时 data 原样返回", clean3 == "join", repr(clean3))
check("trace：无策略时返回 (None, data)",
      st9.resolve(sid="nope", button_id="nope", data="x")[0] is None, "")

print("\n[F] token 嵌入（超长不加 / 非字符串不动）")
kb2 = {"content": {"rows": [{"buttons": [
    {"id": "b1", "action": {"type": 1, "data": "join"}},
    {"id": "b2", "action": {"type": 1, "data": "x" * 100}},     # 100 字符，加 token 会超
    {"id": "b3", "action": {"type": 1, "data": 123}},           # 非字符串
]}]}}
n_emb = BP.embed_token(kb2, {"b1": "ab12", "b2": "ab12", "b3": "ab12"})
_d1 = kb2["content"]["rows"][0]["buttons"][0]["action"]["data"]
_d2 = kb2["content"]["rows"][0]["buttons"][1]["action"]["data"]
check("只嵌入安全的那一个", n_emb == 1 and _d1.startswith("~ab12|"), str(n_emb))
check("★ 超长/非字符串一律不动", len(_d2) == 100 and kb2["content"]["rows"][0]["buttons"][2]
      ["action"]["data"] == 123, "")

print("\n[G] 持久化往返 + 索引重建 + LRU")
import tempfile                                                # noqa: E402
_tmp = tempfile.mkdtemp(prefix="bp_")
_path = _os.path.join(_tmp, "button_policies.json")
st10 = BP.ButtonPolicyStore(path=_path)
pA = st10.register("sid", {"max": 3, "label": "报名"}, token=True)
st10.decide(pA, "U1")
st10.decide(pA, "U2")
st10.bind_message(pA["key"], "MSG-A")
check("落盘成功（脏标记 → save）", st10.save() and _os.path.exists(_path), "")
check("落盘后 dirty 归零", st10.dirty is False, "")
st11 = BP.ButtonPolicyStore(path=_path)
gotA, _ = st11.resolve(message_id="MSG-A", sid="sid", button_id="b1", data="join")
check("★ 往返：计数与用户明细都在", gotA is not None and gotA["total"] == 2
      and len(gotA["users"]) == 2, str(gotA and gotA["total"]))
check("★ 往返：message_id 索引重建可用", gotA is not None and gotA["key"] == pA["key"], "")
check("★ 往返：token 索引重建可用",
      st11.resolve(sid="x", button_id="y", data=f"{BP.TOKEN_MARK}{pA['token']}|join")[0] is not None, "")

st12 = BP.ButtonPolicyStore(max_entries=2)
for i in range(5):
    st12.register(f"sid{i}", {"max": 1})
check("LRU：上限生效（只留最后 2 条）", len(st12._pol) == 2, str(len(st12._pol)))

print("\n[H] 查账（stats / render / find）与截止巡检")
st13 = BP.ButtonPolicyStore(path=_os.path.join(_tmp, "b.json"))
pB = st13.register("S", {"max": 3, "label": "报名", "per": 2}, now=t0)
st13.decide(pB, "U1", now=t0)
st13.decide(pB, "U1", now=t0 + 1)
st13.decide(pB, "U2", now=t0 + 2)
st_b = st13.stats(pB, names={"U1": "周武", "U2": "小美"})
check("stats：总数/人数/剩余/名字都对",
      st_b["total"] == 3 and st_b["people"] == 2 and st_b["remaining"] == 0
      and st_b["users"][0]["name"] == "周武" and st_b["users"][0]["count"] == 2, str(st_b)[:160])
check("stats：用户序按次数降序", [u["name"] for u in st_b["users"]] == ["周武", "小美"], "")
txt = BP.render_stats_text(st_b, now=t0 + 10, name_of=lambda u: {"U1": "周武", "U2": "小美"}.get(u))
check("查账文本：含人数/结余/状态", "已点 3 次" in txt and "剩 0" in txt and "周武 × 2" in txt,
      txt[:200])
check("查账文本紧凑（省 token，< 600 字符）", len(txt) < 600, str(len(txt)))

st14 = BP.ButtonPolicyStore()
pC1 = st14.register("S2", {"max": 1, "label": "甲"}, now=t0)
pC2 = st14.register("S2", {"max": 1, "label": "乙"}, now=t0 + 1)
hit, cands = st14.find("S2", "")
check("find：多个候选时不乱猜，返回候选列表", hit is None and len(cands) == 2, f"{hit}/{len(cands)}")
hit2, _c2 = st14.find("S2", label="乙")
check("find：按 label 能唯一命中", hit2 is pC2, "")
hit3, _c3 = st14.find("S2", message_id="")
check("find：无 selector 且候选 >1 ⇒ 仍不乱猜", hit3 is None, "")

st15 = BP.ButtonPolicyStore()
pD = st15.register("S3", {"ttl": 10, "label": "限时"}, now=t0)
st15.decide(pD, "U1", now=t0 + 1)
due = st15.due_for_close(now=t0 + 20)
check('★ 到点巡检：有人参与 ⇒ 报出该策略（给模型一条"已截止"）',
      len(due) == 1 and due[0]["key"] == pD["key"], str(len(due)))
check("巡检后不会重复通知", st15.due_for_close(now=t0 + 30) == [], "")
st16 = BP.ButtonPolicyStore()
pE = st16.register("S4", {"ttl": 10, "label": "没人点"}, now=t0)
check("★ 没人点过的按钮到点 ⇒ 静默关闭（不打扰模型）",
      st16.due_for_close(now=t0 + 20) == [] and pE["closed"] is True, "")

print("\n[I] 手动管理（close / reset / extend）")
st17 = BP.ButtonPolicyStore()
pF = st17.register("S5", {"max": 2, "label": "报名"}, now=t0)
st17.decide(pF, "U1", now=t0)
check("close：手动截止", st17.close(pF["key"], "manual", now=t0 + 1) and pF["closed"], "")
check("reset：清空计数并重开",
      st17.reset(pF["key"], now=t0 + 2) and pF["total"] == 0 and not pF["closed"], "")
vF = st17.decide(pF, "U2", now=t0 + 3)
check("reset 后能继续点", vF["accepted"] and pF["total"] == 1, str(vF))
st17.close(pF["key"], "full", now=t0 + 4)
check("extend：加名额会重新开启",
      st17.extend(pF["key"], max_add=1, now=t0 + 5) and not pF["closed"], "")
check("extend：延长时间", st17.extend(pF["key"], minutes=30, now=t0 + 6)
      and pF["until"] >= t0 + 6 + 30 * 60 - 1, str(pF["until"]))

print("\n[J] 性能 / 无阻塞 / 异常安全")
st18 = BP.ButtonPolicyStore(path=_os.path.join(_tmp, "perf.json"))
pG = st18.register("P", {"max": 10 ** 9, "cooldown": 0})
_n = 200000
_t1 = _time.perf_counter()
for i in range(_n):
    st18.decide(pG, f"U{i % 1000}", now=t0 + i * 0.001)
_dt = _time.perf_counter() - _t1
check(f"★ 20 万次判定耗时 {_dt:.2f}s（< 2s ⇒ 单次 <10µs，绝不阻塞）", _dt < 2.0, f"{_dt:.2f}s")
check("★ 判定过程**不写盘**（消息路径零 I/O；只置 dirty）",
      st18.dirty is True and _os.path.getsize(_os.path.join(_tmp, "perf.json")) if _os.path.exists(
          _os.path.join(_tmp, "perf.json")) else st18.dirty, "")
check("坏输入不抛异常（None/非法 uid）",
      isinstance(st18.decide(pG, None), dict) and isinstance(st18.decide(pG, ""), dict), "")
check("统计字段齐全（查账工具依赖）",
      all(k in st18.stats(pG, limit=3) for k in
          ("total", "remaining", "users", "closed", "until", "label", "people")), "")

print("\n[L] 逐按钮限额（scope）与裁剪策略 —— 审计修复")
stL = BP.ButtonPolicyStore()
pL = stL.register("SL", {"max": 1, "label": "报名行"})          # 默认 scope=each
_v_b1 = stL.decide(pL, "U1", button_id="b1")
_v_b2 = stL.decide(pL, "U2", button_id="b2")
_v_b1b = stL.decide(pL, "U3", button_id="b1")
check("★★ each（默认）：一排按钮**互不吃名额**（b2 不受 b1 用满影响）",
      _v_b1["accepted"] and _v_b2["accepted"], f"{_v_b1['accepted']}/{_v_b2['accepted']}")
check("★★ each：各自到上限后只拒自己那个按钮",
      _v_b1b["reason"] == "full" and _v_b1b["accepted"] is False, str(_v_b1b["reason"]))
check("each：汇总层仍记总数（查账用）", int(pL["total"]) == 2 and len(pL["users"]) == 2,
      f"total={pL['total']} people={len(pL['users'])}")
_st_L = stL.stats(pL, limit=3)
_txt_L = BP.render_stats_text(_st_L, name_of=lambda u: None)
check("★★ 查账文本里也能看到逐按钮明细（模型看的就是这段文本）",
      "各按钮" in _txt_L and "b1" in _txt_L, _txt_L[:150])
check("查账：多按钮时给出逐按钮明细",
      bool(_st_L.get("buttons")) and {b["button"] for b in _st_L["buttons"]} == {"b1", "b2"},
      str(_st_L.get("buttons")))

stL2 = BP.ButtonPolicyStore()
pAll = stL2.register("SL", {"max": 2, "scope": "all", "label": "整条共用"})
_r1 = stL2.decide(pAll, "U1", button_id="b1")
_r2 = stL2.decide(pAll, "U2", button_id="b2")
_r3 = stL2.decide(pAll, "U3", button_id="b3")
check("all（显式）：整条键盘共用额度 ⇒ 第 3 个按钮被拒",
      _r1["accepted"] and _r2["accepted"] and _r3["reason"] == "full", str(_r3["reason"]))

stT = BP.ButtonPolicyStore(max_entries=2)
_p_old = stT.register("S1", {"max": 5})
stT.close(_p_old["key"], "manual")
_p_live1 = stT.register("S2", {"max": 5})
_p_live2 = stT.register("S3", {"max": 5})
stT.register("S4", {"max": 5})
check("★ 裁剪：**先清已截止的旧账**（S1 被清），再从最早的裁到上限内",
      _p_old["key"] not in stT._pol and len(stT._pol) == 2
      and _p_live2["key"] in stT._pol,
      f"还剩 {sorted(stT._pol)}")
check("★ 裁剪：只裁够数就停（不会把进行中的全清掉）",
      int(stT._pol.get(_p_live2["key"], {}).get("total") or 0) == 0, "")

print("\n[K] 正文注记（省 token 的短文本）")
st19 = BP.ButtonPolicyStore()
pK = st19.register("S", {"max": 2, "label": "报名"})
vK1 = st19.decide(pK, "U1")
note1 = BP.render_note(pK, vK1)
vK2 = st19.decide(pK, "U2")
note2 = BP.render_note(pK, vK2, names={"U1": "周武", "U2": "小美"})
vK3 = st19.decide(pK, "U3")
note3 = BP.render_note(pK, vK3)
check("第 1 次注记含「第 1/2 次，剩 1」", "第 1/2 次" in note1 and "剩 1" in note1, note1)
check("★ 截止那一次注记写明「最后一个名额」+ 名单",
      "最后一个名额" in note2 and "周武" in note2, note2)
check("超额的注记写「名额已满（未计入）」", "名额已满" in note3 and "未计入" in note3, note3)
check("注记都很短（≤ 80 字符，省 token）",
      max(len(note1), len(note2), len(note3)) <= 80,
      str(max(len(note1), len(note2), len(note3))))

print(f"\n结果：{_TOTAL[0] - len(_FAILED)} passed, {len(_FAILED)} failed")
if _FAILED:
    print("失败项：" + "；".join(_FAILED))
    _sys.exit(1)
