"""私聊昵称修复：跨场景共享 + 自动更新。

问题
----
`C2C_MESSAGE_CREATE`（私聊）事件里的 `author.username` **恒为空字符串**
（官方文档示例即为 `"username": ""`，botpy 的 `C2CMessage._User` 更是只留了
`user_openid` 一个字段）。OpenAPI 里**没有任何"按 openid 查用户资料"的接口**
（`/users/@me` 只查机器人自己；`union_openid`/`union_user_account` 需特殊申请+内邀）。
⇒ 私聊拿不到昵称，框架原生行为也是回退成那串 OpenID。

破解思路（利用一个已核实的事实）
------------------------------
**私聊的 `user_openid` 与群里的 `member_openid` 是同一个值。**
（用户日志实证：私聊 `9CD54739CC9BAA46B93243088802DC72` == 群里 @ 的 member_openid）

而**群消息里是带 `username` 的**。所以：

> 把昵称通讯录从「按场景隔离」改成「按人共享」——
> 群里认识过的人，私聊也认得。

顺带补第二个来源：`message_type=103`（引用消息）时，`msg_elements[0].author`
是**完整 User 对象**（文档明确列了 username），私聊引用自己的话也能学到昵称。

自动更新 / 自动维护
------------------
* 每次见到新昵称就写入；**与已记录的不同就覆盖** ⇒ **用户改名自动跟随**；
* 落盘仍是「内存 + 脏标记 + 后台 to_thread」，**消息路径零 I/O**；
* 数据格式升级为 v2（`adapter|uid -> [name, ts]`），
  旧格式（`adapter|scope|uid -> name`）**读取时自动迁移**，不丢数据。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import OrderedDict
from typing import Any, Optional


class IdentityStore:
    """``(adapter, uid) -> (nickname, ts)`` —— 跨场景共享、自动更新。

    **为什么旧版要按 scope 隔离**：担心同一个 id 在群 A 和群 B 是不同的人。
    但官方文档写明 `member_openid` 是「**群成员 OpenID**」而 `user_openid` 是
    「用户 OpenID」，两者在实测里同值 —— 即该 id 是**机器人维度下这个人的标识**，
    不随群变化。共享因此是安全的，而且正好解决私聊拿不到昵称的问题。

    **持久化绝不在消息路径上**：``remember()`` 只标脏；
    真正的写盘由插件用 ``asyncio.to_thread`` 调 :meth:`save`。
    """

    __slots__ = ("path", "max_entries", "_store", "_dirty", "_last_seen")

    #: 数据格式版本（v2 = 跨场景共享；v1 = 旧的按场景隔离）
    FORMAT = 2

    def __init__(self, path: Optional[str] = None, max_entries: int = 4000):
        self.path = path
        self.max_entries = int(max_entries)
        self._store: "OrderedDict[str, tuple]" = OrderedDict()   # key -> (name, ts)
        self._dirty = False
        if path:
            self.load()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _key(adapter: str, uid: str) -> str:
        return f"{adapter}|{uid}"

    def remember(self, adapter: str, scope: str, uid: str, nickname: Optional[str]) -> Optional[str]:
        """记下昵称并返回"目前最可信的名字"。

        :param scope: 保留参数（旧的 gm/dm 区分）。**不再参与 key** —— 见类注释。
        :param nickname: 本次事件带来的名字；为空表示"这次没带名字"。
        :returns: 本次能用的昵称；都没见过则返回 None（调用方退回 openid）。

        行为：
        * 带了名字 ⇒ 写入/更新（**与旧值不同就覆盖 ⇒ 改名自动跟随**），返回新名字；
        * 没带名字 ⇒ 返回已记住的名字（跨场景共享就在这里生效：私聊事件没有名字，
          但同一个 id 在群里被记过 ⇒ 这里就能取到）。
        """
        del scope  # 兼容旧调用签名，但不再用于 key
        key = self._key(adapter, uid)
        store = self._store
        now = time.time()

        if nickname:
            prev = store.get(key)
            if prev is None or prev[0] != nickname:
                self._dirty = True          # 新记录 or 改名
            store[key] = (nickname, now)
            store.move_to_end(key)
            while len(store) > self.max_entries:
                store.popitem(last=False)
            return nickname

        item = store.get(key)
        if item is None:
            return None
        store.move_to_end(key)
        store[key] = (item[0], now)         # 刷新热度，避免被 LRU 淘汰
        return item[0]

    def lookup(self, adapter: str, uid: str) -> Optional[str]:
        """只查不写（给需要"顺带看一眼"的地方用）。"""
        item = self._store.get(self._key(adapter, uid))
        return item[0] if item else None

    def remember_from_quoted(self, adapter: str, elements: Any) -> int:
        """从引用消息的 `msg_elements[]` 里学昵称（免费的第二来源）。

        `message_type=103` 时每个元素的 `author` 都是完整 User 对象，带 `username`。
        私聊里对方引用自己的话时，就能顺手把他的昵称记下来。

        :returns: 学到了几条（0 表示这次没收获）。
        """
        if not isinstance(elements, list):
            return 0
        learned = 0
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            author = elem.get("author")
            if not isinstance(author, dict):
                continue
            name = author.get("username")
            uid = author.get("member_openid") or author.get("user_openid") or author.get("id")
            if not isinstance(name, str) or not name.strip():
                continue
            if not uid:
                continue
            if self.remember(adapter, "any", str(uid), name.strip()):
                learned += 1
        return learned

    # ------------------------------------------------------------------ #
    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def size(self) -> int:
        return len(self._store)

    def load(self) -> None:
        """读盘，并**自动迁移**旧格式（v1 按场景隔离 → v2 跨场景共享）。"""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:
            return
        if not isinstance(data, dict):
            return

        migrated = False
        store = self._store
        # 新格式：{"version": 2, "names": {key: [name, ts]}}
        names = data.get("names") if isinstance(data.get("names"), dict) else data

        def looks_like_openid(text: str) -> bool:
            """32 位 hex ⇒ 旧数据里"没学到名字"的占位（当时回退成了 uid）。"""
            return bool(re.fullmatch(r"[0-9A-Fa-f]{32}", text or ""))

        for raw_key, value in names.items():
            raw_key = str(raw_key)
            if isinstance(value, list) and value and isinstance(value[0], str):
                # 已是新格式，直接收
                ts = float(value[1]) if len(value) > 1 and isinstance(value[1], (int, float)) else 0.0
                store[raw_key] = (value[0], ts)
                continue
            if not isinstance(value, str):
                continue

            # 旧格式 key = adapter|gm|uid 或 adapter|dm|uid ⇒ 折叠成 adapter|uid
            parts = raw_key.split("|")
            scope = ""
            if len(parts) == 3 and parts[1] in ("gm", "dm", "any"):
                scope = parts[1]
                key = f"{parts[0]}|{parts[2]}"
                migrated = True
            else:
                key = raw_key

            # 折叠冲突时的取舍：群里那个才是真昵称；长得像 openid 的算"没学到"，最低优先
            score = {"gm": 3, "any": 2, "dm": 1, "": 2}.get(scope, 2)
            if looks_like_openid(value):
                score = 0
            prev = store.get(key)
            prev_score = prev[2] if prev is not None and len(prev) > 2 else -1
            if prev is None or score >= prev_score:
                store[key] = (value, 0.0, score)
        # 清理打分字段（不落盘）
        for key, item in list(store.items()):
            if len(item) > 2:
                store[key] = (item[0], item[1])
                store.move_to_end(key)
        self._dirty = self._dirty or migrated

    def save(self) -> bool:
        """落盘。阻塞式 —— 调用方负责丢到线程里。"""
        if not self.path or not self._dirty:
            return False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            payload = {
                "version": self.FORMAT,
                "names": {k: [v[0], v[1]] for k, v in self._store.items()},
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
            return True
        except Exception:
            return False
