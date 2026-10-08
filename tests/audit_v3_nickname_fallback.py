"""★★★ 私聊昵称仍显示 hex 的根因回归（KiraAI 3.0）。

## 线上现象（2026-10-08 用户日志）

    群聊：[group_name: 香里、周武 ... user_nickname: 周武, user_id: 9CD54739...]
    私聊：[user_nickname: 9CD54739CC9BAA46B93243088802DC72, user_id: 同上]

## 两层根因（v1.6.0 只修了第一层，所以用户说"还是 hex"）

1. **学不到**：3.0 上桥接不接管事件 ⇒ 挂在 2.x 事件路径上的"记昵称"从不执行。
   v1.6.0 补了 `_handle_message` 旁听 —— **这层是对的**。
2. **学了没人用**（真缺口）：3.0 显示昵称的**唯一路径**是核心的
   `QQOfficialMessageParser.nickname()`，它只读事件自带的 `author.username`，
   读不到就 `return user_id`。我们写进 `IdentityStore` 的名字**没有任何人读**。

   ⇒ 本版包一层 parser 的 `nickname`：**事件没带名字时**才去通讯录里按 openid 找
     （群里认识过的同一个人）。找到就借核心自己的实现写回它的缓存，
     这样 `content_elements()` 里渲染 @ 用的也是同一个名字。

本测试用**真实核心的 parser**（`QQOfficialMessageParser`）+ 最小的假适配器，
断言：学得到、读得出、不误伤群消息、可还原、通讯录为空时行为不变。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT

import asyncio
import sys

CORE = str(_CORE_ROOT("3"))
sys.path.insert(0, _BR())
sys.path.insert(0, CORE)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class _Log:
    def __init__(self):
        self.lines = []

    def _p(self, lv, a):
        self.lines.append((lv, (a[0] % tuple(a[1:])) if len(a) > 1 else str(a[0])))

    def info(self, *a):
        self._p("info", a)

    def warning(self, *a):
        self._p("warning", a)

    def debug(self, *a):
        pass


class _FakeIM:
    """最小能力对象：只放 v3_support 会碰的几个成员。"""

    def __init__(self, parser):
        self._parser = parser
        self.handled = []

    async def _handle_message(self, message, is_group, force_mention):
        self.handled.append({"message": message, "is_group": is_group})
        return "orig"

    def _is_self_quote(self, message, is_group, target_id):
        return False


class _Info:
    name = "qqo"


class _FakeAdapter:
    def __init__(self, im):
        self.im = im
        self.info = _Info()

    def publish(self, event):
        pass


class _Profile:
    def __init__(self, im):
        self.im_capability = im


class _Plugin:
    def __init__(self, store):
        self.identities = store


def main():
    print("═══ 私聊昵称：学到 ⇒ 用上（KiraAI 3.0）═══")
    try:
        from core.adapter.src.qq_official.message_parser import QQOfficialMessageParser
    except Exception as exc:
        print(f"  skip  找不到 3.0 核心的 parser（{exc}）")
        return 0
    from identity_shared import IdentityStore
    from v3_support import V3Enhancer

    UID = "9CD54739CC9BAA46B93243088802DC72"
    store = IdentityStore(path=None)          # 不落盘
    parser = QQOfficialMessageParser()
    im = _FakeIM(parser)
    ad = _FakeAdapter(im)
    plugin = _Plugin(store)
    log = _Log()
    enh = V3Enhancer(plugin, log)

    print("\n[1] 安装：parser.nickname 被包了一层（且可还原）")
    check("★ install 成功", enh.install(ad, "qqo", _Profile(im)) is True)
    check("★ parser.nickname 已被包", getattr(parser.nickname, "_kira_bridge_nickname", False))
    orig = parser.nickname

    print("\n[2] 群消息：学到的名字进通讯录")
    group_body = {"author": {"member_openid": UID, "username": "周武"},
                  "group_openid": "G1",
                  "mentions": [{"member_openid": "OTHER", "username": "香里"}]}
    asyncio.run(im._handle_message(group_body, True, False))
    check("★★ 群里的昵称进了通讯录", store.lookup("qqo", UID) == "周武",
          str(store.lookup("qqo", UID)))
    check("★ @ 列表里的第三方也学到了", store.lookup("qqo", "OTHER") == "香里",
          str(store.lookup("qqo", "OTHER")))
    check("★ 旁听没有改行为（原方法照常返回）",
          asyncio.run(im._handle_message(group_body, True, False)) == "orig")

    print("\n[3] ★★★ 私聊：author.username 恒为空 ⇒ 显示真名而不是 hex")
    name = parser.nickname(False, UID, {"user_openid": UID, "username": ""}, UID)
    check("★★★ 私聊昵称 = 周武（不再是那串 hex）", name == "周武", repr(name))
    check("★ 学习日志只打一次（不刷屏）",
          sum(1 for _lv, m in log.lines if "私聊昵称已恢复真名" in m) == 1,
          str(log.lines))

    print("\n[4] 反向验证：没有这个补丁时就是 hex")
    fresh = QQOfficialMessageParser()
    check("★★ 原核心行为 = user_id（证明这个测试测的是真缺口）",
          fresh.nickname(False, UID, {"user_openid": UID, "username": ""}, UID) == UID)

    print("\n[5] 不误伤：群消息自带的名字仍然优先")
    check("★ 事件带 username ⇒ 用它（不改语义）",
          parser.nickname(True, "G1", {"member_openid": "X", "username": "小李"}, "X") == "小李")
    check("★ 通讯录里没有的人 ⇒ 仍然是 user_id（不编造）",
          parser.nickname(False, "NOPE", {"user_openid": "NOPE", "username": ""}, "NOPE") == "NOPE")
    check("★ 名字与 id 相同（占位）⇒ 不会被当成真名",
          parser.nickname(False, UID, {"user_openid": UID, "username": UID}, UID) == UID)

    print("\n[6] 可还原（插件关掉后行为回到核心原生）")
    check("★ restore 返回改动数 ≥ 1", enh.restore("qqo") >= 1)
    check("★ parser.nickname 已还原成类上的原方法",
          not getattr(parser.nickname, "_kira_bridge_nickname", False))
    check("★ 还原后再查 ⇒ 又是 user_id",
          parser.nickname(False, UID, {"user_openid": UID, "username": ""}, UID) == UID)

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(main())
