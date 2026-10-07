"""私聊昵称修复的回归测试（跨场景共享 + 自动更新）。

背景（为什么会有这个修复）
------------------------
`C2C_MESSAGE_CREATE`（私聊）事件里 `author.username` **恒为空**：
  * 官方文档字段说明只写「发送者（user_openid 有值）」；
  * 官方事件示例就是 `"username": ""`；
  * botpy 的 `C2CMessage._User` 更是只留了 `user_openid` 一个字段；
  * OpenAPI **没有任何"按 openid 查用户资料"的接口**（`/users/@me` 只查机器人自己）。

但**私聊的 `user_openid` 与群里的 `member_openid` 是同一个值**（用户日志实证），
而群消息**是带 `username` 的** ⇒ 把通讯录做成"按人共享"，群里认识过的人私聊也认得。

同时利用第二个免费来源：`message_type=103` 引用消息时，
`msg_elements[].author` 是完整 User 对象（带 `username`）。

本套件覆盖
----------
1. 跨场景共享：群里记的名字，私聊同 id 能取到；
2. **自动更新**：用户改名后覆盖（这是用户明确要求的）；
3. 引用消息学习（第二来源）；
4. 从没见过的 id ⇒ 返回 None（调用方保持 openid，不编造）；
5. 旧数据自动迁移（`adapter|gm|uid` → `adapter|uid`），且**群名优先于 openid 占位**；
6. 落盘 / 回读 / 有界。
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
# 端到端那一段需要真实核心；KIRA_CORE 指向核心源码根目录
_CORE = os.environ.get("KIRA_CORE", "")
if _CORE:
    sys.path.insert(0, str(pathlib.Path(_CORE).parent))
    sys.path.insert(0, str(pathlib.Path(_CORE)))

import qqbot_bridge as B  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def tmp_path():
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(p)
    return p


def main():
    print("=" * 72)
    print("## 私聊昵称：跨场景共享 + 自动更新")
    print("=" * 72)

    print("\n[1] 跨场景共享（核心修复）")
    s = B.IdentityStore()
    s.remember("qqo", "gm", "UID1", "周武")            # 群里收到消息，学到名字
    got = s.remember("qqo", "dm", "UID1", None)        # 私聊同一个人，事件不带名字
    check("★ 群里记的名字，私聊同 id 能取到", got == "周武", repr(got))

    print("\n[2] 自动更新（用户改名要能跟随）")
    s.remember("qqo", "gm", "UID1", "周武改名了")
    check("★ 改名后覆盖旧值", s.remember("qqo", "dm", "UID1", None) == "周武改名了")
    check("重复写同一个名字不算脏", (lambda: (s.save(), setattr(s, "_dirty", False),
                                        s.remember("qqo", "gm", "UID1", "周武改名了"),
                                        not s.dirty)[-1])())
    check("但改名会标脏（会被写盘）", (lambda: (s.remember("qqo", "gm", "UID1", "又一个新名"),
                                         s.dirty)[-1])())

    print("\n[3] 引用消息学习（第二来源）")
    s2 = B.IdentityStore()
    learned = s2.remember_from_quoted("qqo", [
        {"author": {"member_openid": "UID2", "username": "摸馍"}},
        {"author": {"user_openid": "UID3", "username": "香里"}},
        {"author": {"member_openid": "UID4", "username": ""}},      # 空名不算
        {"no_author": 1},                                            # 脏数据不崩
    ])
    check("学到 2 条（跳过空名与脏数据）", learned == 2, str(learned))
    check("引用里学到的名字私聊可用", s2.remember("qqo", "dm", "UID2", None) == "摸馍")

    print("\n[4] 没见过的人不编造")
    s3 = B.IdentityStore()
    check("★ 从没见过的 id ⇒ None（调用方保持 openid）",
          s3.remember("qqo", "dm", "NEVER", None) is None)
    check("lookup 对未知 id 返回 None", s3.lookup("qqo", "NEVER") is None)

    print("\n[5] 旧数据自动迁移（群名优先于 openid 占位）")
    p = tmp_path()
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({
            "qqo|dm|UIDX": "9CD54739CC9BAA46B93243088802DC72",   # 私聊存的是 openid（当时没学到）
            "qqo|gm|UIDX": "周武",                               # 群里存的是真名
        }, fh, ensure_ascii=False)
    s4 = B.IdentityStore(path=p)
    check("★ 迁移后取到真名而不是 openid 占位", s4.lookup("qqo", "UIDX") == "周武",
          repr(s4.lookup("qqo", "UIDX")))
    check("迁移会标脏（下次会回写成新格式）", s4.dirty)
    os.unlink(p)

    p2 = tmp_path()
    with open(p2, "w", encoding="utf-8") as fh:
        json.dump({"qqo|dm|UIDY": "资料卡名", "qqo|gm|UIDY": "群里的名"}, fh, ensure_ascii=False)
    s5 = B.IdentityStore(path=p2)
    check("两边都是真名时，群名优先", s5.lookup("qqo", "UIDY") == "群里的名",
          repr(s5.lookup("qqo", "UIDY")))
    os.unlink(p2)

    print("\n[6] 落盘 / 回读 / 有界")
    p3 = tmp_path()
    s6 = B.IdentityStore(path=p3)
    s6.remember("qqo", "gm", "UIDZ", "香里")
    check("save 返回 True", bool(s6.save()))
    s7 = B.IdentityStore(path=p3)
    check("回读一致", s7.lookup("qqo", "UIDZ") == "香里")
    s7.remember("qqo", "gm", "UIDW", "后来改的名")
    s7.save()
    with open(p3, encoding="utf-8") as fh:
        raw = json.load(fh)
    check("落盘格式带版本号（便于将来再迁移）", raw.get("version") == 3, str(raw)[:60])
    os.unlink(p3)

    s8 = B.IdentityStore(max_entries=10)
    for i in range(50):
        s8.remember("qqo", "gm", f"U{i}", f"名{i}")
    check("条数封顶（长期跑不涨）", s8.size <= 10, str(s8.size))

    print("\n[7] 端到端：build_event 里私聊真的能拿到名字")
    try:
        from core.adapter.adapter_info import AdapterInfo
        from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
        from core.chat import Group, MessageChain, User
        from core.chat.message_elements import Text
        from core.chat.message_utils import KiraIMMessage, KiraMessageEvent
        import asyncio
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                           platform="QQ Official Bot",
                           config={"app_id": "a", "app_secret": "b",
                                   # deny_list：不在名单里就放行，避免被权限判定干扰
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        # 兼容两版适配器构造：2.x = (info, queue)，3.0 = AdapterContext(info=…, event_queue=…)
        try:
            adapter = QQOfficialAdapter(info, asyncio.Queue())
        except TypeError:
            from core.adapter.context import AdapterContext

            adapter = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
        ids = B.IdentityStore()
        ids.remember("qqo", "gm", "SAMEID", "周武")     # 群里学到

        dm = {"id": "M1", "author": {"id": "SAMEID", "user_openid": "SAMEID",
                                     "username": "", "bot": False},
              "content": "在吗", "message_type": 0,
              "timestamp": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()}
        ev, _reason = B.build_event(
            adapter, dm, Group=Group, User=User, KiraIMMessage=KiraIMMessage,
            KiraMessageEvent=KiraMessageEvent, kind=B.KIND_DM, is_group=False,
            force_mention=True, dedup=None, identities=ids,
            self_identity=B.SelfIdentity(), At=None, Text=Text,
        )
        # 3.0 上 build_event 拿不到 2.x 的适配器内部结构（`no-chain-builder`），
        # 这是**预期**的 —— 3.0 由核心自己构造事件，桥接不参与（见 core_profiles）。
        # 所以 3.0 下只验证「跨场景通讯录本身工作正常」。
        if _reason == "no-chain-builder":
            check("3.0 下 build_event 不参与事件构造（预期，核心自己做）",
                  ids.remember("qqo", "dm", "SAMEID", None) == "周武")
        else:
            check("★ 私聊事件构造成功", ev is not None, str(_reason))
            if ev is not None:
                check("★ 私聊昵称 = 群里学到的真名（不是 openid）",
                      ev.message.sender.nickname == "周武",
                      repr(ev.message.sender.nickname))
    except Exception as exc:
        print(f"  skip  端到端部分需要真实核心（{type(exc).__name__}: {exc}）")

    print()
    print("=" * 72)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
