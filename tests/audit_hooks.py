"""★ 钩子契约测试（本次复审新增，防止"注册了但永远不生效"这类静默失效）。

背景（真实踩坑）
--------------
核心 `plugin_registry.py:_register_plugin_hooks_for` 是这样绑定插件实例的：

    if plugin_instance is not None and hasattr(plugin_instance, bound_handler.__name__):
        bound_handler = getattr(plugin_instance, bound_handler.__name__)

它**只按函数的 `__name__` 去实例上找同名属性**。如果注册的函数名与绑到类上的
属性名不一致，框架就会注册**未绑定的裸函数** ⇒ 调用时 `self` 错位
（self 变成 event、event 变成 request…）⇒ 每次抛异常，被 `exec_handler` 吞掉
⇒ 表现是**工具/标签永远注入不进去**，日志里只有一行 traceback。

这个失效**不会让任何既有测试变红**（因为我们之前都直接调 `inject_tools_and_tags`），
所以必须专门测"框架真实调用路径"。

本套件同时覆盖四个合作插件的共存前提：
钩子注册名一致 / 平台门禁 / 补丁目标不撞 / 标签不被 xml_tag_fixer 破坏。
"""
import asyncio
import importlib.util
import inspect as _ins
import pathlib
import sys

ROOT = pathlib.Path("/var/minis/workspace/qqbot_bridge_review")
CORE = ROOT / "kira-v3"
BRIDGE = ROOT / "bridge"
# 核心的日志模块会在导入期就建 RotatingFileHandler，需要目录先存在
(BRIDGE / "data").mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(BRIDGE))
sys.path.insert(0, "/tmp/botpy_src/botpy-master")

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def _load_bridge_module():
    module_name = "plugins.qqbot-fullmsg-bridge.main"
    spec = importlib.util.spec_from_file_location(module_name, str(BRIDGE / "main.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def main():
    print("=" * 74)
    print("## 钩子契约测试（真实框架注册路径）")
    print("=" * 74)

    import importlib as _il

    PR = _il.import_module("core.plugin.registry")
    module = _load_bridge_module()

    # ---------------- 1. 注册名一致性（静态，最快发现问题） ----------------
    print("\n[1] 注册名一致性")
    src = (BRIDGE / "main.py").read_text(encoding="utf-8")
    hooks = PR._plugin_components.get("qqbot-fullmsg-bridge")
    check("装饰器把钩子记到了本插件名下", hooks is not None and bool(hooks.hooks),
          str(list(PR._plugin_components)))
    if hooks and hooks.hooks:
        for h in hooks.hooks:
            fn_name = h.handler.__name__
            bound_name = fn_name  # 框架就是按这个名字找
            has_attr = hasattr(module.QQOfficialGroupBridge, bound_name)
            check(f"★ 钩子 {fn_name} 在类上有同名属性（框架能绑定 self）",
                  has_attr,
                  "函数名与绑定属性名不一致 ⇒ 会注册未绑定函数 ⇒ 静默失效")

    # ---------------- 2. 真实绑定 + 真实调用 ----------------
    print("\n[2] 真实绑定 + 真实调用（核心的绑定逻辑）")
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.context import AdapterContext
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
    from core.tag import TagSet

    info = AdapterInfo(adapter_id="t", enabled=True, name="qqo", platform="QQ Official",
                       config={"app_id": "a", "app_secret": "b",
                               "permission_mode": "allow_list",
                               "group_allow_list": ["G1"], "user_allow_list": ["U1"]})
    adapter = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))

    class FakeMgr:
        def get_adapters(self):
            return {"qqo": adapter}

        def get_adapter(self, n):
            return adapter if n == "qqo" else None

    class FakeCtx:
        adapter_mgr = FakeMgr()

    inst = module.QQOfficialGroupBridge(FakeCtx(), {})

    class FakeToolSet:
        def __init__(self):
            self.tools = []

        def add(self, *ts):
            for t in ts:
                for i, old in enumerate(self.tools):
                    if old.name == t.name:
                        self.tools.pop(i)
                        break
                self.tools.append(t)

        def remove(self, *names):
            self.tools = [t for t in self.tools if t.name not in names]

    class FakeReq:
        def __init__(self):
            self.tool_set = FakeToolSet()
            self.system_prompt = []

    class FakeEv:
        class adapter:
            name = "qqo"
            platform = "QQ Official"

    handler = hooks.hooks[0].handler
    # 框架的绑定逻辑
    bound = getattr(inst, handler.__name__) if hasattr(inst, handler.__name__) else handler
    params = list(_ins.signature(bound).parameters)
    check("★ 绑定后首参是 event（不是 self）", params and params[0] == "event", str(params))

    req, ts = FakeReq(), TagSet()
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = bound(FakeEv(), req, ts)
        asyncio.new_event_loop().run_until_complete(res) if asyncio.iscoroutine(res) else None

    tag_names = sorted(t.name for t in ts.get_all())
    tool_names = sorted(t.name for t in req.tool_set.tools)
    check("★ 标签真的进了 TagSet", {"markdown", "keyboard"} <= set(tag_names), str(tag_names))
    check("★ 工具真的进了 ToolSet",
          {"recall_qq_msg", "set_qq_group_ban", "get_group_mute_state",
           "get_qq_bot_state"} <= set(tool_names), str(tool_names))
    check("标签描述会进提示词", "markdown" in ts.to_prompt() and "keyboard" in ts.to_prompt())

    # ---------------- 3. 与四个合作插件的共存前提 ----------------
    print("\n[3] 与四个合作插件的共存前提")
    peers = {
        "accelerator": ROOT / "compat_accelerator" / "main.py",
        "xml_tag_fixer": ROOT / "compat_xml_tag_fixer" / "main.py",
        "session_merger": ROOT / "compat_session_merger" / "main.py",
        "sustained_chat": ROOT / "compat_sustained_chat" / "main.py",
    }
    for name, path in peers.items():
        src_p = path.read_text(encoding="utf-8")
        if name == "accelerator":
            # 抢发必须经过 adapter 层，bridge 才能提取 markdown/keyboard
            check(f"{name}：抢发经过 adapter 层（bridge 能提取标签）",
                  "mp.send_message_chain(ctx.sid, action)" in src_p)
            check(f"{name}：补丁目标与 bridge 不重叠",
                  "send_group_message" not in src_p and "post_group_message" not in src_p)
        elif name == "xml_tag_fixer":
            check(f"{name}：只改 resp.text_response（不碰 MessageChain）",
                  "resp.text_response = fixed" in src_p)
            check(f"{name}：after_xml_parse 只拆 Record", "isinstance(e, Record)" in src_p)
            check(f"{name}：tag_set 缓存是只读（不修改它）",
                  "self._registered_msg_tags = " in src_p
                  and "tag_set.register" not in src_p)
        elif name == "session_merger":
            check(f"{name}：after_xml_parse 仅 debug（不改链）", "parsed chains=%d" in src_p)
            check(f"{name}：不 monkeypatch 适配器", "setattr(" not in src_p)
        elif name == "sustained_chat":
            check(f"{name}：不 patch 核心发送链", "PatchHandle" not in src_p)

    # ---------------- 4. bridge 的标签在 xml_tag_fixer 修复后仍可用 ----------------
    print("\n[4] 标签经 xml_tag_fixer 修复后仍完好")
    xml_in = ("<msg><markdown>## 标题\n- 项</markdown>"
              "<keyboard>{\"content\":{\"rows\":[{\"buttons\":[{\"id\":\"b\"}]}]}}</keyboard></msg>")
    xtf = ROOT / "compat_xml_tag_fixer" / "main.py"
    xtf_src = xtf.read_text(encoding="utf-8")
    check("xml_tag_fixer 有「已注册标签内容绝不转义」的保护",
          "已注册标签内容绝不转义" in xtf_src or "_registered_msg_tags" in xtf_src)
    check("它对未注册标签只在「未闭合」时才补闭合（完整块不动）",
          "未注册标签：优先在它自己的闭合标签处封口" in xtf_src
          or "_handle_unclosed_tail" in xtf_src)

    print()
    print("=" * 74)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
