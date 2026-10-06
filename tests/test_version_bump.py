"""版本一致性检查：manifest.version ⇄ README 标题 ⇄ README 最新变更小节。

    python3 tests/test_version_bump.py
"""
import json
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
if (ROOT / "main.py").exists():
    PLUGIN = ROOT
else:
    PLUGIN = ROOT / "bridge_plugin"

FAILS = []


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


version = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))["version"]
readme = (PLUGIN / "README.md").read_text(encoding="utf-8")

title = re.search(r"(?:补丁|增强)\s*v(\d+\.\d+\.\d+)", readme)
check("manifest.version 是 x.y.z", bool(re.fullmatch(r"\d+\.\d+\.\d+", version)), version)
check("README 标题版本 == manifest.version",
      bool(title) and title.group(1) == version,
      f"标题={title.group(1) if title else None} manifest={version}")

logs = re.findall(r"<summary><b>v(\d+\.\d+\.\d+)</b>", readme)
check("README 有变更小节", bool(logs), str(logs))
check("最新变更小节 == manifest.version", bool(logs) and logs[0] == version,
      f"最新={logs[0] if logs else None} manifest={version}")
# manifest.description 的风格约束（用户 2026-10-08 要求）：
#   保持原有风格（口语、面向用户、不用功能清单口吻），**三句话内**说完亮点。
_desc = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8")).get("description", "")
_sents = [x for x in re.split(r"[。！？]", _desc) if x.strip()]
check("description 三句以内", len(_sents) <= 3, f"{len(_sents)} 句")
check("description 长度克制（<=220 字）", len(_desc) <= 220, f"{len(_desc)} 字")
check("description 不用功能清单口吻（不出现 markdown 列表符）",
      "\n-" not in _desc and "\n*" not in _desc)
check("变更小节按版本降序",
      logs == sorted(logs, key=lambda v: [int(i) for i in v.split(".")], reverse=True), str(logs))

print()
print("PASSED" if not FAILS else f"{len(FAILS)} FAILED")
sys.exit(1 if FAILS else 0)
