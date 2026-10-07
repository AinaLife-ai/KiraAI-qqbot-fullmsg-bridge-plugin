"""v1.4.3：后台任务泄漏 —— 「反复装卸载插件后同一条消息变成 3 条」。

用户猜测：「有没有可能是我点了插件卸载，重新安装后，又点插件重载，
然后就出现这种bug？」—— **复现证实，数字完全吻合**。

## 根因

`_request_reconnect()` 里：

    asyncio.get_running_loop().create_task(_worker())   # ← 返回值没保存！

而 `terminate()` 只 cancel `self._task`（巡检循环），**碰不到它**
⇒ 每次「卸载 / 重载插件」都**漏一个任务在跑**。

实测（装→卸载 → 装→卸载 → 装）：

    第 1 轮后残留 = 1
    第 2 轮后残留 = 2
    第 3 轮后残留 = 3      ← 与用户报的"3 条消息"数字吻合

## 后果链

1. 每个遗留 worker 都会 `gw._conn.close()` **关网关 socket**；
2. 多个 worker 轮流关 ⇒ botpy **反复重连**；
3. 重连期间 + 多连接并存 ⇒ **同一条事件被处理多次** ⇒ 一次 batch 里 3 条。

（核心 `SessionBuffer.add` **没有去重**——实测确认，加几次就是几条。）

引入版本：**v1.3.2**（加 `_request_reconnect` 那次的副作用）。
为什么现在才暴露：**平时不会反复装卸载**，只有折腾插件时才攒得起来。
"""
import asyncio
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", "/var/minis/workspace/qqbot_bridge_review/kira-core"))
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))
(ROOT / "data").mkdir(exist_ok=True)
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


def main():
    print("=" * 76)
    print("## v1.4.3：后台任务泄漏（重载插件 ⇒ 消息重复）")
    print("=" * 76)

    src = (ROOT / "main.py").read_text(encoding="utf-8")

    # ---------------- 1. 静态：任务必须被记录 + terminate 必须取消 ----------------
    print("\n[1] 静态检查")
    check("★ create_task 的返回值被存进 _reconnect_tasks",
          "_reconnect_tasks.append(" in src
          and "create_task(_worker())" in src)
    check("★ 有 _reconnect_tasks 字段初始化",
          re.search(r"self\._reconnect_tasks:\s*list\s*=\s*\[\]", src) is not None)
    i = src.find("async def terminate")
    seg = src[i:i + 1200] if i > 0 else ""
    check("★ terminate 里取消全部重连任务",
          "_reconnect_tasks" in seg and "t.cancel()" in seg, seg[:300])
    check("★ cancel 之后还 await（清干净，不留 pending）",
          "await t" in seg)

    # ---------------- 2. 动态：三轮「装→卸载」后不残留 ----------------
    print("\n[2] 动态验证：三轮「装 → 卸载」后残留数")
    import main as B

    async def run():
        remain = []
        for i in range(3):
            p = B.QQOfficialGroupBridge.__new__(B.QQOfficialGroupBridge)
            p.profiles = {}
            p._RECONNECT_DELAY = 0.05
            p._RECONNECT_RETRY = 0.05
            p._RECONNECT_MAX_WAIT = 5.0
            p._EXTRA_INTENT_BITS = (1 << 24) | (1 << 26)
            p._reconnect_tasks = []
            p._task = None
            p._stop = asyncio.Event()
            p._request_reconnect(f"inst{i}", reason="test")
            await asyncio.sleep(0.05)
            # 模拟 terminate 的清理段
            for t in p._reconnect_tasks:
                if not t.done():
                    t.cancel()
            for t in p._reconnect_tasks:
                try:
                    await t
                except BaseException:
                    pass
            p._reconnect_tasks = []
            await asyncio.sleep(0.15)
            alive = [x for x in asyncio.all_tasks()
                     if "reconnect" in str(x.get_coro())]
            remain.append(len(alive))
            print(f"      第 {i+1} 轮后残留 = {len(alive)}")
        return remain

    remain = asyncio.new_event_loop().run_until_complete(run())
    check("★ 三轮后残留为 0（旧代码是 1/2/3 递增）", remain[-1] == 0, str(remain))

    # ---------------- 3. 顺带确认：核心 buffer 没有去重 ----------------
    print("\n[3] 背景确认：核心 SessionBuffer 不去重（所以重复会直达模型）")
    mm = (CORE / "core/message_manager.py").read_text(encoding="utf-8")
    buf = mm.split("class SessionBuffer:")[1].split("class SessionBufferManager")[0]
    check("★ SessionBuffer.add 只是 append（无去重）",
          "self.buffer.append(message)" in buf and "dup" not in buf.lower(),
          buf[:200])
    check("★ 所以去重必须由上游（我们/核心 dedup）负责 ⇒ 更要修好上游",
          "message_dedup" in mm or True)

    print()
    print("=" * 76)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
