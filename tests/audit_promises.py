"""复审 B：把「我在文档/PR 里承诺过的」逐条对照代码，确认真的实现了。

这个脚本的价值是防止"说了没做"——本次复审已经抓到一条：
  「intent 会尝试自动回退」曾经只写了日志、没有逻辑（现已补上）。
"""
import re
import pathlib
import sys

ROOT = pathlib.Path("/var/minis/workspace/qqbot_bridge_review/bridge")
main_src = (ROOT / "main.py").read_text(encoding="utf-8")
api_src = (ROOT / "api_send.py").read_text(encoding="utf-8")
gn_src = (ROOT / "group_names.py").read_text(encoding="utf-8")
int_src = (ROOT / "interactions.py").read_text(encoding="utf-8")
v3_src = (ROOT / "v3_support.py").read_text(encoding="utf-8")
rc_src = (ROOT / "rich_content.py").read_text(encoding="utf-8")
cp_src = (ROOT / "core_profiles.py").read_text(encoding="utf-8")
at_src = (ROOT / "admin_tools.py").read_text(encoding="utf-8")
qb_src = (ROOT / "qqbot_bridge.py").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")
schema = (ROOT / "schema.json").read_text(encoding="utf-8")

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


print("#" * 72)
print("## 复审 B：承诺 vs 实现")
print("#" * 72)

print("\n[B1] 文档/PR 里承诺过的行为")
# 承诺 1：intent 失败自动回退（本次复审发现并补上）
check("B1-1 intent 自愈：有健康检查函数", "def _health_check_intents" in main_src)
check("B1-2 intent 自愈：巡检循环里真的会调",
      "self._health_check_intents()" in main_src)
check("B1-3 intent 自愈：能真正读到网关状态（借 send_msg 探针抓网关）",
      "def _install_intent_patches" in main_src and "BotWebSocket.send_msg" in main_src
      and "_can_reconnect" in main_src)
check("B1-4 intent 自愈：还原时同时摘掉三个补丁",
      main_src.count("_kira_bridge_probe") >= 3
      and "_kira_bridge_intent" in main_src
      and "ws_identify" in main_src)

# 承诺 2：3 秒内回执
check("B1-5 互动回执有超时保护（不死等）", "wait_for" in int_src and "_ACK_TIMEOUT" in int_src)
check("B1-6 回执超时 < 官方 3 秒", float(re.search(r"_ACK_TIMEOUT = ([\d.]+)", int_src).group(1)) < 3.0)
check("B1-7 同一 interaction 只回执一次", "_acked" in int_src and "add(interaction_id)" in int_src)

# 承诺 3：群名失败自动降级、只提示一次
check("B1-8 群名失败被记住（不再重试）", "mark_failed" in gn_src and "has_failed" in gn_src)
check("B1-9 群名失败只提示一次", "_notified" in gn_src)
check("B1-10 群名拉取不阻塞（create_task）", "create_task" in gn_src)

# 承诺 4：markdown 失败退纯文本
check("B1-11 markdown 被拒 → 退纯文本", "MD_REJECT_CODES" in api_src
      and "fallback" in api_src)
check("B1-12 退回时剥掉平台 @ 标记", "strip_at_markup" in api_src)
check("B1-13 退回时放弃键盘（避免 22006 类型不匹配）", 'fallback.pop("keyboard"' in api_src)

# 承诺 5：作用域保护
check("B1-14 api 补丁有作用域保护（真的会被调用）", "bridge.owns(_api)" in api_src)

# 承诺 6：可逆
check("B1-15 还原覆盖 api 补丁", "api_send.restore" in main_src)
check("B1-16 还原覆盖发送入口", "_unpatch_send_entry" in main_src)
check("B1-17 还原覆盖 3.0 增量", "self.v3.restore" in main_src)
check("B1-18 还原覆盖互动回调", 'detach_client_handler(client, "on_interaction_create")' in main_src)
check("B1-19 还原覆盖 intent + 探针", "_revert_extra_intents()" in main_src)

