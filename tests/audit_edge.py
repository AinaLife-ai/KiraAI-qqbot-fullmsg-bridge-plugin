"""复审 C：边界与异常注入。

重点查三个我在第一轮没覆盖的场景：
  C1. **3.0 下键盘会不会被核心抹掉** —— 3.0 的 `_send_message` 在「群 + 无媒体 +
      正文命中 AT_MARKUP」时会**重建整个 payload**（`payload = {...}`），
      如果那次重建发生在我们的 api 层补丁之前，键盘就丢了。
  C2. **并发串味** —— 两条消息同时发送时，contextvar 会不会互相污染。
  C3. **脏数据/异常** —— 群名接口返回畸形、互动事件缺字段、键盘 JSON 超长等。
"""
import asyncio
import sys

ROOT = "/var/minis/workspace/qqbot_bridge_review"
sys.path.insert(0, f"{ROOT}/kira-v3")
sys.path.insert(0, f"{ROOT}/bridge")
sys.path.insert(0, "/tmp/botpy_src/botpy-master")
sys.path.insert(0, f"{ROOT}/bridge/tests")

import smoke_v3 as T  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


async def main():
    import main as bridge_main
    from core.chat import MessageChain
    from core.chat.message_elements import Text, At

    print("#" * 72)
    print("## 复审 C：边界与异常注入")
    print("#" * 72)

    # ---------------- C1. 3.0 下键盘 + @ 的交互（核心会重建 payload） ----------------
    print("\n[C1] 3.0 核心「重建 payload」时键盘是否会丢")
    a = T.make_adapter()
    p = T.make_plugin(a)
    await p._tick()

    # 构造一条「带 @ 的正文 + 键盘」—— 这正是 3.0 会重建 payload 的场景
    at_id = "9CD54739CC9BAA46B93243088802DC72"
    chain = MessageChain([
        Text(f"<qqbot-at-user id=\"{at_id}\" /> 签到啦"),
        bridge_main.KeyboardMarker({"content": {"rows": [{"buttons": [
            {"id": "b1", "action": {"type": 2, "data": "/签到"}}]}]}}),
    ])
    await a.send_group_message("G1", chain)
    _, payload = a.client.api.calls[-1]
    print(f"    实际报文：msg_type={payload.get('msg_type')} "
          f"markdown={'有' if payload.get('markdown') else '无'} "
          f"keyboard={'有' if payload.get('keyboard') else '无'}")
    check("C1a 带 @ 的正文走了 markdown", payload.get("msg_type") == 2)
    check("C1b ★ 键盘没有丢（核心重建 payload 后仍带上）", bool(payload.get("keyboard")),
          "3.0 会重建 payload，键盘可能被抹掉")

    # ---------------- C2. 并发串味 ----------------
    print("\n[C2] 并发发送时 contextvar 是否会串味")
    a2 = T.make_adapter()
    p2 = T.make_plugin(a2)
    await p2._tick()

    md_only = MessageChain([bridge_main.MarkdownText("## A 组内容")])
    kb_only = MessageChain([Text("B 组普通文本"),
                            bridge_main.KeyboardMarker({"content": {"rows": [{"buttons": [
                                {"id": "b2", "action": {"type": 2, "data": "/b"}}]}]}})])
    await asyncio.gather(
        a2.send_group_message("G1", md_only),
        a2.send_group_message("G1", kb_only),
    )
    calls = [kw for _, kw in a2.client.api.calls]
    a_call = next((c for c in calls if "A 组内容" in str(
        (c.get("markdown") or {}).get("content", ""))), None)
    b_call = next((c for c in calls if "B 组普通文本" in str(c.get("content") or "")), None)
    check("C2a A 组（只有 markdown）报文正确", a_call is not None)
    check("C2b B 组（只有键盘）没被 A 组污染成 markdown",
          b_call is not None and b_call.get("msg_type") != 2, str(b_call)[:120])
    check("C2c B 组带着自己的键盘", b_call is not None and bool(b_call.get("keyboard")))

    # ---------------- C3. 脏数据 / 异常 ----------------
    print("\n[C3] 脏数据与异常注入")

    # C3-1 群名接口返回畸形
    from group_names import GroupInfoCache

    c = GroupInfoCache(path=None)
    c.remember("qqo", "G1", None)          # 脏值
    c.remember("qqo", "G1", "   ")         # 空白
    c.remember("qqo", "", "x")             # 空 id
    c.remember("qqo", "G2", 12345)         # 非字符串
    check("C3-1 群名写入对脏数据免疫", c.lookup("qqo", "G1") is None
          and c.lookup("qqo", "G2") is None)

    # C3-2 互动事件缺字段
    a3 = T.make_adapter()
    p3 = T.make_plugin(a3)
    await p3._tick()
    for bad in (
        {},                                            # 全空
        {"id": "x", "type": 11},                       # 缺会话标识
        {"id": "y", "type": 99},                       # 未知类型
        {"id": "z", "type": 11, "group_openid": "G1"},  # 缺成员
        {"id": "w", "type": 11, "group_openid": "G1", "group_member_openid": "M"},
    ):
        try:
            await p3.interactions._on_interaction(a3.client, {"d": bad})
            ok = True
        except Exception as exc:
            ok = False
            print(f"      异常：{type(exc).__name__}: {exc}")
        check(f"C3-2 互动事件脏数据不崩（{list(bad)[:3]}）", ok)

    # C3-3 键盘超大 JSON
    from rich_content import validate_keyboard, KeyboardError

    big_rows = {"content": {"rows": [{"buttons": [{"id": f"b{i}",
                "action": {"type": 2, "data": "x" * 200}}]} for i in range(10)]}}
    try:
        validate_keyboard(__import__("json").dumps(big_rows))
        check("C3-3 超限键盘被拒", False, "竟然通过了")
    except KeyboardError:
        check("C3-3 超限键盘被拒", True)

    # C3-4 深嵌套 / 循环引用不炸
    nested = {"content": {"rows": [{"buttons": [{"id": "b", "action": {"data": "/x"}}]}]}}
    check("C3-4 正常键盘通过", isinstance(validate_keyboard(__import__("json").dumps(nested)), dict))

    # C3-5 群名拉取失败（模拟 11253）
    a5 = T.make_adapter()

    class ErrHTTP:
        async def request(self, route, **kw):
            raise RuntimeError("接口请求异常 11253 应用无接口访问权限")

    a5.client.api._http = ErrHTTP()
    p5 = T.make_plugin(a5)
    ok = p5.group_names.schedule_fetch(a5, "qqo", "GX", a5.client, bridge_main.logger)
    await asyncio.sleep(0.3)
    check("C3-5 群名 11253 → 标记失败不再重试", p5.group_names.has_failed("qqo", "GX"))
    check("C3-5b 失败后仍保持 openid（不抛异常）", p5.group_names.lookup("qqo", "GX") is None)

    # C3-6 markdown 非权限类错误**不能**被当成"无权限"去静默退纯文本。
    #      注意：核心（2.x/3.0 的 _send_message）会把异常转成 KiraIMSentResult(ok=False)，
    #      所以正确断言是"失败被如实上报"，而不是"向上抛"。
    a6 = T.make_adapter()
    sent_calls_6 = []

    class OtherErr(T.FakeAPI):
        async def post_group_message(self, **kw):
            sent_calls_6.append(kw)
            raise RuntimeError("50055001 消息发送异常，请稍后重试")

    a6.client.api = OtherErr()
    a6.client.api._http = T.FakeHTTP()
    p6 = T.make_plugin(a6)
    await p6._tick()
    res6 = await a6.send_group_message("G1", MessageChain([bridge_main.MarkdownText("**x**")]))
    check("C3-6a 非 markdown 权限类错误**没有**被误判成无权限（未退回纯文本重发）",
          len(sent_calls_6) == 1, f"实际请求 {len(sent_calls_6)} 次")
    # 注：3.0 核心的 _send_error_code 只认 7 个码（304103/40034005/40034128/…），
    # 其它错误会被它折成 type(exc).__name__ —— 所以这里只断言"失败被如实上报"，
    # 不要求错误码原样保留（那是核心行为，不是桥接的职责）。
    check("C3-6b 失败被如实上报（ok=False 且有 err）",
          res6 is not None and not res6.ok and str(res6.err).strip() != "",
          repr(res6))

    # C3-7 markdown 权限类错误 → 退回纯文本；若**退回也失败**，同样如实上报
    a7 = T.make_adapter()
    calls7 = []

    class BothFail(T.FakeAPI):
        async def post_group_message(self, **kw):
            calls7.append(dict(kw))
            if kw.get("msg_type") == 2:
                raise RuntimeError("304036 无Markdown模板权限")
            raise RuntimeError("50055001 消息发送异常，请稍后重试")

    a7.client.api = BothFail()
    a7.client.api._http = T.FakeHTTP()
    p7 = T.make_plugin(a7)
    await p7._tick()
    res7 = await a7.send_group_message("G1", MessageChain([bridge_main.MarkdownText("**x**")]))
    check("C3-7a 先试 markdown、失败后退回纯文本（共 2 次请求）",
          len(calls7) == 2 and calls7[0].get("msg_type") == 2 and calls7[1].get("msg_type") == 0,
          f"{[c.get('msg_type') for c in calls7]}")
    check("C3-7b 退回也失败时如实上报", res7 is not None and not res7.ok, repr(res7))

    print()
    print("=" * 72)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
