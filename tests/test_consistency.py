"""一致性 & 静态不变量检查 —— 防止「改了代码忘了改文档/schema」这类漂移。

    python3 tests/test_consistency.py

覆盖：
  1. schema.json ⇄ main.py（每个配置项都真的被读；代码读的键都在 schema 里）
  2. schema.json ⇄ README 配置表（不多不少）
  3. AST：同步函数里不能出现裸 await（运行时会直接报错）
  4. 未使用的导入（`from __future__` 除外）
  5. 本仓库明确约定的几条不变量（都是踩过坑之后立的规矩）
"""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
PLUGIN = ROOT if (ROOT / "main.py").exists() else ROOT / "bridge_plugin"

FAILS = []


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def read(p):
    return (PLUGIN / p).read_text(encoding="utf-8")


main_py = read("main.py")
bridge_py = read("qqbot_bridge.py")
readme = read("README.md")
schema = json.loads(read("schema.json"))

# ---------------------------------------------------------------- 1. schema ⇄ 代码
print("schema ⇄ 代码")
schema_keys = set()
for section in schema.values():
    schema_keys |= set(section.get("fields", {}))
# 各 section 对应的读取变量名（v1.3.3 起拆成 member / admin 两组）
code_keys = set(re.findall(
    r'(?:basic|proactive|member|admin)\.get\("([a-z_0-9]+)"', main_py))

missing_in_code = sorted(schema_keys - code_keys)
missing_in_schema = sorted(code_keys - schema_keys)
check("schema 里的每个配置项都被代码读取", not missing_in_code, f"未读取: {missing_in_code}")
check("代码读取的每个配置键都在 schema 里", not missing_in_schema, f"schema 缺失: {missing_in_schema}")
check("配置项数量非空", len(schema_keys) > 0, str(len(schema_keys)))

# ---------------------------------------------------------------- 2. schema ⇄ README
print("\nschema ⇄ README 配置表")
readme_keys = set(re.findall(r"^\| `([a-z_0-9]+)` \|", readme, re.M))
check("README 配置表覆盖全部配置项", not (schema_keys - readme_keys),
      f"漏文档: {sorted(schema_keys - readme_keys)}")
check("README 配置表没有已删除的项", not (readme_keys - schema_keys),
      f"多余: {sorted(readme_keys - schema_keys)}")

# ---------------------------------------------------------------- 3. 裸 await
print("\nAST 静态检查")


def bare_await_in_sync(src, label):
    """只检查同步函数**自身**函数体里的 await（不进入嵌套函数定义）。"""
    tree = ast.parse(src)

    def scan(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(child, ast.Await) or scan(child):
                return True
        return False

    return [f"{label}:{n.name}" for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and scan(n)]


bad_await = bare_await_in_sync(main_py, "main.py") + bare_await_in_sync(bridge_py, "qqbot_bridge.py")
check("同步函数里没有裸 await", not bad_await, str(bad_await))

# ---------------------------------------------------------------- 4. 未使用导入
for fname, src in (("main.py", main_py), ("qqbot_bridge.py", bridge_py)):
    names = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names += [(a.asname or a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue  # 编译指令，不是名字
            names += [(a.asname or a.name) for a in node.names]
    body = re.sub(r"^(from|import) .*$", "", src, flags=re.M)
    unused = [n for n in names if not re.search(r"\b" + re.escape(n) + r"\b", body)]
    check(f"{fname} 没有未使用的导入", not unused, str(unused))

# ---------------------------------------------------------------- 5. 约定不变量
print("\n约定不变量（踩坑后立的规矩）")
check("本地不设备份的平台配额（主动消息无每日上限）",
      "proactive_daily_limit" not in main_py and "daily limit" not in main_py)
check("不去动框架的会话缓冲（ctx.get_buffer 已移除）",
      "get_buffer" not in main_py and "get_buffer" not in bridge_py)
check("at_grace_seconds 默认 0", 'basic.get("at_grace_seconds", 0)' in main_py)
check("proactive_min_interval 默认 0", 'proactive.get("proactive_min_interval", 0)' in main_py)
check("消息路径上没有同步写文件", "open(" not in re.sub(r'""".*?"""', "", main_py, flags=re.S)
      or not re.search(r"open\([^)]*['\"]w['\"]", main_py))
check("build_event 调用被 try 包裹", "构造事件失败" in main_py)
check("handler 静默兜住异常（不抛回 botpy）", "绝不把异常抛回 botpy" in main_py)
check("自我身份按适配器隔离", "self._self_ident = {}" in main_py)
check("AT 标记正则限定为字母数字 id（两种形态都覆盖）",
      'AT_MARKUP_RE = re.compile(' in bridge_py
      and '[0-9A-Za-z]{8,}' in bridge_py
      and 'qqbot-at-user' in bridge_py)
check("发送侧 @ 用平台标记（不是纯文本 @昵称）", "qqbot-at-user id=" in bridge_py)
check("@ 解析有短路保护（无 <@ 直接返回）", '"<@" not in' in bridge_py)
check("At 元素渲染保留 pid（不退回只显示昵称）", "At(oid, label) if label else At(oid)" in bridge_py)

check("发送链路补丁的门控包含全部三个开关（防配置互相关掉功能）",
      "self.proactive_enabled or self.quote_reply or self.send_at_mention" in main_py)
check("发出的 @ 会校验 pid 合法性（避免发出坏标记）", "pid.isalnum()" in main_py)
check("富内容归一化对脏 message_type 有兜底", "except (TypeError, ValueError):\n            _mtype = 0" in bridge_py)

print()
print("PASSED" if not FAILS else f"{len(FAILS)} 项失败：{FAILS}")
sys.exit(1 if FAILS else 0)
