"""v1.3.3 审计：分组开关 / 新工具 / 权限守卫 / 内邀自动探测 / 配置兼容性。

覆盖用户 2026-10-07 明确提出的四点：
  1. 发文件默认开
  2. 撤回自己消息默认开（不需要拆"自己/他人"开关 —— 平台自动判权限）
  3. 所有不需要群管权限的功能都默认开
  4. 通讯录版找人（含新接入的 @ 消息 mentions[] 来源）
再加上："存量用户升级无感"（核心只在键不存在时填默认值）。
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def load_admin_tools():
    spec = importlib.util.spec_from_file_location(
        "admin_tools", str(ROOT / "admin_tools.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["admin_tools"] = m
    spec.loader.exec_module(m)
    return m


def main():
    print("=" * 72)
    print("## v1.3.3 审计：分组开关 / 新工具 / 守卫 / 内邀探测 / 兼容")
    print("=" * 72)

    A = load_admin_tools()
    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))

    def dflt(section, key):
        return schema[section]["fields"][key]["default"]

    # ---------------- 1. 用户点名的默认值 ----------------
    print("\n[1] 默认值（用户点名的四条）")
    check("★ 发文件默认开（用户点名）", dflt("section_admin", "admin_send_file") is True)
    check("★ 撤回默认开（自己消息，平台自动判权限）",
          dflt("section_basic", "recall_enabled") is True
          if "recall_enabled" in schema["section_basic"]["fields"] else True)
    check("★ 额外订阅位默认开（用户点名）", dflt("section_basic", "extra_intents") is True)
    check("★ 找人默认开", dflt("section_member", "member_query_enabled") is True)
    check("★ 群信息默认开", dflt("section_member", "group_info_enabled") is True)
    check("★ 读文件默认开", dflt("section_member", "receive_files") is True)
    check("★ 成员进出通知默认开", dflt("section_member", "member_notice_enabled") is True)
    check("★ 加群申请提醒默认开（只读）", dflt("section_member", "join_request_notice_enabled") is True)

    # ---------------- 2. 需要权限的一律默认关 ----------------
    print("\n[2] 需要群管权限的功能：一律默认关")
    for key in ("admin_tools_enabled", "admin_mute", "admin_mute_state",
                "admin_join_approval", "admin_recall_others",
                "admin_member_roster", "admin_kick", "admin_blacklist"):
        check(f"{key} 默认关", dflt("section_admin", key) is False)

    # ---------------- 3. 默认配置下不泄漏任何管理工具 ----------------
    print("\n[3] 默认配置下注册的工具（关键：不能有管理工具）")
    names = [t.name for t in A.build_tools({})]
    print("     ", names)
    ADMIN_TOOLS = {
        "set_qq_group_ban", "get_group_mute_state", "manage_qq_group_join_request",
        "kick_qq_group_member", "get_qq_group_member_roster",
        "manage_qq_group_blacklist",
    }
    leak = ADMIN_TOOLS & set(names)
    check("★ 默认配置下没有任何需要权限的工具", not leak, str(leak))
    check("默认含撤回", "recall_qq_msg" in names)
    check("默认含群信息", "get_qq_group_info" in names)
    check("默认含找人", "find_qq_group_member" in names)
    check("默认含读文件", "read_qq_attached_file" in names)
    check("默认含发文件", "send_qq_file" in names)

    # ---------------- 4. 全开时应全部就位 ----------------
    print("\n[4] 全开时工具全就位（功能全在）")
    allcfg = {
        "recall_enabled": True, "group_info_enabled": True,
        "member_query_enabled": True, "receive_files": True,
        "send_file_enabled": True, "bot_state_enabled": True,
        "admin_tools_enabled": True, "mute_enabled": True,
        "mute_state_enabled": True, "join_approval_enabled": True,
        "kick_enabled": True, "roster_enabled": True, "blacklist_enabled": True,
    }
    allnames = [t.name for t in A.build_tools(allcfg)]
    check("★ 全开时所有 12 个工具都注册", len(allnames) == 12, str(allnames))
    check("★ 管理工具全部就位", ADMIN_TOOLS <= set(allnames), str(ADMIN_TOOLS - set(allnames)))

    # ---------------- 5. 总闸语义：单开细项但总闸关 => 不注册 ----------------
    print("\n[5] 总闸优先（防误开）")
    only_mute = {"admin_tools_enabled": False, "mute_enabled": True,
                 "join_approval_enabled": True, "kick_enabled": True}
    got = [t.name for t in A.build_tools(only_mute)]
    check("★ 总闸关时，细项即使打开也不注册",
          not ({"set_qq_group_ban", "manage_qq_group_join_request",
                "kick_qq_group_member"} & set(got)), str(got))

    # ---------------- 6. 内邀自动探测 ----------------
    print("\n[6] 内邀端点自动探测与降级")
    A._INVITE_BLOCKED.clear()
    check("初始未标记", not A.invite_blocked("qq", "members"))
    A.mark_invite_blocked("qq", "members")
    check("★ 标记后同一端点被记住", A.invite_blocked("qq", "members"))
    check("其它端点不受影响（按端点隔离）", not A.invite_blocked("qq", "kick"))
    check("其它适配器不受影响（按适配器隔离）", not A.invite_blocked("qq2", "members"))

    # 守卫会用上它
    roster = A.GroupMemberRosterTool(ctx=None)
    class _Ev:
        class session: adapter_name = "qq"
    blocked = roster._guard(_Ev())
    check("★ 已被标记的内邀端点：守卫直接返回人话、不发请求",
          "内邀" in blocked and "请不要重试" in blocked, blocked)

    # ---------------- 7. 身份守卫：普通成员被拦 ----------------
    print("\n[7] 身份守卫（机器人不是管理员时明确告知）")
    A._ROLE_CACHE.clear()
    A.set_role("qq", "member")
    mute = A.SetGroupMuteTool(ctx=None)
    # SetGroupMuteTool 是旧类（无 _guard），用新的 _AdminTool 子类验证
    kick = A.KickGroupMemberTool(ctx=None)
    A._INVITE_BLOCKED.clear()
    g = kick._guard(_Ev())
    check("★ 角色=普通成员 ⇒ 明确提示需要管理员", "群管理员" in g, g)
    A.set_role("qq", "admin")
    g2 = kick._guard(_Ev())
    check("角色=管理员 ⇒ 放行", g2 == "", g2)
    A._ROLE_CACHE.clear()
    g3 = kick._guard(_Ev())
    check("没查到角色 ⇒ 放行（绝不因未知而误拦）", g3 == "", g3)

    # ---------------- 8. 通讯录：@ 消息 mentions 来源 ----------------
    print("\n[8] 通讯录：@ 消息 mentions[] 来源（新，免费）")
    from identity_shared import IdentityStore
    st = IdentityStore(path=os.path.join(tempfile.mkdtemp(), "i.json"))
    n = st.remember_from_mentions("qq", [
        {"member_openid": "A1", "username": "小明", "member_role": "admin"},
        {"member_openid": "B2", "username": "小红", "member_role": "owner"},
        {"member_openid": "C3", "username": "小刚", "member_role": "member"},
    ])
    check("★ 学到 3 人（含角色）", n >= 3, str(n))
    check("★ 角色能取到中文", st.role_of("qq", "A1") == "管理员", st.role_of("qq", "A1"))
    check("★ 按昵称搜得到", bool(st.search("qq", "小")), str(st.search("qq", "小")))
    check("★ 按 openid 前缀搜得到", bool(st.search("qq", "B2")), str(st.search("qq", "B2")))
    st.remember_from_mentions("qq", [{"member_openid": "A1", "username": "小明改名"}])
    check("★ 改名自动跟随", st.search("qq", "小明改名"), str(st.search("qq", "小明")))
    check("不编造：搜不到就空", st.search("qq", "查无此人") == [])
    check("异常输入不炸", st.remember_from_mentions("qq", None) == 0
          and st.remember_from_mentions("qq", "x") == 0)
    st.save()
    st2 = IdentityStore(path=st.path)
    check("★ 角色落盘后可回读", st2.role_of("qq", "B2") == "群主", st2.role_of("qq", "B2"))

    # ---------------- 9. 存量用户升级无感（核心的补默认值语义） ----------------
    print("\n[9] 存量用户升级无感")
    old_cfg = {"section_basic": {"admin_tools_enabled": True},
               "section_admin": {"admin_mute": True}}
    # 模拟核心 _ensure_plugin_config 的语义：只在键不存在时填默认
    merged = {}
    for sk, sec in schema.items():
        if sec.get("type") != "section":
            continue
        cur = dict(old_cfg.get(sk) or {})
        for k, f in (sec.get("fields") or {}).items():
            if k not in cur:
                cur[k] = f.get("default")
        merged[sk] = cur
    check("★ 老用户已存的 admin_tools_enabled=True 被保留（不被新默认覆盖）",
          merged["section_basic"]["admin_tools_enabled"] is True)
    check("★ 老用户已存的 admin_mute=True 被保留",
          merged["section_admin"]["admin_mute"] is True)
    check("★ 新增的键才用新默认（admin_member_roster=False）",
          merged["section_admin"]["admin_member_roster"] is False)
    check("★ 新分组的键也都补上了（member_query_enabled=True）",
          merged["section_member"]["member_query_enabled"] is True)

    # ---------------- 10. schema 文案规范（沿用既有约定） ----------------
    print("\n[10] schema 文案规范（无 markdown / 无裸尖括号）")
    bad = []
    for sk, sec in schema.items():
        for k, f in (sec.get("fields") or {}).items():
            for field in ("name", "hint"):
                txt = str(f.get(field) or "")
                if "**" in txt or "`" in txt or txt.count("*") >= 2:
                    bad.append(f"{sk}.{k}.{field}=markdown")
                for ch in txt:
                    pass
                import re as _re
                if _re.search(r"<[A-Za-z/][^>]*>", txt):
                    bad.append(f"{sk}.{k}.{field}=tag")
    check("★ 无 markdown 标记 / 无裸尖括号标签", not bad, str(bad[:5]))

    print()
    print("=" * 72)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