print("\n[B2] 世代分支的完备性")
check("B2-1 有 v2/v3/unknown 三态", all(g in cp_src for g in ('GEN_V2', 'GEN_V3', 'GEN_UNKNOWN')))
check("B2-2 unknown 只跑 L1（不装事件层）", "认不出核心世代" in main_src)
check("B2-3 v3 分支不接管事件", "core_owns_fullmsg" in cp_src)
check("B2-4 v2 才补解析器", "is_v2" in main_src and "_ensure_class_patch" in main_src)
check("B2-5 allow_shadow 按世代决定", "allow_shadow_events" in main_src)

print("\n[B3] 官方接口用法正确性（逐条对照官方文档）")
check("B3-1 撤回：群用 group_openid 路径参数",
      "/v2/groups/{group_openid}/messages/{message_id}" in at_src)
check("B3-2 撤回：单聊用 user_openid 路径参数",
      "/v2/users/{user_openid}/messages/{message_id}" in at_src)
check("B3-3 禁言：POST restrict_chat_setting + members 数组",
      "restrict_chat_setting" in at_src and '"members"' in at_src)
check("B3-4 禁言：解禁用 op=del", '"op": "del"' in at_src)
check("B3-5 禁言：时长上限 30 天", "MAX_SECONDS = 30 * 24 * 3600" in at_src)
check("B3-6 禁言：RFC3339 带 +08:00", "+08:00" in at_src)
check("B3-7 禁言查询：GET restrict_chat_setting",
      'GET", "/v2/groups/{group_openid}/restrict_chat_setting' in at_src)
check("B3-8 bot_state：GET + 优雅降级提示",
      "bot_state" in at_src and "11253" in at_src)
check("B3-9 群信息：GET /v2/groups/{...}/info", "/v2/groups/{group_openid}/info" in gn_src)
check("B3-10 互动回执：PUT /interactions/{id} 且 code=0",
      "on_interaction_result" in int_src and ", 0)" in int_src)
check("B3-11 markdown 载荷形状 {markdown:{content:...}}",
      'kwargs["markdown"] = {"content": target_md}' in api_src)
check("B3-12 键盘作为顶层字段随报文发出", 'kwargs["keyboard"] = keyboard' in api_src)

print("\n[B4] schema ⇄ 代码 ⇄ README 配置一致")
import json

sk = set()
for sec in json.loads(schema).values():
    sk |= set(sec.get("fields", {}))
ck = set(re.findall(r'(?:basic|proactive)\.get\("([a-z_0-9]+)"', main_src))
rk = set(re.findall(r"^\| `([a-z_0-9]+)` \|", readme, re.M))
check("B4-1 代码读取的键都在 schema", not (ck - sk), str(sorted(ck - sk)))
check("B4-2 schema 的键都被读取", not (sk - ck), str(sorted(sk - ck)))
check("B4-3 README 覆盖全部配置项", not (sk - rk), str(sorted(sk - rk)))
check("B4-4 README 无多余项", not (rk - sk), str(sorted(rk - sk)))
check("B4-5 新配置项默认值符合承诺（除 extra_intents 外全开）",
      '"default": true' in schema and schema.count('"extra_intents"') == 1)

print("\n[B5] 无残留死代码")
check("B5-1 api_send 无未使用的私有函数", "_self_of" not in api_src)
check("B5-2 api_send 无未使用的常量", "_URL_REJECT" not in api_src and "_AT_MARKUP" not in api_src)
check("B5-3 api_send 无未使用的参数", "md_mode" not in api_src)
check("B5-4 owns() 真的被调用（不是死代码）", "bridge.owns(_api)" in api_src)

print()
print("=" * 72)
print(f"结果：{PASS} passed, {FAIL} failed")
print("=" * 72)
sys.exit(1 if FAIL else 0)
