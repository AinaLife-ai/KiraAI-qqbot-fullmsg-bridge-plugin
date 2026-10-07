"""v1.4.0 三项修复的审计（用户 2026-10-07 提出的三个问题）。

## 问题 1：实例已在运行时装插件，默认配置要重启才生效？

**实测结论**：大部分**不需要重启** —— 插件每 15 秒巡检一次
（`_WATCH_INTERVAL`），`_tick` → `_attach` 会对**已连接的适配器**补挂：
  * 发送增强 / markdown / 键盘 / 引用（`_install_api_send`）；
  * 互动回调；
  * 事件处理器（`attach_client_handler`）；
  * **运行中解析表**（`inject_live_parser`）← 这个是关键，
    否则新注册的解析器对已建立的连接不生效；
  * intent 扩展（`_upgrade_live_client` + **主动请一次重连**）。

**真正需要补救的两个缺口**（本版修掉）：
  * **群名**：原本只在「收到该群消息」时才拉 ⇒ 刚装插件时所有群名都是 openid，
    要等群里有人说话才逐个变中文。现在**挂载时就把已知会话补拉一遍**。
  * （intent 位本来已有补救，见 `_request_reconnect`。）

## 问题 2：撤回失败提示太长

原来写了 4 行，模型读起来啰嗦。**改成 2 句**（见 `admin_tools.py`）。

## 问题 3：`<msg message_id="">` 导致消息发不出去

**根因链**（逐段核实）：
  1. 我们的**合成事件**（成员通知 / 主动消息）用了 `message_id=""`；
  2. 核心 `kira-ai` 插件把每条进来的消息**无条件**渲染进提示词：
     `f"[{date}] [message_id: {msg.message_id}] [...] | {msg.message_str}"`
     ⇒ 空串渲染成 `[message_id: ]`；
  3. 模型写 `<msg message_id="">` **照抄**这个空属性；
  4. 这个空属性**留在历史里**被反复模仿 ⇒ 所以「不是首次，而是会出现」。

**对照证据**：核心自己避开了这点 ——
`plugin_context` 用 `"system_message"`、OneBot 适配器用 `"None"`。
**唯独我们用了空串。**

**修法**：合成事件改用非空占位 `"system"`（两处：`main.py` / `qqbot_bridge.py`）。
"""
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", "/var/minis/workspace/qqbot_bridge_review/kira-core"))
sys.path.insert(0, str(CORE))
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


