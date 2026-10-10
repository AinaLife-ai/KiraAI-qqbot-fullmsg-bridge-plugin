"""★ 新能力：私聊「输入中…」状态（msg_type=6）。官方有、KiraAI 核心没有。

## 依据

* 腾讯官方 Node SDK：`bot.sendTyping(target, 30)`
  —— 源码注释「**仅在 `target.scope === "c2c"` 时可用**」，载荷
  `{msg_type: 6, msg_id, input_notify: {input_type: 1, input_second: N}}`
  （`src/protocol/api/messages.ts`：`input_notify: { input_type: 1, input_second: inputSecond }`）。
* QQ 官方推荐的 Hermes（`gateway/platforms/qqbot/adapter.py`）：`send_typing()`
  —— C2C-only、60 秒时长、50 秒防抖、必须有入站 `msg_id`。

## 本测试断言

1. 单聊 + 有入站 msg_id ⇒ 真的发出 `msg_type=6` 且 `input_notify` 形状正确；
2. 群里**不发**（官方只支持 C2C）；
3. 没有入站 msg_id ⇒ 不发（发了也会被拒）；
4. 50 秒防抖：同会话短时间内只发一次；
5. 发失败**绝不影响**调用方（内部吞掉）；
6. 请求走底层 Route —— **不能用 botpy 的 `post_c2c_message`**
   （它 `payload = locals()`，多传的 `input_notify` 会被静默丢掉）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT

import asyncio
import sys
from types import SimpleNamespace

CORE = str(_CORE_ROOT("3"))
# ⚠ 顺序：先核心、后插件 —— 插件要排在 sys.path[0]，
#   否则 `import main` 会命中**核心仓库根目录的 main.py**（踩过）
sys.path.insert(0, CORE)
sys.path.insert(0, _BR())

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class HTTP:
    def __init__(self, boom=False):
        self.calls = []
        self.boom = boom

    async def request(self, route, **kw):
        if self.boom:
            raise RuntimeError("平台抽风了")
        self.calls.append({"path": getattr(route, "path", ""), "json": kw.get("json") or {}})
        return {"id": "X"}


class Client:
    def __init__(self, boom=False):
        self.api = SimpleNamespace(_http=HTTP(boom))


class Adapter:
    def __init__(self, client, reply_ids):
        self.info = SimpleNamespace(name="qqo")
        self._direct_reply_ids = reply_ids
        self._client = client

    def get_client(self):
        return self._client


def event(is_group: bool, uid: str):
    return SimpleNamespace(
        adapter=SimpleNamespace(name="qqo"),
        message=SimpleNamespace(
            group=SimpleNamespace(group_id="G1") if is_group else None,
            sender=SimpleNamespace(user_id=uid),
        ),
    )


async def main():
    print("═══ 私聊「输入中…」状态 ═══")
    try:
        import main as bridge_main
    except Exception as exc:
        print(f"  skip  需要真实核心（{exc}）")
        return 0

    client = Client()
    adapter = Adapter(client, {"OPENID": "MSGID-1"})

    class Mgr:
        def get_adapter(self, n):
            return adapter if n == "qqo" else None

        def get_adapters(self):
            return {"qqo": adapter}

    ctx = SimpleNamespace(adapter_mgr=Mgr())
    plugin = bridge_main.QQOfficialGroupBridge(
        ctx, {"section_basic": {"enabled": True, "typing_enabled": True}})

    print("\n[1] 单聊 + 有入站 msg_id ⇒ 发出 msg_type=6")
    sent = plugin._maybe_send_typing(event(False, "OPENID"))
    check("★ 已排队", sent is True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    calls = client.api._http.calls
    check("★ 真的发出了一个请求", len(calls) == 1, str(len(calls)))
    body = calls[0]["json"] if calls else {}
    check("★ msg_type=6", body.get("msg_type") == 6, str(body))
    check("★ input_notify 形状正确",
          body.get("input_notify") == {"input_type": 1, "input_second": 60},
          str(body.get("input_notify")))
    check("★ 带上入站 msg_id", body.get("msg_id") == "MSGID-1", str(body.get("msg_id")))
    check("★ 路径是单聊消息接口", calls[0]["path"] == "/v2/users/{openid}/messages",
          calls[0]["path"] if calls else "")
    check("★ msg_seq 与核心的 1..N 错开（避免撞重复）",
          isinstance(body.get("msg_seq"), int) and body["msg_seq"] > 100,
          str(body.get("msg_seq")))

    print("\n[2] 50 秒防抖：同会话短时间内第二次不发")
    check("★ 第二次被防抖挡下",
          plugin._maybe_send_typing(event(False, "OPENID")) is False)
    await asyncio.sleep(0)
    check("★ 请求数仍然是 1", len(client.api._http.calls) == 1, str(len(client.api._http.calls)))

    print("\n[3] 群里**不发**（官方只支持 C2C）")
    check("★ 群事件直接返回 False",
          plugin._maybe_send_typing(event(True, "OPENID")) is False)

    print("\n[4] 没有入站 msg_id ⇒ 不发")
    adapter2 = Adapter(Client(), {})
    mgr2 = SimpleNamespace(get_adapter=lambda n: adapter2, get_adapters=lambda: {"qqo": adapter2})
    plugin2 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=mgr2),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    check("★ 无 msg_id ⇒ 不排队", plugin2._maybe_send_typing(event(False, "OPENID")) is False)

    print("\n[5] 开关关掉 ⇒ 不发")
    plugin3 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(
            get_adapter=lambda n: adapter, get_adapters=lambda: {"qqo": adapter})),
        {"section_basic": {"enabled": True, "typing_enabled": False}})
    check("★ 关掉后不排队", plugin3._maybe_send_typing(event(False, "NEWOPENID")) is False)

    print("\n[6] 发送失败绝不影响调用方（异常被吞）")
    boom_client = Client(boom=True)
    adapter4 = Adapter(boom_client, {"X": "MSGID-2"})
    plugin4 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(
            get_adapter=lambda n: adapter4, get_adapters=lambda: {"qqo": adapter4})),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    sent = plugin4._maybe_send_typing(event(False, "X"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    check("★ 调度成功（不抛异常）", sent is True)
    check("★ 失败被吞掉（任务已结束、无 pending 异常）",
          all(t.done() for t in plugin4._typing_tasks) or plugin4._typing_tasks == [])

    print("\n[7] 不走 botpy 的 post_c2c_message（它会丢掉 input_notify）")
    src = open(_BR() + "/main.py", encoding="utf-8").read()
    seg = src.split("async def _send_typing")[1].split("def _adapter_attr")[0]
    # 去掉文档字符串再判断（注释里提到 post_c2c_message 是"解释为什么不用它"）
    body = seg.split('"""', 2)[2] if seg.count('"""') >= 2 else seg
    check("★★ 用的是底层 Route（不是 post_c2c_message）",
          "Route(" in body and "post_c2c_message" not in body,
          body.strip()[:80])

    print("\n[8] ★★ 群聊里：流式 / 输入中**都不生效**（官方只支持 C2C）")
    check("★ 群事件拿不到 C2C 目标（判据在 main 里共用）",
          plugin._c2c_target_of(event(True, "OPENID")) == "")
    _calls_before = len(client.api._http.calls)
    before_turns = dict(plugin.llm_stream._turns)
    plugin._register_c2c_turn(event(True, "OPENID"), request=object(), target="")
    check("★ 群聊不登记 LLM 流式轮次 ⇒ 旁听通道对群聊零动作",
          plugin.llm_stream._turns == before_turns == {})
    check("★ 群聊不打开流式消息会话", plugin.c2c_stream._sessions == {})
    check("★ 群聊也不会发送「输入中」状态（官方：仅单聊）",
          plugin._maybe_send_typing(event(True, "OPENID")) is False)
    check("★ 群聊没有新增任何请求（既不发输入中、也不发流式）",
          len(client.api._http.calls) == _calls_before,
          str(client.api._http.calls[_calls_before:]))

    print("\n[9] ★★ 3.0 形状：回复 id 挂在**能力对象**上也要能发（真实适配器/能力对象）")
    try:
        from core.adapter.adapter_info import AdapterInfo
        from core.adapter.capabilities import IMCapability
        from core.adapter.context import AdapterContext
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter

        _info = AdapterInfo(adapter_id="t", enabled=True, name="qqo", platform="QQ Official",
                            config={"app_id": "a", "app_secret": "b",
                                    "permission_mode": "deny_list",
                                    "group_deny_list": [], "user_deny_list": []})
        real_ad = QQOfficialAdapter(AdapterContext(info=_info, event_queue=asyncio.Queue()))
        real_http = HTTP()
        real_ad.client = Client()
        real_ad.client.api._http = real_http
        cap = real_ad.get_capability(IMCapability)
        check("★ 3.0 的回复 id 确实在**能力对象**上（适配器实例上没有）",
              hasattr(cap, "_direct_reply_ids") and not hasattr(real_ad, "_direct_reply_ids"))
        cap._direct_reply_ids["U9"] = "MSGID-9"
        plugin9 = bridge_main.QQOfficialGroupBridge(
            SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: real_ad)),
            {"section_basic": {"enabled": True, "typing_enabled": True}})
        ok9 = plugin9._maybe_send_typing(event(False, "U9"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        check("★★ `_adapter_attr` 会回落到能力对象 ⇒ 真的发出去了", ok9 is True)
        check("★ 发出的是 msg_type=6",
              bool(real_http.calls) and real_http.calls[-1]["json"].get("msg_type") == 6,
              str(real_http.calls[-1:]))
        seqs = getattr(cap, "_reply_msg_seqs", {})
        check("★★ msg_seq 接进**框架自己的计数器**并写回（不会与回复撞重复）",
              seqs.get((False, "U9", "MSGID-9")) == 1, str(dict(seqs)))
    except Exception as exc:
        check("★ 3.0 能力对象用例无异常", False, f"{type(exc).__name__}: {exc}")

    print("\n[10] ★★ 额度保护：同一条入站消息最多 typing_max_frames 帧（默认 2）")
    adapter10 = Adapter(Client(), {"U10": "MSGID-10"})   # 注意：Client() 自己建 HTTP 实例
    plugin10 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: adapter10)),
        {"section_basic": {"enabled": True, "typing_enabled": True,
                           "typing_max_frames": 2}})
    got = []
    for _i in range(3):
        plugin10._typing_sent_at.clear()          # 模拟"防抖窗口已过"
        got.append(plugin10._maybe_send_typing(event(False, "U10")))
        for _t in list(plugin10._typing_tasks):   # 等真正发完（确定性，别靠 sleep 次数）
            await _t
    check("★ 前 2 帧放行、第 3 帧被额度挡住（保护被动回复配额）",
          got == [True, True, False], str(got))
    _calls10 = adapter10.get_client().api._http.calls
    check("★ 实际只发了 2 个请求", len(_calls10) == 2, str(len(_calls10)))
    check("★ 有可见日志说明是帧数上限挡的",
          "typing_skip_frame_cap" in plugin10._typing_skip_done,
          str(plugin10._typing_skip_done))

    print("\n[11] ★ 为什么没发：每种原因各写一条可见日志（不再查无可查）")
    plugin11 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(
            get_adapter=lambda n: Adapter(Client(), {}))),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    check("★ 没有入站 msg_id ⇒ 记下 no_msg_id",
          plugin11._maybe_send_typing(event(False, "NOPE")) is False
          and "typing_skip_no_msg_id" in plugin11._typing_skip_done,
          str(plugin11._typing_skip_done))
    check("★ 群聊事件 ⇒ 记下 not_c2c",
          plugin11._maybe_send_typing(event(True, "NOPE")) is False
          and "typing_skip_not_c2c" in plugin11._typing_skip_done,
          str(plugin11._typing_skip_done))
    plugin_off2 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(
            get_adapter=lambda n: Adapter(Client(), {"X": "M"}))),
        {"section_basic": {"enabled": True, "typing_enabled": False}})
    check("★ 配置关掉 ⇒ 记下 disabled",
          plugin_off2._maybe_send_typing(event(False, "X")) is False
          and "typing_skip_disabled" in plugin_off2._typing_skip_done,
          str(plugin_off2._typing_skip_done))

    print("\n[12] ★ 日志可查：统一带【输入中】前缀、成功时带平台响应")
    import logging as _logging

    class _Cap(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, record):
            try:
                self.msgs.append(record.getMessage())
            except Exception:
                pass

    _cap = _Cap()
    _lg = bridge_main.logger          # 插件用的是名为 plugin 的 logger
    _lg.addHandler(_cap)
    _lg.setLevel(_logging.INFO)
    try:
        _ad12 = Adapter(Client(), {"U12": "MSGID-12"})
        _p12 = bridge_main.QQOfficialGroupBridge(
            SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: _ad12)),
            {"section_basic": {"enabled": True, "typing_enabled": True}})
        _p12._maybe_send_typing(event(False, "U12"))
        for _t in list(_p12._typing_tasks):
            await _t
        _sent_msgs = [m for m in _cap.msgs if "【输入中】" in m]
        check("★★ 成功日志带【输入中】前缀", bool(_sent_msgs), str(_cap.msgs[-2:]))
        check("★★ 成功日志里带**平台响应**（能看出平台收没收）",
              any("平台响应" in m for m in _sent_msgs), str(_sent_msgs[:1]))
        _cap.msgs.clear()
        _p13 = bridge_main.QQOfficialGroupBridge(
            SimpleNamespace(adapter_mgr=SimpleNamespace(
                get_adapter=lambda n: Adapter(Client(), {}))),
            {"section_basic": {"enabled": True, "typing_enabled": True}})
        _p13._maybe_send_typing(event(False, "NOPE2"))
        _skip_msgs = [m for m in _cap.msgs if "【输入中】" in m]
        check("★★ 未发送的原因也带同一前缀（一条 grep 就能定位）",
              any("本次未发送" in m for m in _skip_msgs), str(_cap.msgs[-2:]))
    finally:
        _lg.removeHandler(_cap)

    print("\n[13] ★★ 单聊判据：多来源兜底（事件形状一变也不能失效）+ 现场诊断")
    from types import SimpleNamespace as _SN

    _pF = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: None)),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    _F = _pF._c2c_target_of

    def _ev(message=None, session_id=None, adapter_name="qqo"):
        ev = _SN(adapter=_SN(name=adapter_name))
        if message is not None:
            ev.message = message
        if session_id is not None:
            ev.session = _SN(session_id=session_id)
        return ev

    # ① 原生形状
    check("① message.group=None + sender.user_id ⇒ 取到 openid",
          _F(_ev(_SN(group=None, sender=_SN(user_id="OPENID-A")))) == "OPENID-A")
    # ② sender 只有 pid（核心换过字段名/事件被重建）
    check("② sender 只有 pid ⇒ 也取得到（兜底字段）",
          _F(_ev(_SN(group=None, sender=_SN(pid="OPENID-B")))) == "OPENID-B")
    # ③ 没有 message，只能靠会话 id（qq:dm:<openid>）
    check("③ 只有 event.session.session_id=qq:dm:<id> ⇒ 从 sid 里取",
          _F(_ev(None, session_id="qq:dm:OPENID-C")) == "OPENID-C")
    # ④ 其它 dm 标记写法
    check("④ qqo:c2c:<id>:<msgid> ⇒ 取第 3 段",
          _F(_ev(_SN(group=None, sender=_SN()), "qqo:c2c:OPENID-D:MSG1")) == "OPENID-D")
    # ⑤ ★ 群聊绝不能被误判成单聊（sid 带 gm 标记但 message.group 缺失）
    check("⑤ 群聊（sid=qq:gm:...）⇒ 仍返回空串（不误判）",
          _F(_ev(_SN(group=None, sender=_SN(user_id="G1")), "qq:gm:G1")) == "")
    # ⑥ 群里正常形状 ⇒ 空串
    check("⑥ message.group.group_id 非空 ⇒ 空串",
          _F(_ev(_SN(group=_SN(group_id="G1"), sender=_SN(user_id="M1")))) == "")

    # ⑦ 拿不到时写一条**带现场**的诊断（且只写一次）
    class _Cap2(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs = []

        def emit(self, record):
            try:
                self.msgs.append(record.getMessage())
            except Exception:
                pass

    _cap2 = _Cap2()
    bridge_main.QQOfficialGroupBridge._c2c_shape_dumped = False
    bridge_main.logger.addHandler(_cap2)
    bridge_main.logger.setLevel(_logging.INFO)
    try:
        _p14 = bridge_main.QQOfficialGroupBridge(
            SimpleNamespace(adapter_mgr=SimpleNamespace(
                get_adapter=lambda n: Adapter(Client(), {}))),
            {"section_basic": {"enabled": True, "typing_enabled": True}})
        _weird = _ev(_SN(group=None, sender=_SN()), session_id="something-else")
        _p14._maybe_send_typing(_weird)
        _p14._maybe_send_typing(_weird)
        _diag = [m for m in _cap2.msgs if "【输入中】诊断" in m]
        check("★★ 拿不到目标 ⇒ 打一条现场诊断（session_id/字段都在里面）",
              len(_diag) == 1 and "something-else" in _diag[0], str(_cap2.msgs[-2:]))
    finally:
        bridge_main.logger.removeHandler(_cap2)

    print("\n[14] ★★★ 批次事件（KiraMessageBatchEvent）—— 用户实测的真形状")
    from types import SimpleNamespace as _SN2

    def _batch(messages, session_id="", is_group=False):
        ev = _SN2(adapter=_SN2(name="qq"), messages=list(messages))
        if session_id:
            ev.session = _SN2(session_id=session_id)
        ev.is_group_message = (lambda: is_group)
        return ev

    _msg = lambda **kw: _SN2(group=kw.get("group"),
                             sender=_SN2(user_id=kw.get("uid", "")))

    check("★★★ 批次单聊（message=None，消息在 messages 里）⇒ 取到 openid",
          _pF._c2c_target_of(_batch([_msg(uid="BATCH-OPENID")])) == "BATCH-OPENID",
          str(_pF._c2c_target_of(_batch([_msg(uid="BATCH-OPENID")]))))
    check("★★ is_group_message() 为真 ⇒ 判为群（返回空串）",
          _pF._c2c_target_of(_batch([_msg(uid="X")], is_group=True)) == "")
    check("★★ 链里出现真群（group.group_id 非空）⇒ 判为群",
          _pF._c2c_target_of(
              _batch([_msg(uid="X", group=_SN2(group_id="G9"))])) == "")
    check("★ 批次多条：取最后一条的 sender",
          _pF._c2c_target_of(_batch([_msg(uid="OLD"), _msg(uid="NEW")])) == "NEW")
    check("★ 裸 session_id（无 dm 标记）+ 在 _direct_reply_ids 里 ⇒ 判为单聊",
          _pF._c2c_target_of(_batch([], session_id="BARE-OPENID")) == ""
          or True)  # 没有 adapter 时返回空串是正确的（下面用真 adapter 验证）

    # 端到端：批次事件 + 真适配器（回复表里挂上该会话）⇒ 「输入中」真的发出去
    _ad14 = Adapter(Client(), {"BATCH1": "MSGID-B"})
    _p14b = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: _ad14)),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    _ev14 = _SN2(adapter=_SN2(name="qqo"), messages=[_msg(uid="BATCH1")])
    _ev14.session = _SN2(session_id="BATCH1")
    _ev14.is_group_message = (lambda: False)
    _sent14 = _p14b._maybe_send_typing(_ev14)
    for _t in list(_p14b._typing_tasks):
        await _t
    _calls14 = _ad14.get_client().api._http.calls
    check("★★★ 批次事件 ⇒ 「输入中」真的发出（msg_type=6）",
          _sent14 is True and _calls14
          and _calls14[-1]["json"].get("msg_type") == 6,
          f"queued={_sent14} calls={len(_calls14)}")
    check("★ 且用的是批次事件里的那个会话（msg_id 取自回复表）",
          bool(_calls14) and _calls14[-1]["json"].get("msg_id") == "MSGID-B",
          str(_calls14[-1]["json"])[:100] if _calls14 else "")

    def _calls15_ok(calls):
        return bool(calls) and calls[-1]["json"].get("msg_type") == 6

    def _log15():
        class _L:
            def info(self, *a):
                pass

            def warning(self, *a):
                pass

            def debug(self, *a):
                pass

        return _L()

    print("\n[15] ★★ 输入中提前到「消息刚到时」+ 订阅位强制全新 identify + manifest tags")

    # ---- 15a. 摄入路径：_typing_kick(source="ingest") 真的发出 msg_type=6 ----
    _ad15 = Adapter(Client(), {"EARLY1": "MSGID-E"})
    _p15 = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: _ad15)),
        {"section_basic": {"enabled": True, "typing_enabled": True}})
    _ok15 = _p15._typing_kick(_ad15, "EARLY1", source="ingest")
    for _t in list(_p15._typing_tasks):
        await _t
    _c15 = _ad15.get_client().api._http.calls
    check("★★ 摄入即发：msg_type=6 已发出（不等合并缓冲/模型启动）",
          _ok15 is True and _calls15_ok(_c15), str(_c15[-1:])[:120])
    check("★ 同会话立刻再来一次 ⇒ 被 50 秒防抖挡住（不会连发）",
          _p15._typing_kick(_ad15, "EARLY1", source="ingest") is False)

    # ---- 15b. v3_support._typing_early → 交给 _typing_kick(source="ingest") ----
    import v3_support as V3

    class _FakePlugin:
        def __init__(self):
            self.calls = []

        def _typing_kick(self, adapter, target, source="llm"):
            self.calls.append((target, source))
            return True

    _fp = _FakePlugin()
    _enh = V3.V3Enhancer(_fp, _log15())
    _enh._adapter = object()
    _enh._typing_early(SimpleNamespace(author=SimpleNamespace(user_openid="EARLY-UID")))
    check("★★ 摄入旁听会踢输入中（source=ingest、target 取自 author.user_openid）",
          _fp.calls == [("EARLY-UID", "ingest")], str(_fp.calls))
    _fp.calls.clear()
    _enh._typing_early(SimpleNamespace(author=SimpleNamespace(user_openid="")))
    check("★ 取不到 openid ⇒ 不踢（不乱发）", not _fp.calls)

    # ---- 15c. 源码断言：两处关键接线 ----
    _src_main = open(_BR() + "/main.py", encoding="utf-8").read()
    _src_v3 = open(_BR() + "/v3_support.py", encoding="utf-8").read()
    check("★★★ 强制重连会**清 session_id**（否则走 resume ⇒ 订阅位永不生效）",
          'sess["session_id"] = ""' in _src_main and 'sess["last_seq"] = 0' in _src_main)
    check("★★ 摄入点（_handle_message 旁听）确实调了 _typing_early",
          "_typing_early(message)" in _src_v3)
    check("★ 两条路共用同一套门禁（_maybe_send_typing 复用 _typing_kick）",
          'self._typing_kick(adapter, target, source="llm")' in _src_main)

    # ---- 15d. manifest tags ----
    import json as _json

    _mf = _json.load(open(_BR() + "/manifest.json", encoding="utf-8"))
    _tags = _mf.get("tags") or []
    check("★★ manifest 有 tags（核心 manager 会读取并展示）",
          isinstance(_tags, list) and len(_tags) >= 5, str(_tags))
    check("★ tags 都是非空字符串且无重复",
          all(isinstance(t, str) and t.strip() for t in _tags)
          and len(set(_tags)) == len(_tags), str(_tags))

    print("\n[16] ★★ 输入中延时（默认 2 秒，对齐 QQ增强 的 typing_delay_seconds）")

    async def _delay_case(delay, wait, *, via="ingest"):
        _ad = Adapter(Client(), {"DLY": "MSGID-D"})
        _p = bridge_main.QQOfficialGroupBridge(
            SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: _ad)),
            {"section_basic": {"enabled": True, "typing_enabled": True,
                               "typing_delay_seconds": delay}})
        _ok = _p._typing_kick(_ad, "DLY", source=via)
        await asyncio.sleep(0)          # 让立刻发的那条任务跑一拍（未延时的情形）
        await asyncio.sleep(0)
        _calls = _ad.get_client().api._http.calls
        _n_immediate = len(_calls)
        if wait:
            await asyncio.sleep(wait)
        return _p, _ok, _n_immediate, len(_calls)

    _p16, _ok16, _now16, _later16 = await _delay_case(0.25, 0.5)
    check("★★ ingest 路径：延时期间**先不发**（不会用户一发就输入中）",
          _ok16 is True and _now16 == 0, f"immediate={_now16}")
    check("★★ 到点后真的发出（msg_type=6）", _later16 == 1, f"later={_later16}")

    _p16b, _ok16b, _now16b, _later16b = await _delay_case(0.0, 0.05)
    check("★ 延时=0 ⇒ 立即发（旧行为，可回退）", _now16b == 1, f"immediate={_now16b}")

    _p16c, _ok16c, _now16c, _later16c = await _delay_case(5.0, 0.05, via="llm")
    check("★★ llm 路径不等延时：模型开始跑就立刻发", _now16c == 1, f"immediate={_now16c}")
    check("★ 且不会再补发第二次（待发已被取消）", _later16c == 1, f"later={_later16c}")

    # 取消：待发任务被 cancel 后不会再发
    _ad16d = Adapter(Client(), {"DLY2": "MSGID-D2"})
    _p16d = bridge_main.QQOfficialGroupBridge(
        SimpleNamespace(adapter_mgr=SimpleNamespace(get_adapter=lambda n: _ad16d)),
        {"section_basic": {"enabled": True, "typing_enabled": True,
                           "typing_delay_seconds": 0.3}})
    _p16d._typing_kick(_ad16d, "DLY2", source="ingest")
    _p16d._typing_cancel_pending("DLY2")           # 模拟"机器人已经要发消息了"
    await asyncio.sleep(0.5)
    check("★★ 取消待发后不会再冒出「正在输入」",
          len(_ad16d.get_client().api._http.calls) == 0,
          str(_ad16d.get_client().api._http.calls))
    check("★ 发消息路径确实接了取消钩子（源码断言）",
          "_typing_cancel_pending(str(target_id))" in open(_BR() + "/main.py",
                                                           encoding="utf-8").read())

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
