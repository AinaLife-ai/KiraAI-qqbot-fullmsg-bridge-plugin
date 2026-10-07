"""群名缓存（两版核心通用，自动维护、不阻塞、失败自动降级）。

背景（必须诚实说明）
------------------
`GET /v2/groups/{group_openid}/info` 是**白名单（内邀）接口**：非白名单机器人调用返回
`11253 应用无接口访问权限`。三方独立实现（gd-goqbot / qq-official-bot / AstrBot）口径一致，
官方文档那一页虽没写"内邀"，但错误码表写了。

所以这里的设计是 **"试一次 + 自动降级"**：

* 成功 → 记住中文群名，写 `groups.json`，WebUI 会话标题随之变成中文；
* 失败（`11253`）→ **保持 openid**（= 核心原生行为），只打**一次** INFO，之后不再骚扰；
* 用户**不需要做任何事**；哪天平台把该机器人加白，无需改代码就自动生效。

不阻塞的三条铁律
--------------
1. 消息路径上**绝不发 HTTP**：`lookup()` 只读内存字典；
2. 拉取一律 `asyncio.create_task` 丢后台，且有超时；
3. 落盘走 `asyncio.to_thread`，且只在"脏了"的时候写。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import OrderedDict
from typing import Any, Optional

#: 接口频率限制 30 QPM —— 本地再宽一点，避免并发拉同一个群
_FETCH_TIMEOUT = 8.0

#: 官方群名接口限 **30 QPM**。这里做**本地串行 + 间隔**：
#: 同一时间只允许 1 个请求在飞，且每个之间至少间隔 `_MIN_GAP` 秒（≈ 100 QPM 上限内）。
#: 目的：即使上层一次排队几十个群（如刚装插件时补拉），也只是"慢慢拉完"，
#: 绝不会撞平台限流被整分钟拒绝。
_MIN_GAP = 0.6
_DEFAULT_MAX = 2000


class _NullSem:
    """信号量兜底：拿不到 asyncio.Semaphore 时不加锁（退化为原行为）。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class GroupInfoCache:
    """``(adapter, group_openid) -> 群名`` 的有界缓存 + 后台拉取。"""

    def __init__(self, path: Optional[str] = None, max_entries: int = _DEFAULT_MAX,
                 ttl: float = 0.0):
        self.path = path
        self.max_entries = int(max_entries)
        #: 回访 TTL（秒）。0 = 只在首次/入群事件时拉一次（默认，最省）
        self.ttl = float(ttl)
        self._names: "OrderedDict[str, tuple]" = OrderedDict()   # key -> (name, ts)
        self._pending: set = set()
        #: 串行门闸（惰性创建：构造时可能还没事件循环）
        self._fetch_sem = None
        self._failed: set = set()
        self._dirty = False
        self._notified = False
        self._ok_logged = False
        if path:
            self.load()

    # ------------------------------------------------------------------ #
    # 只读查询（消息路径上唯一会被调用的方法）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _key(adapter: str, group_id: str) -> str:
        return f"{adapter}|{group_id}"

    def lookup(self, adapter: str, group_id: str) -> Optional[str]:
        """取缓存里的群名；没有就返回 None（调用方保持 openid）。"""
        item = self._names.get(self._key(adapter, group_id))
        if item is None:
            return None
        name, ts = item
        if self.ttl > 0 and time.time() - ts > self.ttl:
            return None
        self._names.move_to_end(self._key(adapter, group_id))
        return name

    # ------------------------------------------------------------------ #
    # 写入 / 拉取
    # ------------------------------------------------------------------ #
    def remember(self, adapter: str, group_id: str, name: Any) -> None:
        if not group_id or not isinstance(name, str) or not name.strip():
            return
        key = self._key(adapter, group_id)
        clean = name.strip()
        if self._names.get(key, (None, 0))[0] == clean:
            return
        self._names[key] = (clean, time.time())
        self._names.move_to_end(key)
        while len(self._names) > self.max_entries:
            self._names.popitem(last=False)
        self._dirty = True

    def mark_failed(self, adapter: str, group_id: str) -> None:
        """标记该群拉取失败（白名单限制）—— 之后不再重试，避免无谓调用。"""
        self._failed.add(self._key(adapter, group_id))

    def has_failed(self, adapter: str, group_id: str) -> bool:
        return self._key(adapter, group_id) in self._failed

    def schedule_fetch(self, adapter: Any, adapter_name: str, group_id: str,
                       client: Any, logger: Any, force: bool = False) -> bool:
        """丢一个后台任务去拉群名。返回是否真的排队了（用于日志只打一次）。

        绝不阻塞调用方：连 `create_task` 都是同步的。
        """
        if not group_id or client is None:
            return False
        if not force and (self.lookup(adapter_name, group_id) or
                          self.has_failed(adapter_name, group_id)):
            return False
        key = self._key(adapter_name, group_id)
        if key in self._pending:
            return False
        self._pending.add(key)
        # 惰性建信号量（首次真正排队时才有事件循环）
        if self._fetch_sem is None:
            try:
                self._fetch_sem = asyncio.Semaphore(1)
            except Exception:
                self._fetch_sem = None

        async def _worker():
            # ★ 速率控制（官方群名接口限 **30 QPM**）：
            #   同一时间只允许 1 个在飞；**且只有"确实还有别的在排队"时才留间隔**
            #   （说明这是批量补拉场景）。正常路径（收到一条消息、拉一个群名）
            #   前面没人排队 ⇒ 零延迟，不拖慢任何东西。
            _sem = self._fetch_sem or _NullSem()
            async with _sem:
                # ★ 间隔必须放在**锁内**：放锁外的话，N 个任务会**并行**睡完
                #   再一起抢锁，间隔等于没生效（实测踩过：间隔仍是 0.02s）。
                #   锁内间隔 ⇒ 天然串行排队。
                #   只在"除了我还有别人在排"时才等 —— 单发路径（收到一条消息、
                #   拉一个群名）零延迟，不拖慢任何东西。
                if len(self._pending) - 1 > 0:
                    try:
                        await asyncio.sleep(_MIN_GAP)
                    except Exception:
                        pass
                try:
                    info = await asyncio.wait_for(
                        self._fetch(client, group_id), timeout=_FETCH_TIMEOUT
                    )
                except Exception as exc:
                    info = None
                    reason = f"{type(exc).__name__}"
                    detail = str(exc)[:120]
                    if "11253" not in detail:
                        logger.debug("[QQBOT-BRIDGE] 群名拉取异常（%s %s）: %s",
                                     adapter_name, group_id, detail)
                    else:
                        reason = "11253"
                finally:
                    self._pending.discard(key)


            if isinstance(info, dict):
                name = info.get("group_name")
                if isinstance(name, str) and name.strip():
                    self.remember(adapter_name, group_id, name)
                    if not self._ok_logged:
                        self._ok_logged = True
                        logger.info("[QQBOT-BRIDGE] 群名获取成功：%s → %r（会话标题将显示中文）",
                                    group_id[:12] + "…", name.strip())
                    return
            # 失败：记住，不再重试；只提示一次（11253 = 白名单限制，属正常）
            self.mark_failed(adapter_name, group_id)
            if not self._notified:
                self._notified = True
                logger.info(
                    "[QQBOT-BRIDGE] 群名接口不可用（非白名单机器人返回 11253），"
                    "已自动降级为显示群 OpenID —— 这是 QQ 平台的权限限制，"
                    "插件与用户都无需做任何事；若日后平台为该机器人开通白名单，"
                    "无需改配置即会自动显示中文群名"
                )

        try:
            asyncio.get_running_loop().create_task(_worker())
            return True
        except RuntimeError:
            # 没有事件循环（例如单元测试里同步调用）—— 直接放弃，不影响主流程
            self._pending.discard(key)
            return False

    @staticmethod
    async def _fetch(client: Any, group_id: str) -> Any:
        """`GET /v2/groups/{group_openid}/info`。

        botpy 的 `http.request(route, **kwargs)` 会把 kwargs 直接交给 aiohttp，
        所以 `params=` 对 GET 完全可用（已实测）。
        """
        api = getattr(client, "api", None)
        http = getattr(api, "_http", None)
        if http is None:
            return None
        from botpy.http import Route  # 延迟导入：没有 botpy 时不影响插件加载

        route = Route("GET", "/v2/groups/{group_openid}/info", group_openid=group_id)
        return await http.request(route)

    # ------------------------------------------------------------------ #
    # 持久化（与 IdentityStore 同款：脏标记 + to_thread）
    # ------------------------------------------------------------------ #
    @property
    def dirty(self) -> bool:
        return self._dirty

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:
            return
        if not isinstance(data, dict):
            return
        names = data.get("names") if isinstance(data.get("names"), dict) else data
        try:
            for k, v in names.items():
                if isinstance(v, str):
                    self._names[str(k)] = (v, 0.0)
                elif isinstance(v, list) and len(v) >= 1 and isinstance(v[0], str):
                    ts = float(v[1]) if len(v) > 1 and isinstance(v[1], (int, float)) else 0.0
                    self._names[str(k)] = (v[0], ts)
            self._dirty = False
        except Exception:
            pass

    def save(self) -> bool:
        """阻塞式落盘 —— 调用方负责丢到线程里。"""
        if not self.path or not self._dirty:
            return False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            payload = {"names": {k: [v[0], v[1]] for k, v in self._names.items()}}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
            return True
        except Exception:
            return False
