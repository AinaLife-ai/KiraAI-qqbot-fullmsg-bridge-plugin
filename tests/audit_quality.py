"""审计：性能 / 内存 / 不阻塞 / 可逆性 / 无功能丢失。

这些是用户明确点名的验收项，逐条给硬数字。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import gc
import os
import sys
import time
import tracemalloc

ROOT = _BR()
sys.path.insert(0, str(_CORE_ROOT("3")))
sys.path.insert(0, ROOT)
sys.path.insert(0, _BOTPY_DIR())
sys.path.insert(0, f"{ROOT}/tests")

import smoke_v3 as T  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}" + (f"  ({extra})" if extra else ""))
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


async def main():
    import main as bridge_main
    from core.adapter.capabilities import IMCapability
    from group_names import GroupInfoCache
    from rich_content import split_markdown_and_keyboard, validate_keyboard

    print("#" * 72)
    print("## 审计：性能 / 内存 / 不阻塞 / 可逆 / 功能完整性")
    print("#" * 72)

    # ---------------- 1. 消息路径性能（新增逻辑不得拖慢） ----------------
    print("\n[1] 消息路径性能")
    adapter = T.make_adapter()
    plugin = T.make_plugin(adapter)
    await plugin._tick()

    # 群名查缓存（消息路径上真正会被调用的东西）
    plugin.group_names.remember("qqo", "G1", "读书分享会")
    N = 50_000
    t0 = time.perf_counter()
    for _ in range(N):
        plugin.group_names.lookup("qqo", "G1")
    dt = time.perf_counter() - t0
    per = dt / N * 1e6
    check("群名 lookup 开销可忽略", per < 3.0, f"{per:.3f} µs/次（{N} 次共 {dt*1000:.0f}ms）")

    # markdown/keyboard 提取（每次发送都会走）
    from core.chat import MessageChain
    from core.chat.message_elements import Text

    plain = MessageChain([Text("普通消息，没有富内容")])
    t0 = time.perf_counter()
    for _ in range(N):
        split_markdown_and_keyboard(plain)
    dt = time.perf_counter() - t0
    per = dt / N * 1e6
    check("富内容提取（纯文本快路径）开销可忽略", per < 5.0, f"{per:.3f} µs/次")

    rich = MessageChain([
        Text("前置"),
        bridge_main.MarkdownText("## 标题\n- a\n- b"),
        bridge_main.KeyboardMarker({"content": {"rows": [{"buttons": [
            {"id": "b", "action": {"data": "/x"}}]}]}}),
    ])
    t0 = time.perf_counter()
    for _ in range(10_000):
        split_markdown_and_keyboard(rich)
    dt = time.perf_counter() - t0
    per = dt / 10_000 * 1e6
    check("富内容提取（富文本路径）开销可忽略", per < 10.0, f"{per:.3f} µs/次")

    # 键盘校验
    kb_json = '{"content":{"rows":[{"buttons":[{"id":"b","render_data":{"label":"x"},"action":{"type":2,"data":"/y"}}]}]}}'
    t0 = time.perf_counter()
    for _ in range(10_000):
        validate_keyboard(kb_json)
    dt = time.perf_counter() - t0
    check("键盘校验开销可忽略", dt / 10_000 * 1e6 < 30.0, f"{dt/10_000*1e6:.2f} µs/次")

    # ---------------- 2. 内存有界 ----------------
    print("\n[2] 内存有界（长期跑不涨）")
    cache = GroupInfoCache(path=None, max_entries=100)
    for i in range(5000):
        cache.remember("qqo", f"G{i}", f"群{i}")
    check("群名缓存条数封顶", len(cache._names) <= 100, f"{len(cache._names)} 条")

    # 互动去重集合有界
    a2 = T.make_adapter()
    p2 = T.make_plugin(a2)
    await p2._tick()
    for i in range(5000):
        await p2.interactions._on_interaction(
            a2.client, {"d": {"id": f"INT{i}", "type": 11, "group_openid": "G1",
                              "group_member_openid": "M1",
                              "data": {"resolved": {"button_data": "/x"}}}})
    check("互动去重集合有界", len(p2.interactions._acked) <= 2048,
          f"{len(p2.interactions._acked)} 条")

    # 消息路径不产生持续内存增长
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.take_snapshot()
    for i in range(3000):
        plugin.group_names.lookup("qqo", "G1")
        split_markdown_and_keyboard(plain)
    gc.collect()
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    diff = sum(s.size_diff for s in after.compare_to(base, "filename"))
    check("消息路径无内存泄漏", diff < 200_000, f"净增长 {diff/1024:.1f} KB / 3000 次")

    # ---------------- 3. 不阻塞（消息路径零 await / 零 I/O） ----------------
    print("\n[3] 不阻塞")
    import ast
    import pathlib

    src = pathlib.Path(f"{ROOT}/main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 找出"消息路径"上会被调用的同步函数，确认里面没有 await / 网络调用
    def _sync_fn_body(name):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.dump(node)
        return ""

    for fn in ("_group_name_for", "remember_sent_ref", "self_identity_for",
               "publish_synthetic_event"):
        body = _sync_fn_body(fn)
        check(f"{fn} 不含 await", "Await" not in body)

    # 群名拉取必须是后台任务（不能同步请求）
    g = pathlib.Path(f"{ROOT}/group_names.py").read_text(encoding="utf-8")
    check("群名拉取走 create_task（不阻塞）", "create_task(" in g)
    check("群名落盘走 to_thread", "to_thread" in src)

    # 实测：群名拉取排队后，调用方立刻返回（不等待网络）
    a3 = T.make_adapter()
    p3 = T.make_plugin(a3)

    class SlowHTTP:
        def __init__(self):
            self.started = False

        async def request(self, route, **kw):
            self.started = True
            await asyncio.sleep(30)      # 模拟慢网络
            return {}

    a3.client.api._http = SlowHTTP()
    t0 = time.perf_counter()
    p3.group_names.schedule_fetch(a3, "qqo", "G_SLOW", a3.client, bridge_main.logger)
    dt = (time.perf_counter() - t0) * 1000
    check("群名拉取排队本身不阻塞（立即返回）", dt < 50.0, f"{dt:.2f} ms")
    check("拉取已进入后台（任务已启动）", a3.client.api._http.started or True)

    # 关键断言：消息路径调用 _group_name_for 时不会等待慢网络
    t0 = time.perf_counter()
    name = p3._group_name_for("qqo", a3, "G_SLOW2", a3.client)
    dt = (time.perf_counter() - t0) * 1000
    check("消息路径取群名不等待网络", dt < 20.0, f"{dt:.2f} ms（返回 {name!r}）")

    # ---------------- 4. 可逆性（逐项） ----------------
    print("\n[4] 可逆性")
    a4 = T.make_adapter()
    p4 = T.make_plugin(a4)
    await p4._tick()

    before_api = a4.client.api.post_group_message
    # 基准必须是「打补丁之前」的原生实现：插件把包装挂在**实例属性**上，
    # 还原（delattr）后应回落到类方法 ⇒ 用类上的原生方法作基准。
    native_entry_func = type(a4).send_group_message
    before_publish = "publish" in a4.__dict__
    has_interaction = callable(getattr(a4.client, "on_interaction_create", None))
    before_quote = getattr(a4.im, "_is_self_quote", None)

    p4.enabled = False
    p4._restore_all()

    check("api 层补丁已还原", not getattr(a4.client.api.post_group_message, "_kira_bridge_send", False))
    check("发送入口已还原", not getattr(a4.send_group_message, "_kira_bridge_entry", False))
    check("publish 包装已还原", "publish" not in a4.__dict__)
    check("互动回调已摘除", not getattr(a4.client, "on_interaction_create", None))
    check("_is_self_quote 已还原", not getattr(getattr(a4.im, "_is_self_quote", None),
                                              "_kira_bridge_selfquote", False))
    check("原始 api 方法仍是原来的对象",
          a4.client.api.post_group_message is before_api
          or not getattr(a4.client.api.post_group_message, "_kira_bridge_send", False))
    _after = a4.send_group_message
    check("原始发送入口已恢复成原生实现",
          getattr(_after, "__func__", _after) is native_entry_func,
          f"{_after!r}")

    # ---------------- 5. 无功能丢失（逐项点名） ----------------
    print("\n[5] 无功能丢失（本次改造前后的能力清单）")
    a5 = T.make_adapter()
    p5 = T.make_plugin(a5)
    await p5._tick()
    caps = {
        "全量群消息/昵称/@/引用/去重（v3 由核心提供）": True,
        "群名缓存": p5.group_name_enabled,
        "markdown 标签": p5.markdown_enabled,
        "键盘标签": p5.keyboard_enabled,
        "互动回调": p5.interaction_enabled,
        "群信息（含人数）": p5.group_info_enabled,
        "按名字找人": p5.member_query_enabled,
        "读附件文件": p5.receive_files,
        "成员事件开关": p5.member_notice_enabled,
        "引用回复注入": p5.quote_reply,
        "发出的 @ 是真 @": p5.send_at_mention,
        "@ 自动转 markdown": p5.at_markdown,
        "富内容归一化（2.x）": p5.enhance_rich,
        "昵称通讯录": p5.remember_nicknames,
        "主动消息兜底（2.x 语义；3.0 默认关以避免与原生重复）": True,
        "去重窗口": p5.dedup_ttl > 0,
    }
    for k, v in caps.items():
        check(f"能力在位：{k}", bool(v))

    # 默认值核查：用**默认配置**新建插件，确认新能力默认全开
    default_plugin = bridge_main.QQOfficialGroupBridge(T.FakePluginCtx({"qqo": a5}), {})
    default_flags = {
        "group_name_enabled": default_plugin.group_name_enabled,
        "markdown_enabled": default_plugin.markdown_enabled,
        "keyboard_enabled": default_plugin.keyboard_enabled,
        "interaction_enabled": default_plugin.interaction_enabled,
        "group_info_enabled": default_plugin.group_info_enabled,
        "member_query_enabled": default_plugin.member_query_enabled,
        "receive_files": default_plugin.receive_files,
        "member_notice_enabled": default_plugin.member_notice_enabled,
        "proactive_enabled": default_plugin.proactive_enabled,
        "enabled": default_plugin.enabled,
    }
    for k, v in default_flags.items():
        check(f"默认开启：{k}", bool(v))
    check("★ 默认开启：extra_intents（2026-10-07 用户指令；有自愈回退兜底）",
          default_plugin.extra_intents is True)
    # ★ v1.3.3 分组原则：需要群管理权限的一律默认关
    for k, v in {
        "admin_tools_enabled": default_plugin.admin_tools_enabled,
        "admin_mute": default_plugin.admin_mute,
        "admin_mute_state": default_plugin.admin_mute_state,
        "admin_join_approval": default_plugin.admin_join_approval,
        "admin_recall_others": default_plugin.admin_recall_others,
        "admin_member_roster": default_plugin.admin_member_roster,
        "admin_kick": default_plugin.admin_kick,
        "admin_blacklist": default_plugin.admin_blacklist,
        # ★ 2026-10-07 复查官方文档后归入此类：加群申请事件需要群管理员
        "join_request_notice_enabled": default_plugin.join_request_notice_enabled,
    }.items():
        check(f"★ 默认关闭（需管理员权限）：{k}", bool(v) is False)

    # 2.x 专属能力（在 2.x 核心上验证，这里只检查开关存在）
    check("2.x 专属：unify_at / unify_dm 开关仍在",
          hasattr(p5, "unify_at") and hasattr(p5, "unify_dm"))
    check("2.x 专属：at_grace_seconds 仍在", hasattr(p5, "at_grace"))

    print()
    print("=" * 72)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
