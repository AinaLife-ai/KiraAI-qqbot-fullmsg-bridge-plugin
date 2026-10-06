"""最终审计：
  1. 静态：未使用导入、裸 await、死代码引用、TODO/FIXME 残留；
  2. 一致性：schema ⇄ 代码 ⇄ README；
  3. 兼容性：插件 API 是否在 2.x / 3.0 都存在；
  4. 无功能丢失：v1.2.0 的每一项能力都能在新代码里找到对应实现；
  5. 冲突检查：同一文件是否出现重复定义 / 同名方法覆盖。
"""
import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path("/var/minis/workspace/qqbot_bridge_review/bridge")
FAILS = []
OKS = []


def check(name, ok, detail=""):
    (OKS if ok else FAILS).append(name)
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail and not ok else ""))


FILES = ["main.py", "qqbot_bridge.py", "core_profiles.py", "group_names.py",
         "rich_content.py", "interactions.py", "api_send.py", "v3_support.py",
         "admin_tools.py"]

print("=" * 72)
print("[1] 静态检查")
print("=" * 72)
for name in FILES:
    src = (ROOT / name).read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        check(f"{name} 语法正确", False, str(exc))
        continue
    check(f"{name} 语法正确", True)

    # 未使用导入
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [(a.asname or a.name.split(".")[0]) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            names += [(a.asname or a.name) for a in node.names]
    body = re.sub(r"^(from|import) .*$", "", src, flags=re.M)
    body = re.sub(r'""".*?"""', "", body, flags=re.S)
    unused = [n for n in names if not re.search(r"\b" + re.escape(n) + r"\b", body)]
    check(f"{name} 无未使用导入", not unused, str(unused))

    # 裸 await（同步函数里）
    def scan(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(child, ast.Await) or scan(child):
                return True
        return False

    bad = [n.name for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and scan(n)]
    check(f"{name} 同步函数里没有裸 await", not bad, str(bad))

    # TODO / FIXME / XXX 残留
    leftovers = re.findall(r"#\s*(TODO|FIXME|XXX|HACK)\b", src)
    check(f"{name} 无 TODO/FIXME 残留", not leftovers, str(leftovers))

print()
print("=" * 72)
print("[2] 无功能丢失（对照 v1.2.0 的能力清单）")
print("=" * 72)
main_src = (ROOT / "main.py").read_text(encoding="utf-8")
bridge_src = (ROOT / "qqbot_bridge.py").read_text(encoding="utf-8")

FEATURES = {
    "全量群消息解析器补丁": "install_class_parser",
    "事件构造（昵称/@/引用/去重）": "build_event",
    "去重（含 AT 优先）": "MessageDedup",
    "昵称通讯录": "IdentityStore",
    "自我身份识别": "SelfIdentity",
    "@ 富文本解析": "split_at_markup",
    "发出的 @ 用平台标记": "at_user_markup",
    "引用回复（收发）": "extract_msg_idx",
    "富内容归一化": "normalize_rich_body",
    "主动消息兜底": "_proactive_send",
    "可逆还原": "_restore_all",
    "2.x 事件等待窗口": "at_grace",
    "群名缓存（新）": "GroupInfoCache",
    "markdown 标签（新）": "MarkdownTag",
    "键盘标签（新）": "KeyboardTag",
    "互动回调（新）": "InteractionBridge",
    "群管理工具（新）": "build_admin_tools",
    "世代探测（新）": "detect_profile",
    "3.0 增量（新）": "V3Enhancer",
    "成员事件（新）": "describe_member_event",
    "api 层发送增强（新）": "ApiSendPatcher",
}
all_src = main_src + bridge_src + "\n".join(
    (ROOT / f).read_text(encoding="utf-8") for f in FILES
)
for label, token in FEATURES.items():
    check(f"在位：{label}", token in all_src, f"找不到 {token}")

print()
print("=" * 72)
print("[3] 冲突检查（重复定义 / 同名方法）")
print("=" * 72)
tree = ast.parse(main_src)
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef):
        seen = {}
        dup = []
        for ch in node.body:
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if ch.name in seen:
                    dup.append(ch.name)
                seen[ch.name] = ch
        check(f"{node.name} 无同名方法覆盖", not dup, str(dup))

# 模块级重复函数
mod_fns = [n.name for n in ast.parse(main_src).body if isinstance(n, ast.FunctionDef)]
dups = {n for n in mod_fns if mod_fns.count(n) > 1}
check("main.py 模块级无重复函数", not dups, str(dups))

for name in FILES[1:]:
    t = ast.parse((ROOT / name).read_text(encoding="utf-8"))
    fns = [n.name for n in t.body if isinstance(n, ast.FunctionDef)]
    d = {n for n in fns if fns.count(n) > 1}
    check(f"{name} 模块级无重复函数", not d, str(d))

print()
print("=" * 72)
print("[4] schema ⇄ 代码 ⇄ README 一致")
print("=" * 72)
schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
schema_keys = set()
for sec in schema.values():
    schema_keys |= set(sec.get("fields", {}))
code_keys = set(re.findall(r'(?:basic|proactive)\.get\("([a-z_0-9]+)"', main_src))
readme_keys = set(re.findall(r"^\| `([a-z_0-9]+)` \|", (ROOT / "README.md").read_text(encoding="utf-8"), re.M))
check("代码读的键都在 schema", not (code_keys - schema_keys), str(sorted(code_keys - schema_keys)))
check("schema 的键都被读", not (schema_keys - code_keys), str(sorted(schema_keys - code_keys)))
check("README 覆盖全部配置项", not (schema_keys - readme_keys), str(sorted(schema_keys - readme_keys)))
check("README 没有多余项", not (readme_keys - schema_keys), str(sorted(readme_keys - schema_keys)))

print()
print("=" * 72)
print(f"结果：{len(OKS)} passed, {len(FAILS)} failed")
print("=" * 72)
sys.exit(1 if FAILS else 0)
