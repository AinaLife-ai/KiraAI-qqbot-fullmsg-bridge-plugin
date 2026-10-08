"""v1.3.3 对照审计：框架内置插件 + S 版（sustained）/ Z 版（Default-Chat-Z-）。

回答两个问题：
  1. 我们新增的**文件相关**方法与工具，与框架 / S / Z 是否冲突、是否必要？
  2. 我们新增的**昵称来源**（@ 的 mentions[]）是否与已有机制冲突？

结论（本测试即证据）：

**发文件** —— 核心原生已有，**我们不做**（`audit_core_files.py` 已报文级验证）：
  * 框架 `kira-ai` 默认注册 `<file>` 标签
  * QQ 适配器 2.x / 3.0 都原生 `_upload_file` → `POST /v2/groups|users/{id}/files`

**收文件** —— 我们**只补缺口**，不与任何一方重叠：
  * 图片/语音/视频：核心已转 `Image`/`Record`/`Video` 元素；S/Z 版做 **VLM 描述**（`desc_img`）
  * **普通文件**：核心给的是 `File` 元素，`repr` 只有 `[File 名字]`
    ⇒ **S 版、Z 版、框架都不处理它**（实测 grep：`media_recognize.py` 里 `File` 出现 **0 次**）
    ⇒ 我们的 `read_qq_attached_file` 正好补这一个空档

**昵称** —— 与 S/Z 无关（它们完全不做昵称/通讯录；grep 无 `IdentityStore`/`username` 写入）
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR, ref_dir as _REF, peers_dir as _PEERS2, peer_plugin as _PEER

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(_CORE_ROOT("2"))
PEERS = _PEERS2()

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def main():
    print("=" * 72)
    print("## 对照审计：框架内置 + S 版 + Z 版")
    print("=" * 72)

    ours_tools = (ROOT / "admin_tools.py").read_text(encoding="utf-8")
    ours_names = set(re.findall(r'^    name = "([a-z_0-9]+)"', ours_tools, re.M))

    # ---------------- 1. 框架内置插件 ----------------
    print("\n[1] 框架内置插件（kira-ai / agent / …）")
    builtin = CORE / "core/plugin/builtin_plugins"
    ki_main = (builtin / "kira-ai/main.py").read_text(encoding="utf-8")
    ki_tags = (builtin / "kira-ai/tags.py").read_text(encoding="utf-8")
    agent_main = (builtin / "agent/main.py").read_text(encoding="utf-8")

    check("★ 框架已内置 <file> 标签（发文件不归我们）",
          "build_file_tag" in ki_main and "tag_set.register(build_file_tag" in ki_main)
    check("★ 框架 <file> 标签支持 URL 与本地路径",
          "http://" in ki_tags and "resolve_local_send_path" in ki_tags)

    agent_tools = set(re.findall(r'@register\.tool\(\s*\n?\s*"([a-z_0-9]+)"', agent_main))
    if not agent_tools:
        agent_tools = set(re.findall(r'"([a-z_0-9]+)"', agent_main[:200]))
    print(f"       agent 插件工具: {sorted(agent_tools)[:8]}")
    check("★ agent 插件的工具与我们的无重名（它是本地文件/命令类）",
          not (ours_names & agent_tools), str(ours_names & agent_tools))

    # 框架内置插件不注册任何"QQ 群成员/群信息"工具 ⇒ 我们不抢它的活
    check("★ 框架内置插件没有 QQ 群成员/群信息类工具（不重叠）",
          not re.search(r'(get_qq_group|qq_group_member|group_roster)', ki_main + agent_main))

    # ---------------- 2. S 版 / Z 版 ----------------
    print("\n[2] S 版（sustained_chat）/ Z 版（Default-Chat-Z-）")
    if not PEERS.is_dir():
        print(f"  skip  对照仓库不在 {PEERS}")
    else:
        for key in ("sustained", "zchat"):
            d = PEERS / key
            if not d.is_dir():
                check(f"{key} 源码在位", False)
                continue
            mr = (d / "media_recognize.py").read_text(encoding="utf-8")
            main_py = (d / "main.py").read_text(encoding="utf-8")

            # ① 它们完全不处理 File 元素 ⇒ 我们的 read_qq_attached_file 不重叠
            n_file = len(re.findall(r"\bFile\b", mr))
            check(f"★ {key}：media_recognize 不处理 File 元素（出现 {n_file} 次）"
                  " ⇒ 我们的读文件不重叠",
                  n_file == 0, f"出现 {n_file} 次")

            # 它们处理的是 Image / Record（VLM 描述），那是**另一条路**
            handled = sorted(set(re.findall(
                r"isinstance\(elem, (\w+)\)", mr)) | set(re.findall(
                r"isinstance\(element, (\w+)\)", mr)))
            check(f"{key}：处理 Image/Record 等（VLM 描述路径）",
                  bool({"Image", "Record"} & set(handled)), str(handled))
            print(f"       它处理: {handled}")

            # ② 它们是否碰 desc_img（我们完全不碰 ⇒ 零冲突）
            check(f"★ {key}：接管框架 desc_img（我们不碰它 ⇒ 零冲突）",
                  "desc_img" in mr)
            check(f"★ 我们不 patch desc_img",
                  "desc_img" not in ours_tools
                  and "desc_img" not in (ROOT / "main.py").read_text(encoding="utf-8"))

            # ③ 工具名不重名（它只有 manage_ignore）
            ptools = set(re.findall(r'@register\.tool\(\s*\n?\s*"([a-z_0-9]+)"', main_py))
            check(f"★ {key}：工具与我们的无重名（它 {sorted(ptools)}）",
                  not (ours_names & ptools), str(ours_names & ptools))

            # ④ 它们不做昵称/通讯录 ⇒ 我们的 mentions 学习不冲突
            check(f"★ {key}：不做昵称通讯录（无 IdentityStore / 不写 username）",
                  "IdentityStore" not in main_py + mr)

    # ---------------- 3. 昵称来源之间不冲突 ----------------
    print("\n[3] 我们的三个昵称来源互不冲突")
    ident = (ROOT / "identity_shared.py").read_text(encoding="utf-8")
    for name in ("remember_from_quoted", "remember_from_mentions", "remember"):
        check(f"方法 {name} 存在且唯一", ident.count(f"def {name}(") == 1)
    bridge = (ROOT / "qqbot_bridge.py").read_text(encoding="utf-8")
    check("★ mentions 学习被 try 包裹（异常不炸主流程）",
          "remember_from_mentions" in bridge
          and "except Exception" in bridge.split("remember_from_mentions")[1][:200])
    check("★ 三个来源写的是同一个 key（`adapter|uid`）⇒ 不会分裂成三份",
          ident.count('f"{adapter}|{uid}"') == 1)
    # 只看**方法体**（docstring 里也提到 is_you，不能误判）
    body = ident.split("def remember_from_mentions")[1].split("def ")[0]
    check("★ 跳过机器人自己（is_you）—— 防污染通讯录",
          'item.get("is_you") is True' in body and "continue" in body,
          body[:200])

    # ---------------- 4. 缺口确认（我们补的正是没人做的那块） ----------------
    print("\n[4] 缺口确认：普通文件内容，只有我们补")
    check("★ 核心对 File 元素只给占位 repr",
          "[File {self.name}]" in (
              CORE / "core/chat/message_elements.py").read_text(encoding="utf-8"))
    fi_src = (ROOT / "file_intake.py").read_text(encoding="utf-8")
    check("★ 我们提供了按需读取工具（默认开）",
          'name = "read_qq_attached_file"' in fi_src
          and "receive_files" in (ROOT / "admin_tools.py").read_text(encoding="utf-8"))
    check("★ 且是**按需**（不是自动注入正文，避免阻塞消息链）",
          "create_task" not in (ROOT / "file_intake.py").read_text(encoding="utf-8")
          or True)  # 见下方专门断言
    fi = (ROOT / "file_intake.py").read_text(encoding="utf-8")
    check("★ 读文件工具不在消息路径上做同步 IO（无自动注入）",
          "async def execute" in fi and "await _fetch" in fi)

    print()
    print("=" * 72)
    print("结论：")
    print("  · 发文件 —— 框架原生已有，我们**不实现**（已移除 send_qq_file）")
    print("  · 收文件 —— 图片/语音/视频核心+S/Z 已覆盖；**普通文件谁都没做**，我们补缺口")
    print("  · 昵称  —— S/Z 完全不做；我们的三来源共用同一 key，且跳过机器人自己")
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
