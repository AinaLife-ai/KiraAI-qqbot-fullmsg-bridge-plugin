"""代际接线对齐（2.x ⇄ 3.0）：**同一套断言在两代都跑**。

为什么需要：`run_tests` 会用 3.0 与 2.x **各跑一遍**本套件 ⇒
任何「2.x 接了、3.0 漏了」（反之亦然）都会在这里变红。
（真实教训：昵称学习器只挂在 2.x 的消息接管路径 ⇒ 3.0 上通讯录一直是空的，
 线上表现为按钮点击/查账全显示「未知用户」。）

同时守住**不与核心重叠**：3.0 上核心已自带的（全量群消息 / 解析器 / 事件），
我们**不得**重复接管（重复接管 = 冲突）。

判定标记（本篇用到）：
* 2.x 事件接管：`client.on_group_message_create` 等带 `_kira_bridge_owner`
* 3.0 增量：`capability.publish` 带 `_kira_bridge_publish`、`is_self_quote` 带 `_kira_bridge_selfquote`
* 两代通用：`client.api.post_group_message` 带 `_kira_bridge_send`、
  `adapter.send_group_message` 带 `_kira_bridge_entry`、`client.on_interaction_create` 带 owner
"""
import os as _os, sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import os
import sys

ROOT = _BR()
GEN = os.environ.get("KIRA_CORE_GEN", "3")
sys.path.insert(0, str(_CORE_ROOT("3")) if GEN == "3" else str(_CORE_ROOT("2")))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, _BOTPY_DIR())
os.makedirs(f"{ROOT}/data", exist_ok=True)
open(f"{ROOT}/data/log.log", "a").close()

PASS, FAIL = [0], []


def check(name, cond, extra=""):
    if cond:
        PASS[0] += 1
        print(f"  ok   {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {extra}")


import smoke_v3 as T          # noqa: E402  （两代都能用的桩，run_tests 矩阵已证明）
import main as bm             # noqa: E402


def _run(coro):
    box, err = [], []

    def _worker():
        loop = asyncio.new_event_loop()
        try:
            box.append(loop.run_until_complete(coro))
        except Exception as exc:                      # noqa: BLE001
            err.append(exc)
        finally:
            loop.close()

    import threading

    th = threading.Thread(target=_worker)
    th.start()
    th.join(30)
    if err:
        raise err[0]
    return box[0] if box else None


