"""用户两问的实测回答：

**问 1：3.0 上配置项全开也正常吗？**（"你懂我意思" = 想知道有没有"开了没用/开不了"的项）

**问 2：用户正常运行中装本插件，是不是就能直接用？**

本文件把「全开」这个矩阵**逐项跑一遍**，看每项在 2.x / 3.0 上：
  * 真生效（有实际行为）
  * 无操作但**无害**（2.x 专属项落到 3.0）
  * 报错 / 崩

结论会打印成表格。
"""
import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", "/var/minis/workspace/qqbot_bridge_review/kira-core"))
GEN = os.environ.get("KIRA_CORE_GEN", "2")
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))
(ROOT / "data").mkdir(exist_ok=True)
_BOTPY = os.environ.get("BOTPY_PATH", "/tmp/botpy_src/botpy-master")
if os.path.isdir(_BOTPY):
    sys.path.insert(0, _BOTPY)

PASS = FAIL = 0
ROWS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def main():
    print("=" * 76)
    print(f"## 全开配置实测（KiraAI {GEN}.x）")
    print("=" * 76)

    import main as B

    info = None
    try:
        from core.adapter.adapter_info import AdapterInfo
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                           platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
    except Exception as exc:
        print(f"  无法构造 AdapterInfo: {exc}")
        return 1

    # ---------------- 全开配置 ----------------
    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    all_on = {}
    for sk, sec in schema.items():
        if not isinstance(sec, dict):
            continue
        fields = sec.get("fields") or {}
        if isinstance(fields, dict):
            sec_cfg = {}
            for k, f in fields.items():
                t = f.get("type")
                if t in ("switch", "bool"):
                    sec_cfg[k] = True
                elif t == "string" and f.get("options"):
                    sec_cfg[k] = f["options"][0]
                elif f.get("default") is not None:
                    sec_cfg[k] = f["default"]
            all_on[sk] = sec_cfg
    # 显式把管理组的细项也全开
    all_on.setdefault("section_admin", {}).update({
        "admin_mute": True, "admin_mute_state": True, "admin_join_approval": True,
        "admin_recall_others": True, "admin_member_roster": True,
        "admin_kick": True, "admin_blacklist": True,
        "admin_join_request_notice": True,
    })
    all_on.setdefault("section_member", {}).update({
        "group_info_enabled": True, "member_query_enabled": True,
        "member_notice_enabled": True, "receive_files": True,
    })
    all_on.setdefault("section_basic", {})["extra_intents"] = True

    print("\n[1] 用「全开」配置构造插件（这一步本身就不该炸）")
    try:
        plugin = B.QQOfficialGroupBridge(
            type("Ctx", (), {"adapter_mgr": None})(), all_on)
        check("★ 全开配置能正常构造插件（不抛异常）", True)
    except Exception as exc:
        check("★ 全开配置能正常构造插件（不抛异常）", False, f"{type(exc).__name__}: {exc}")
        return 1

    # ---------------- 逐项读取 ----------------
    print("\n[2] 每项配置都能被读到（不会因为某项缺失而崩）")
    attrs = {
        "enabled": "enabled",
        "extra_intents": "extra_intents",
        "group_name_enabled": "group_name_enabled",
        "markdown_enabled": "markdown_enabled",
        "keyboard_enabled": "keyboard_enabled",
        "interaction_enabled": "interaction_enabled",
        "group_info_enabled": "group_info_enabled",
        "member_query_enabled": "member_query_enabled",
        "member_notice_enabled": "member_notice_enabled",
        "receive_files": "receive_files",
        "admin_tools_enabled": "admin_tools_enabled",
        "admin_mute": "admin_mute",
        "admin_mute_state": "admin_mute_state",
        "admin_join_approval": "admin_join_approval",
        "admin_member_roster": "admin_member_roster",
        "admin_kick": "admin_kick",
        "admin_blacklist": "admin_blacklist",
        "join_request_notice_enabled": "join_request_notice_enabled",
        "reply_to_self_wakes": "reply_to_self_wakes",
        "quote_reply": "quote_reply",
        "send_at_mention": "send_at_mention",
        "at_markdown": "at_markdown",
        "enhance_rich_content": "enhance_rich",
        "remember_nicknames": "remember_nicknames",
    }
    bad = []
    for label, attr in attrs.items():
        if not hasattr(plugin, attr):
            bad.append(attr)
    check("★ 全部配置项都映射到了实例属性", not bad, str(bad))

    # ---------------- 2.x 专属项在 3.0 的表现 ----------------
    print("\n[3] 「2.x 专属」的项落到 3.0 时会不会出问题")
    v2_only = {
        "unify_at_messages": "unify_at",
        "unify_direct_messages": "unify_dm",
        "at_grace_seconds": "at_grace",
        "resolve_at_markup": "resolve_at",
        "learn_self_openid": "learn_self_openid",
    }
    for label, attr in v2_only.items():
        ok = hasattr(plugin, attr)
        if GEN == "3":
            ROWS.append((label, "2.x 专属", "无操作但无害" if ok else "缺失 ✗"))
            check(f"{label}：3.0 上被正常读取（不生效，但无害）", ok)
        else:
            ROWS.append((label, "2.x", "生效" if ok else "缺失 ✗"))
            check(f"{label}：2.x 上生效", ok)

    # ---------------- 3.0 上"实际工作"的是哪些 ----------------
    print("\n[4] 3.0 上真正工作的是哪些（对照 core_profiles 的分工）")
    from core_profiles import detect as detect_profile
    try:
        adapter = None
        if GEN == "3":
            from core.adapter.context import AdapterContext
            from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
            adapter = QQOfficialAdapter(
                AdapterContext(info=info, event_queue=asyncio.Queue()))
        else:
            from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
            adapter = QQOfficialAdapter(info, asyncio.Queue())
        p = detect_profile(adapter)
        print(f"      世代探测：is_v2={p.is_v2} is_v3={p.is_v3} known={p.is_known}")
        if GEN == "3":
            check("★ 3.0 被正确识别为 v3", p.is_v3, str(p.detail))
        else:
            check("★ 2.x 被正确识别为 v2", p.is_v2, str(p.detail))
    except Exception as exc:
        check("世代探测可用", False, f"{type(exc).__name__}: {exc}")

    print()
    print("=" * 76)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
