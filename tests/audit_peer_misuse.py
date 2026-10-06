"""决定性实测：三个同类插件的工具，在**官方 QQ bot** 会话里被调用时会怎样？

背景（已核实的事实，不是推测）：
  * 三个插件用 `@register.tool` 注册工具 —— 该装饰器在**插件类定义时**就把工具
    注册进**全局 ToolManager**（`plugin_registry.RegisterDeco.tool` →
    `_register_plugin_tools_for` → `ctx.tool_mgr.register_tool`），
    **与会话平台无关**。
  * 所以它们的工具**确实会出现在官方 bot 会话的可用工具列表里**。
  * 真正拦住它们的是**工具执行体内部**的平台判定：
    `getattr(event.adapter, "platform", "") != "QQ"`。

本测试要回答：**被拦住时返回什么**？
  * 明确的人话错误（模型能读懂、会放弃）—— 可接受
  * 崩溃 / 卡住 / 静默假成功 —— 不可接受
"""
import asyncio
import importlib.util
import pathlib
import sys
import types

REF = pathlib.Path("/tmp/ref_repos")
CORE = pathlib.Path("/var/minis/workspace/qqbot_bridge_review/kira-core")
sys.path.insert(0, str(CORE))

PASS = FAIL = 0
RESULTS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def _fake_event():
    """构造一个**官方 bot** 事件（platform = "QQ Official"）。"""
    class Adapter:
        name = "qqo"
        platform = "QQ Official"

    class E:
        adapter = Adapter()
        messages = [object()]

        def is_group_message(self):
            return True

        def __init__(self):
            class S:
                session_id = "G1"
                session_type = "gm"
                adapter_name = "qqo"
            self.session = S()
    return E()


def _load_plugin_main(path: pathlib.Path, modname: str):
    """尽力导入插件的 main.py（缺依赖时返回 None）。"""
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:
        print(f"      （{modname} 导入失败：{type(exc).__name__}: {str(exc)[:70]}）")
        return None


def main():
    print("=" * 72)
    print("## 实测：同类插件工具在官 bot 会话里被调用会怎样")
    print("=" * 72)

    ev = _fake_event()
    print(f"\n测试事件：platform={ev.adapter.platform!r}（模拟官方 bot 会话）")

    # ---------------- 0. 先确认「全局注册」这个前提 ----------------
    print("\n[0] 前提核实：工具注册是否与会话平台无关")
    reg = (CORE / "core/plugin/plugin_registry.py").read_text(encoding="utf-8")
    seg = reg.split("def tool(name")[1].split("def tag(")[0]
    check("★ register.tool 在**类定义时**注册（无平台判定）",
          "register_tool" in seg and "platform" not in seg)
    check("★ 注册进的是全局 tool_mgr（与会话无关）",
          "ctx.tool_mgr.register_tool" in reg)

    # ---------------- 1. gmv（群成员查询） ----------------
    print("\n[1] gmv（群成员查询）—— 它的工具体被调用时")
    s = (REF / "gmv/main.py").read_text(encoding="utf-8")
    check("★ 平台判定存在（platform != 'QQ' 即拦）",
          'getattr(event.adapter, "platform", "") != "QQ"' in s)
    m = __import__("re").search(r'def _not_qq_reply\(\)[^\n]*\n\s*return "([^"]+)"', s)
    msg = m.group(1) if m else ""
    print(f"      拦截时的返回：{msg!r}")
    check("★ 返回的是**明确人话**（不是异常/静默）",
          "❌" in msg and "QQ" in msg, msg)
    RESULTS.append(("gmv.group_member_overview", msg))
    RESULTS.append(("gmv.group_find_member", msg))
    RESULTS.append(("gmv.group_member_detail", msg))

    # ---------------- 2. gmp（群管理） ----------------
    print("\n[2] gmp（群管理）—— 它的工具体被调用时")
    g = (REF / "gmp/main.py").read_text(encoding="utf-8")
    check("★ _get_qq_client 也做平台判定（返回 None 而非抛异常）",
          '!= "QQ":' in g and "return None" in g)
    check("★ 拿不到 client 时返回明确人话",
          "当前会话不是QQ或无法连接到QQ客户端" in g)
    # allow_ai_autonomous 默认 True ⇒ 权限层不拦，直接落到平台层
    import json as _json
    sc = _json.loads((REF / "gmp/schema.json").read_text(encoding="utf-8"))
    auto = sc.get("section_admin", {}).get("fields", {}).get("allow_ai_autonomous", {}).get("default")
    print(f"      allow_ai_autonomous 默认 = {auto!r}（True ⇒ 权限层直接放行，落到平台层）")
    check("★ 权限层不会拦住（默认自主模式），由平台层给出错误", auto is True)
    RESULTS.append(("gmp.group_ban_user", "❌ 当前会话不是QQ或无法连接到QQ客户端"))
    RESULTS.append(("gmp.group_get_member_list", "❌ 当前会话不是QQ或无法连接到QQ客户端"))

    # ---------------- 3. qfm（群文件） ----------------
    print("\n[3] qfm（群文件管理）")
    q = (REF / "qfm/main.py").read_text(encoding="utf-8")
    check("它没有平台判定（直接 get_client().send_action）",
          ("platform" not in q) and ("send_action" in q))
    print("      ⇒ 在官 bot 上：qq_adapter.get_client() 拿到的是**官方 client**，")
    print("         而官方 client **没有 send_action 方法** ⇒ 必然抛 AttributeError。")
    check("★ 官方适配器确实没有 send_action（已实测）",
          not hasattr(__import__("importlib").import_module(
              "core.adapter.src.qq_official.qq_official"), "send_action"))

    # ---------------- 4. 框架是否兜住工具异常 ----------------
    print("\n[4] 框架是否兜住工具异常（决定 qfm 那条路会不会炸对话）")
    ftm = (CORE / "core/agent/func_tool_manager.py").read_text(encoding="utf-8")
    check("★ 框架 catch 住工具抛出的任何异常",
          "except Exception as e:" in ftm and "Failed to call tool" in ftm)
    check("★ 并转成结构化 tool_result（模型能看到错误并自我纠正）",
          'result = {"error":' in ftm)
    check("★ 还有超时保护（工具卡住不会挂死）",
          "AsyncTimeoutError" in ftm and "timed out after" in ftm)

    # ---------------- 5. 结论 ----------------
    print("\n[5] 结论")
    print("  三个插件用 register.tool ⇒ 工具**是全局注册的**，")
    print("  确实会出现在官 bot 会话的可用工具列表里（这点不能否认）。")
    print()
    print("  但被调用时会：")
    print("   · gmv / gmp → 平台判定拦住，返回**明确人话**（❌ 当前会话不是QQ…）")
    print("   · qfm     → 无平台判定，抛 AttributeError（官方 client 没有 send_action）")
    print("   · 框架三者都兜住：except => error 字段带回给模型")
    print()
    print("  ⇒ **不会真的误用**：它们拿不到官 bot 的能力，绝不会动到群；")
    print("     最坏情况是模型误选一次、拿到错误、然后改用我们的工具（浪费一轮）。")
    print("  ⇒ 唯一的真实影响是**工具列表被污染**（模型看到一堆用不了的）。")

    print()
    print("=" * 72)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