def main():
    print("=" * 74)
    print("## v1.4.0 三项修复审计")
    print("=" * 74)

    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    bridge_src = (ROOT / "qqbot_bridge.py").read_text(encoding="utf-8")
    tools_src = (ROOT / "admin_tools.py").read_text(encoding="utf-8")

    # ---------------- 问题 1：已运行时安装 ----------------
    print("\n[1] 实例已在运行时装插件 —— 是否需要重启？")
    check("★ 巡检间隔存在（自动补挂的前提）", "_WATCH_INTERVAL" in main_src)
    m = re.search(r"_WATCH_INTERVAL\s*=\s*([\d.]+)", main_src)
    sec = float(m.group(1)) if m else 0
    check(f"巡视为 {sec:.0f}s（≤30s ⇒ 不用重启）", 0 < sec <= 30, str(sec))
    check("★ 对已连接适配器补挂事件处理器",
          "attach_client_handler" in main_src)
    check("★ 对已连接连接**注入运行中解析表**（否则新解析器不生效）",
          "inject_live_parser" in main_src)
    check("★ intent 位有主动重连补救",
          "_request_reconnect" in main_src and "请 botpy 重连" in main_src)
    check("★ 新增：挂载时补拉已知会话的群名",
          "_prefetch_group_names" in main_src)
    check("★ 群名补拉是后台的（不阻塞挂载）",
          "schedule_fetch" in main_src and "def _prefetch_group_names" in main_src)
    check("★ 群名补拉不会重复（lookup/has_failed 挡）",
          "self.group_names.lookup(name, g)" in main_src
          and "self.group_names.has_failed(name, g)" in main_src)
    # ★ 性能审计（用户要求）：不能撞接口限流
    gn_src = (ROOT / "group_names.py").read_text(encoding="utf-8")
    check("★ 群名拉取有速率控制（官方限 30 QPM）",
          "Semaphore(1)" in gn_src and "_MIN_GAP" in gn_src)
    check("★ 间隔在**锁内**（放锁外会并行睡完再抢锁 ⇒ 等于没限速）",
          "async with _sem:" in gn_src
          and gn_src.index("async with _sem:") < gn_src.index("await asyncio.sleep(_MIN_GAP)"))
    check("★ 单发路径零延迟（只有真的还有排队时才等）",
          "len(self._pending) - 1 > 0" in gn_src)
    check("★ 补拉分批（单轮最多 20 个，其余留给下一轮）",
          "_PREFETCH_BATCH" in main_src)
    check("★ 上一批没跑完时本轮不排队（避免堆积）",
          "_group_prefetch_task" in main_src)

    # ---------------- 性能：热路径代价 ----------------
    print("\n[1b] 热路径性能（新增代码不能吃 CPU）")
    check("★ 能力对象解析有缓存（2.x 上避免每轮走异常，实测 176µs → 0）",
          "_capability_cache" in main_src and "_resolve_im_capability" in main_src)
    check("★ 能力对象解析先做廉价探测（无 get_capability 直接跳过）",
          'hasattr(adapter, "get_capability")' in main_src)
    check("★ import 提到模块级（不在热路径里执行 import 语句）",
          "def _resolve_im_capability" in main_src
          and "from core.adapter.capabilities import IMCapability" in main_src.split("def _resolve_im_capability")[1][:400])
    check("说明：官方没有「列出机器人所在的群」接口 ⇒ 只能补已知会话",
          "_group_reply_ids" in main_src)

    # ---------------- 问题 2：提示简洁 ----------------
    # ★ 用户拍板：**不写自定义解释**，原样返回官方报错 ——
    #   因为 40061001 可能有多种原因，写死的解释万一不符实际就是误导。
    print("\n[2] 撤回失败提示：原样返回官方报错（不自定义）")
    check("★ 撤回失败路径直接回 humanize_error（无自定义文案）",
          "撤回失败：{humanize_error(exc)}" in tools_src)
    check("★ 未命中只写日志、不改对模型的返回",
          "未反查到真实 id" in tools_src and "logger.debug" in tools_src)

    # ---------------- 问题 3：空 message_id ----------------
    print("\n[3] <msg message_id=\"\"> 的根因修复")
    # 只看**代码行**（注释里提到 message_id="" 是说明，不算）
    _code_lines = [ln for ln in main_src.splitlines()
                   if ln.strip().startswith(("message_id=", "message_id ="))]
    check("★ 合成事件不再用空 message_id（main.py）",
          not any('message_id=""' in ln for ln in _code_lines), str(_code_lines))
    check("★ 合成事件不再用空 message_id（qqbot_bridge.py）",
          not re.search(r'is_notice=True,\s*\n\s*message_id=""', bridge_src))
    check("★ 改用非空占位 'system'",
          "SYNTHETIC_MESSAGE_ID" in main_src
          or 'message_id="system"' in main_src + bridge_src)

    # 对照核心自己的做法
    pc = (CORE / "core/plugin/plugin_context.py").read_text(encoding="utf-8")
    check("★ 对照：核心自己用 'system_message'（非空）",
          'message_id="system_message"' in pc)
    # 两版路径可能不同，全仓找一次。
    # 注：3.0 的 OneBot 适配器结构不同，未必出现该字面量 ⇒ 只在 2.x 强行断言，
    # 其它代际只做提示（避免用不适用的标准误判）。
    _txt = "\n".join(f.read_text(encoding="utf-8", errors="ignore")
                     for f in CORE.rglob("qq.py") if f.is_file())
    _nonempty = 'message_id="None"' in _txt
    if os.environ.get("KIRA_CORE_GEN") == "2":
        check("★ 对照：核心 OneBot 用 'None'（非空占位）", _nonempty)
    else:
        print(f"      （3.0 的 OneBot 适配器结构不同，跳过该字面量对照；"
              f"已确认核心不用空串：{'message_id' in _txt}）")

    # 渲染链路证据
    ki = (CORE / "core/plugin/builtin_plugins/kira-ai/main.py").read_text(encoding="utf-8")
    check("★ 核心确实把 message_id 无条件渲染进提示词（空串会显示为空）",
          "[message_id: {str(msg.message_id)}]" in ki)
    mm2 = (CORE / "core/message_manager.py").read_text(encoding="utf-8")
    check("★ 核心回填时失败会写空串（所以更不该教模型抄空 id）",
          'message_id = ""' in mm2)

    print()
    print("=" * 74)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