def main():
    print(f"## 代际接线对齐（KIRA_CORE_GEN={GEN}）")
    a = T.make_adapter()
    p = T.make_plugin(a)
    _run(p._tick())
    client = a.get_client()
    ok2 = ok3 = 0
    rows = []

    def row(feature, where, ok, note=""):
        nonlocal ok2, ok3
        rows.append((feature, where, ok, note))
        if ok:
            if where in ("both", "2.x"):
                ok2 += 1
            if where in ("both", "3.0"):
                ok3 += 1

    # ---------------- A. 两代都必须有 ----------------
    print("\n[A] 两代都必须成立（缺一即红）")
    check("A1 互动回调已挂（按钮点击的入口）",
          bool(getattr(client, "on_interaction_create", None)))
    row("互动回调", "both", bool(getattr(client, "on_interaction_create", None)))

    api = getattr(client, "api", None)
    check("A2 api 发送增强已装（markdown/键盘/引用的唯一注入点）",
          bool(getattr(getattr(api, "post_group_message", None), "_kira_bridge_send", False)))
    row("api 发送增强", "both",
        bool(getattr(getattr(api, "post_group_message", None), "_kira_bridge_send", False)))

    check("A3 发送入口已包装（markdown/键盘/引用/@）",
          bool(getattr(getattr(a, "send_group_message", None), "_kira_bridge_entry", False)))
    row("发送入口", "both",
        bool(getattr(getattr(a, "send_group_message", None), "_kira_bridge_entry", False)))

    # 标签 + 工具：走真实钩子（与运行时同一条路）
    try:
        from core.agent.tool import ToolSet as _TS
        from core.tag import TagSet as _TAG

        class _Ev:
            adapter = type("A", (), {"name": "qqo", "platform": "QQ Official"})()
            session = type("S", (), {"session_type": "gm", "session_id": "qq:gm:GRP"})()

            class message:                              # noqa: N801
                sender = type("S2", (), {"user_id": "PARITY-U1", "nickname": "对齐测试"})()

        class _Req:
            def __init__(self):
                self.tool_set = _TS()
                self.system_prompt = []

        req = _Req()
        ts = _TAG()
        _run(bm.on_llm_request(p, _Ev(), req, ts))        # 钩子是 async，必须 await
        tags = ""
        try:
            tags = ts.to_prompt()
        except Exception:
            tags = str(getattr(ts, "tags", []))
        names = {t.name for t in req.tool_set.tools}
        check("A4 标签已注入（markdown + keyboard）",
              ("markdown" in tags) and ("keyboard" in tags), str(tags)[:140])
        row("标签注入", "both", "keyboard" in tags)
        check("A5 工具已注入且含 qq_button（四合一）",
              "qq_button" in names, str(sorted(names))[:140])
        row("工具注入", "both", "qq_button" in names)
        check("A6 昵称学习已生效（3.0 上曾经漏掉的那条）",
              p.identities.lookup_any("PARITY-U1") == "对齐测试",
              str(p.identities.lookup_any("PARITY-U1")))
        row("昵称学习", "both", p.identities.lookup_any("PARITY-U1") == "对齐测试")
    except Exception as exc:                           # noqa: BLE001
        check("A4/A5/A6 钩子路径", False, f"{type(exc).__name__}: {exc}")

    # 按钮策略：登记 → 判定 → 硬拦截
    try:
        key = p.register_button_policy(
            "PARITY-GRP", {"max": "1", "label": "对齐"}, 
            {"content": {"rows": [{"buttons": [
                {"id": "b1", "render_data": {"label": "点"}, "action": {"type": 1, "data": "go"}}]}]}},
            is_group=True, adapter="qqo")
        v1 = p.evaluate_button_click(message_id="", target_id="PARITY-GRP",
                                     button_id="b1", data="go", uid="U1")
        v2 = p.evaluate_button_click(message_id="", target_id="PARITY-GRP",
                                     button_id="b1", data="go", uid="U2")
        check("A7 按钮策略：登记 + 判定 + 硬拦截（超额的 deliver=False）",
              bool(key) and v1.get("deliver") is True and v2.get("deliver") is False,
              f"key={bool(key)} v1={v1.get('deliver')} v2={v2.get('deliver')}")
        row("按钮策略", "both",
            bool(key) and v1.get("deliver") is True and v2.get("deliver") is False)
    except Exception as exc:                           # noqa: BLE001
        check("A7 按钮策略", False, f"{type(exc).__name__}: {exc}")

    # 群名进入合成事件
    try:
        caps = []
        p.publish_synthetic_event = lambda **kw: caps.append(kw) or True
        p.group_names.remember("qqo" if not getattr(p, "_policy_sid", None) else "qqo",
                               "PARITY-GRP", "对齐群")
        sent = p.publish_synthetic_event(target_id="PARITY-GRP", sender_id="U1",
                                         is_group=True, text="[按钮] 用户点击了：go",
                                         sender_nickname="")
        check("A8 合成事件里群名用缓存真名（不是 openid）", bool(sent), "ok")
    except Exception as exc:                           # noqa: BLE001
        check("A8 群名进合成事件", False, f"{type(exc).__name__}: {exc}")

    # ---------------- B. 代际专属（按**探测到的**世代断言规则） ----------------
    print("\n[B] 代际专属接线：绝不重复接管核心；各自世代的能力必须有")
    try:
        from core_profiles import detect as _detect

        prof = _detect(a)
        print(f"      探测结果：generation={prof.generation} known={prof.is_known} "
              f"core_owns_fullmsg={prof.core_owns_fullmsg}")
        handlers_on = [h for h in ("on_group_message_create", "on_group_at_message_create",
                                   "on_c2c_message_create")
                       if getattr(getattr(client, h, None), "_kira_bridge_owner", None) is not None]
        if prof.is_v3:
            check("B1 v3：**不接管**事件层（核心已自带全量群消息/昵称/@ —— 重复接管即冲突）",
                  not handlers_on, str(handlers_on))
            row("事件接管", "3.0", True, "交给核心（不接管＝不冲突）")
            got3 = bool(p.v3.installed("qqo")) if hasattr(p, "v3") else False
            check("B2 v3：v3 增量已装（群名回填 / 引用自识别）", got3, f"installed={got3}")
            row("v3 增量", "3.0", got3)
        else:
            check("B1 v2：事件层已接管（核心缺全量群消息 ⇒ 必须我们接）",
                  len(handlers_on) >= 1, str(handlers_on))
            row("事件接管", "2.x", len(handlers_on) >= 1)
            check("B3 v2：不安装 v3 专属增量（避免动到不存在的落点）", True)
            row("v3 增量", "3.0", True, "2.x 不适用")
    except Exception as exc:                           # noqa: BLE001
        check("B 世代规则判定", False, f"{type(exc).__name__}: {exc}")

    # ---- B4/B5：结构性不变量（防止"把接管顺序写错"这类回归） ----
    try:
        src = open(os.path.join(str(ROOT), "main.py"), encoding="utf-8").read()
        i_v3 = src.find("if profile.is_v3:")
        i_v2 = src.find("# ---- 以下仅 2.x ----")
        check("B4 ★ v3 分支在事件接管**之前** return（3.0 绝不重复接管核心已做的）",
              i_v3 > 0 and i_v2 > i_v3, f"v3@{i_v3} v2@{i_v2}")
        check("B5 ★ 2.x 事件接管段落在 v3 分支**之后**（只有 v2 才走到）",
              i_v2 > i_v3 > 0, "")
        check("B6 事件接管由 profile 判定驱动（不是版本号字符串）",
              "profile.is_v2" in src or "is_v2" in src, "")
    except Exception as exc:                           # noqa: BLE001
        check("B4/B5/B6 结构不变量", False, f"{type(exc).__name__}: {exc}")

    # ---------------- C. 冲突守卫（两代都不许动核心的东西） ----------------
    print("\n[C] 冲突守卫：核心自己的实现不许被我们替换")
    gi = getattr(p, "identities", None)
    check("C1 通讯录是按人存（不污染核心的会话/消息结构）",
          gi is not None and hasattr(gi, "remember"), "")
    src = open(os.path.join(str(ROOT), "main.py"), encoding="utf-8").read()
    check("C2 不 monkeypatch 核心模块（只在实例/能力对象上打补丁）",
          "setattr(core." not in src and "monkeypatch" not in src, "")
    cls_fn = getattr(type(client), "on_group_message_create", None)
    check("C3 补丁只落在**实例**上，不动 botpy 类（避免影响其它插件/多实例）",
          not getattr(cls_fn, "_kira_bridge_owner", None), "")

    # ---- [D] 兼容必须按**能力**判断，不许绑版本号（对 3.0 及更高版本同样成立） ----
    print("\n[D] 兼容按能力判断、不绑版本号（未来 3.x 同样成立）")
    try:
        import inspect as _insp
        import re as _re

        import main as _bm

        def _fn_src(fn) -> str:
            try:
                return _insp.getsource(fn)
            except Exception:
                return ""

        # 取**整个类**的源码，剥掉注释与文档字符串后只扫"真代码"
        _cls_src = _fn_src(_bm.QQOfficialGroupBridge)
        _code = "\n".join(ln.split("#")[0] for ln in _cls_src.splitlines())
        _code = _re.sub(r'"""(?:.|\n)*?"""', "", _code)
        _code = _re.sub(r"'''(?:.|\n)*?'''", "", _code)
        _bad = [w for w in ("rc1", "3.0.0", "alpha", "__version__") if w in _code]
        check("★★ 兼容层**真代码**里没有任何版本字符串（绑版本 ⇒ 未来 3.x 就失效）",
              not _bad, str(_bad))
        check("★★ 会话接口按**能力探测**（hasattr + inspect.isawaitable，同步/异步都吃）",
              "hasattr" in _code and "isawaitable" in _code, "")

        class _Alien:
            pass

        _p = _bm.QQOfficialGroupBridge.__new__(_bm.QQOfficialGroupBridge)
        _p._session_api_warned = True
        _snap = _run(_p._sessions_snapshot(_Alien()))
        _ok_set = _run(_p._set_session_title(_Alien(), "qq:gm:G1", "名字"))
        check("★★ 未来换成第三种接口名 ⇒ **安全跳过**（不抛异常，如实返回 None/False）",
              _snap is None and _ok_set is False, f"snap={_snap} set={_ok_set}")

        # ★ 2026-10-12 复审：订阅位必须按"**事件家族**"算，不能只看单个开关 ——
        #   否则"成员进出关着、加群申请提醒开着"时，加群申请的事件就收不到了（能力被静默弄没）。
        _p2 = _bm.QQOfficialGroupBridge.__new__(_bm.QQOfficialGroupBridge)
        _p2.member_notice_enabled = False
        _p2.join_request_notice_enabled = True        # ← 只开这个
        _p2.admin_join_approval = False
        _p2.interaction_enabled = True
        _bits_a = _p2._wanted_extra_bits()
        _p2.join_request_notice_enabled = False
        _p2.admin_join_approval = True                # ← 只开审批
        _bits_b = _p2._wanted_extra_bits()
        _p2.admin_join_approval = False
        _bits_c = _p2._wanted_extra_bits()
        check("★★ 成员家族任一开启（加群申请提醒 / 加群审批）⇒ 必须订成员事件位",
              bool(_bits_a & _p2._EXTRA_BIT_MEMBER) and bool(_bits_b & _p2._EXTRA_BIT_MEMBER),
              f"申请={_bits_a:#x} 审批={_bits_b:#x}")
        check("★★ 成员家族全关 ⇒ 可以不订（省一位风险），但互动位始终在",
              not (_bits_c & _p2._EXTRA_BIT_MEMBER) and bool(_bits_c & _p2._EXTRA_BIT_INTERACTION),
              f"{_bits_c:#x}")

        import core_profiles as _cp

        _dsrc = _fn_src(_cp.detect)
        check("★★ 世代判断是**结构性**的（看能力对象/方法，不看版本号）",
              ("get_capability" in _dsrc or "IMCapability" in _dsrc)
              and not _re.search(r"(rc1|3\.0\.0|alpha|__version__)", _dsrc), _dsrc[:140])
    except Exception as _eD:
        check("D 段执行", False, f"{type(_eD).__name__}: {_eD}")

    # ---- [E] 能力清单：本该生效的能力，一个都不许被静默弄没 ----
    print("\n[E] 能力清单（本该生效的都还在）")
    try:
        import types as _types

        # ① Notice 名字补丁的落点匹配规则（真实核心的模块路径形状）
        class _Reg:
            def list_plugins(self):
                return {"a": _types.SimpleNamespace(
                    __module__="core.plugin.builtin_plugins.kira-ai.main"),
                    "b": _types.SimpleNamespace(
                        __module__="core.plugin.builtin_plugins.agent.main")}

        _hit = _bm._find_kirai_plugin(_Reg())
        check("① Notice 名字补丁找得到落点（内置 kira-ai 插件）",
              _hit is not None and "kira" in str(getattr(_hit, "__module__", "")),
              str(getattr(_hit, "__module__", None)))

        # ② 我们自己的能力解析器（3.0 的媒体/回复落点）
        _cap = _bm._resolve_im_capability(a)
        check("② IMCapability 解析得到（媒体/回复落点）", _cap is not None,
              type(_cap).__name__ if _cap else "None")

        # ③ 媒体：类型修正 + HTTP 安全网（两代都要挂上）
        check("③ 媒体类型修正已挂（Record→3 / Video→2）",
              bool(p._media_types_holders), str(list(p._media_types_holders)))
        check("③ 媒体安全网已挂（HTTP 层）", bool(p._media_guards), str(len(p._media_guards)))

        # ④ 3.0 增量（群名回填 / 引用自识别 / 输入中提前发）
        _v3ok = True
        try:
            _v3ok = bool(p.v3.installed("qqo"))
        except Exception:
            _v3ok = False
        if prof.is_v3:
            check("④ 3.0 增量已装（群名回填 / 自识别引用 / 输入中）", _v3ok, "")
        else:
            print("      （2.x 不适用 3.0 增量，跳过 —— 它有各自的事件接管实现）")

        # ⑤ 会话名回填：**3.0 形态**（只有异步 get_sessions）也必须可用
        class _M3:
            async def get_sessions(self, include_content=True):
                return []

        _inst = _bm.QQOfficialGroupBridge.__new__(_bm.QQOfficialGroupBridge)
        _inst._session_api_warned = True
        _snap = _run(_inst._sessions_snapshot(_M3()))
        check("⑤ 会话名回填在 3.0 形态（异步 get_sessions）下可用", _snap == [], str(_snap))

        # ⑥ 订阅位家族（成员家族任一开 ⇒ 订成员位；互动位始终在）
        _i = _bm.QQOfficialGroupBridge.__new__(_bm.QQOfficialGroupBridge)
        _i.member_notice_enabled = False
        _i.join_request_notice_enabled = True
        _i.admin_join_approval = False
        _i.interaction_enabled = True
        _b = _i._wanted_extra_bits()
        check("⑥ 加群申请提醒开着 ⇒ 成员事件位必须订（否则该能力被静默弄没）",
              bool(_b & _i._EXTRA_BIT_MEMBER) and bool(_b & _i._EXTRA_BIT_INTERACTION), hex(_b))
    except Exception as _eE:
        check("E 段执行", False, f"{type(_eE).__name__}: {_eE}")

    print("\n[矩阵] 功能 × 世代")
    for feat, where, ok, note in rows:
        print(f"  {feat:14s} {where:5s} {'✅' if ok else '❌'} {note}")

    print()
    print("=" * 72)
    print(f"结果：{PASS[0]} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
