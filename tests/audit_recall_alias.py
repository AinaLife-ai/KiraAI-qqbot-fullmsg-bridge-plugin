"""撤回「图片/表情消息」失败（40061001）的根因与修复（用户 2026-10-07 反馈）。

现象（用户截图）：
    [tool_use] recall_qq_msg args: {'message_id': 'qqo-ad248e7d21'}
    接口请求异常…错误代码: 400, {'message': '请求参数无效', 'code': 40061001}
    → 撤回失败：请求参数无效

## 根因（三层，逐层实测）

### 层 1：展示态 id 必须反查成真实 id
模型看到的是 `qqo-xxxx`（真实 id 的 sha256 前 10 位），官方要真实 id。
反查表是 `adapter._reply_id_aliases[(is_group, target, display_id)] -> raw`。
**查得到就成功，查不到就把 qqo-xxx 直接发出去 ⇒ 40061001。**

### 层 2：这张表是**内存态**，条目会丢
* 每条会话最多保留 **100** 条（`QQ_OFFICIAL_MAX_REPLY_IDS_PER_CONVERSATION`）；
* 表在适配器实例上 ⇒ **重启 KiraAI 就清空**。

### 层 3：★ 我们自己**顶掉了**核心的事件处理器（2.x）
`allow_shadow=True`（见 main.py 的说明）意味着 `on_group_message_create`
不再执行核心实现 ⇒ 核心在事件路径里的 `_remember_reply_id`
**也不会执行**；只剩我们 `build_event` 里补的那一次登记（只登记"本条消息"）。

再加一条我们自己的缺口：**主动消息通道绕过适配器**直接打 `client.api`
（`main.py` 的 `_send_proactive`），核心的发送后登记（line 523）不会发生。

## 修复

1. `_to_raw_message_id` 现在返回 `(raw_id, found)`；
2. 查不到映射且官方回 40061001 时，给出**可操作提示**（而不是干巴巴报错）：
   说明"记录已清掉、这条撤不回来了、别反复重试、可让群管理员手动撤"；
3. 反查时**顺带扫会话级 LRU**（`_reply_alias_lrus`），多一个兜底；
4. **主动消息发送后补登记**别名（补上我们自己的缺口）。
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", "/var/minis/workspace/qqbot_bridge_review/kira-core"))
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))
_BOTPY = os.environ.get("BOTPY_PATH", "/tmp/botpy_src/botpy-master")
if os.path.isdir(_BOTPY):
    sys.path.insert(0, _BOTPY)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def _mk(adapter_cls, info):
    if os.environ.get("KIRA_CORE_GEN") == "3":
        from core.adapter.context import AdapterContext
        return adapter_cls(AdapterContext(info=info, event_queue=asyncio.Queue()))
    return adapter_cls(info, asyncio.Queue())


def main():
    print("=" * 74)
    print("## 撤回失败(40061001) 根因与修复")
    print("=" * 74)

    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
    import admin_tools as AT

    calls = []

    class FakeHTTP:
        async def request(self, route, **kw):
            calls.append(route.url)
            if "qqo-" in route.url:
                # 官方对展示态 id 的真实反应
                raise RuntimeError('{"message":"请求参数无效","code":40061001}')
            return {}

    info = AdapterInfo(adapter_id="t", enabled=True, name="qq",
                       platform="QQ Official Bot",
                       config={"app_id": "a", "app_secret": "b",
                               "permission_mode": "deny_list",
                               "group_deny_list": [], "user_deny_list": []})
    adapter = _mk(QQOfficialAdapter, info)
    adapter.client = type("C", (), {})()
    adapter.client.api = type("A", (), {})()
    adapter.client.api._http = FakeHTTP()

    # ★ 3.0 把别名表与 _display_message_id 放在**能力对象**
    #   （`QQOfficialIMCapability`）上，不在 adapter 实例上 —— 取出来统一用。
    holder = adapter
    if not hasattr(adapter, "_display_message_id"):
        try:
            from core.adapter.capabilities import IMCapability
            holder = adapter.get_capability(IMCapability) or adapter
        except Exception:
            holder = adapter
    globals()["HOLDER"] = holder

    class E:
        class session:
            adapter_name = "qq"
            session_type = "gm"
            session_id = "G1"

        def is_group_message(self):
            return True

    class Ctx:
        class M:
            def get_adapter(self, n):
                return adapter
        adapter_mgr = M()

        def __getattr__(self, n):
            return None

    tool = [t for t in AT.build_tools({"recall_enabled": True})
            if t.name == "recall_qq_msg"][0](ctx=Ctx())
    loop = asyncio.new_event_loop()

    RAW = "ROBOT1.0_REALID_ABC"
    DIS = holder._display_message_id(RAW)

    # ---------------- 1. 命中：反查成真实 id ----------------
    print("\n[1] 表里有映射 → 反查成功")
    HOLDER._reply_id_aliases[(True, "G1", DIS)] = RAW
    calls.clear()
    r = loop.run_until_complete(tool.execute(E(), message_id=DIS))
    used = calls[-1].rsplit("/", 1)[-1]
    check("★ 用的是**真实 id**（不是 qqo-xxx）", used == RAW, used)
    check("撤回成功", "成功" in r, r)

    # ---------------- 2. 未命中：给可操作提示 ----------------
    print("\n[2] 表里没有（模拟重启 / 超过 100 条被淘汰）→ 应给可操作提示")
    HOLDER._reply_id_aliases.clear()
    calls.clear()
    r = loop.run_until_complete(tool.execute(E(), message_id="qqo-ad248e7d21"))
    for ln in r.splitlines():
        print("    " + ln)
    check("★ 说明「原始 ID 已经查不到」（而不是只说'参数无效'）",
          "查不到" in r, r)
    check("★ 给出原因（记录有限/重启清空）",
          "100" in r and "重启" in r, r)
    check("★ 给出可操作建议（撤不回来/让管理员手动撤）",
          "撤不回来" in r and "管理员" in r, r)
    check("★ 明确说「不要反复重试」（省 token）", "不要反复重试" in r, r)

    # ---------------- 3. found 标记语义 ----------------
    print("\n[3] _to_raw_message_id 的 found 标记")
    got, found = tool._to_raw_message_id(E(), "G1", DIS, True)
    check("未命中 → found=False", found is False, str(found))
    HOLDER._reply_id_aliases[(True, "G1", DIS)] = RAW
    got, found = tool._to_raw_message_id(E(), "G1", DIS, True)
    check("命中 → found=True 且返回真实 id", found is True and got == RAW, str((got, found)))
    got, found = tool._to_raw_message_id(E(), "G1", "ROBOT1.0_RAW", True)
    check("★ 传真实 id 时视为命中（照用，不误判）", found is True and got == "ROBOT1.0_RAW")

    # ---------------- 4. 会话级 LRU 兜底 ----------------
    print("\n[4] 主表被淘汰时，会话级 LRU 兜底")
    HOLDER._reply_id_aliases.clear()
    HOLDER._reply_alias_lrus[(True, "G1")] = {DIS: RAW}
    got, found = tool._to_raw_message_id(E(), "G1", DIS, True)
    check("★ 主表空了也能从 LRU 找到", found is True and got == RAW, str((got, found)))

    # ---------------- 5. 表本身的限制（说明为什么必然会发生） ----------------
    print("\n[5] 别名表的固有限制（说明这个失败是**必然**会遇到的）")
    # 2.x 的发送链在 qq_official.py；3.0 拆到了 im.py —— 两版都读
    _base = CORE / "core/adapter/src/qq_official"
    core_src = "\n".join(
        f.read_text(encoding="utf-8") for f in (_base / "qq_official.py", _base / "im.py")
        if f.is_file())
    check("★ 每条会话的别名有上限（会淘汰旧的）",
          "MAX_REPLY_IDS_PER_CONVERSATION" in core_src)
    check("★ 别名表是实例属性 ⇒ 重启即清空",
          "self._reply_id_aliases" in core_src)

    # ---------------- 6. 我们的缺口：主动通道补登记 ----------------
    print("\n[6] 我们自己的缺口：主动消息发送后要补登记")
    our = (ROOT / "main.py").read_text(encoding="utf-8")
    lines = our.splitlines()
    # 找**紧跟着** return KiraIMSentResult(message_id=message_id) 的那一处
    # （主动通道的收尾），确认它前面有登记调用
    hit = False
    for i, ln in enumerate(lines):
        if "主动消息已发送" in ln and "return KiraIMSentResult(message_id=message_id)" in \
                "\n".join(lines[i:i + 3]):
            back = "\n".join(lines[max(0, i - 12):i])
            hit = "_remember_reply_id" in back
            break
    check("★ 主动通道里补了 _remember_reply_id 登记", hit, "未在主动通道收尾处找到")
    check("★ 官方撤回本来就有 2 分钟时限（所以不必落盘持久化）",
          "2 分钟" in (ROOT / "admin_tools.py").read_text(encoding="utf-8"))

    print()
    print("=" * 74)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
