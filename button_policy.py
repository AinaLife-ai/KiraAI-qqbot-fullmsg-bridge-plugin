"""按钮点击「策略层」—— 限次 / 限人 / 截止 / 计数 / 查账（插件侧实现）。

为什么需要它
------------
官方 `action.click_limit` **已弃用**（默认不限）⇒ 平台不提供任何次数限制；
但**每次点击都会推给我们**（INTERACTION_CREATE）⇒ 限制、计数、截止、查账
只能在插件侧做，而且只有插件侧做得成。

声明（两个入口，都会在**发送前剥离**，平台永远看不到）
----------------------------------------------------
1. **标签属性**（简洁，模型日常用）::

       <keyboard once="1" max="5" per="2" ttl="600" until="22:30"
                 cooldown="30" deliver="last" hard="1" notify="0"
                 notify_text="报名满啦" label="报名">{"content":{...}}</keyboard>

   * `once`      每人只能点一次
   * `max`       全局最多接受多少次
   * `per`       每人最多几次
   * `ttl`       从发出起多少秒后截止
   * `until`     绝对截止时刻 `HH:MM`（早于当前时间则视为次日）或 ISO 串
   * `cooldown`  同一人多少秒内的连点只算一次（不计入次数）
   * `deliver`   `all`（**默认**：每次有效点击都告诉模型，与旧版一致）/
                 `last`（只在截止那一次告诉）/ `off`（都不告诉，模型自己查账）
   * `hard`      超额/过期**不再转给模型**（默认取全局配置，全局默认 1=硬）
   * `notify`    截止时给**用户**发一条机械文案（默认关；机械文案一向不推荐）
   * `notify_text` 自定义文案
   * `label`     给模型看的按钮名（查账/汇总用）

2. **JSON 内 `kirai` 对象**（精细，可逐按钮不同策略）::

       {"content":{"rows":[{"buttons":[
          {"id":"b1","render_data":{"label":"报名"},"action":{"type":1,"data":"join"},
           "kirai":{"max":1,"once":true,"until":"14:30","label":"双人团"}}]}]}}

匹配一次点击的三条路（自动降级）
--------------------------------
1. **发送回执的 message_id** → 策略（最准；发送成功即登记）；
2. **`action.data` 前缀 token**（可选，`~ab12|原data`；接收时剥掉，超长则不加）；
3. **会话 + button_id 的最近一条未截止策略**（兜底）。

全部**消息路径零 I/O**：只有内存字典查改；落盘由调用方在巡检里 `to_thread` 触发。
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

#: 全局上限（防内存膨胀）
DEFAULT_MAX_POLICIES = 500
#: 单个策略最多记多少人的明细
MAX_USERS_PER_POLICY = 2000
#: 汇总里最多列几个名字（省 token）
SUMMARY_NAMES = 8

#: 布尔字段的宽松解析
_TRUE = {"1", "true", "yes", "y", "on", "是", "开"}


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _TRUE


def _as_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    if v is None or v == "":
        return default
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


def _as_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    if v is None or v == "":
        return default
    try:
        return float(str(v).strip())
    except Exception:
        return default


def parse_until(value: Any, now: Optional[float] = None) -> Optional[float]:
    """把 `until` 解析成绝对时间戳。

    支持：`HH:MM`（当天，早于现在则算次日）、`HH:MM:SS`、ISO8601（含 `T`）、
    纯数字（当 Unix 时间戳）。
    """
    if value is None or value == "":
        return None
    text = str(value).strip()
    base = now if now is not None else time.time()

    if text.replace(".", "", 1).isdigit() and float(text) > 1e9:
        return float(text)

    # HH:MM[:SS]
    parts = text.split(":")
    if len(parts) in (2, 3) and all(p.strip().isdigit() for p in parts):
        hh, mm = int(parts[0]), int(parts[1])
        ss = int(parts[2]) if len(parts) == 3 else 0
        if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60):
            return None
        lt = time.localtime(base)
        cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, ss, 0, 0, -1))
        if cand <= base:
            cand += 86400.0
        return cand

    # ISO8601
    try:
        import datetime as _dt

        iso = text.replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            return dt.timestamp()
        return dt.timestamp()
    except Exception:
        return None


def spec_from_attrs(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """把标签属性（或 JSON 里的 `kirai` 对象）规范化成策略规格。"""
    a = attrs or {}
    spec: Dict[str, Any] = {}

    once = _as_bool(a.get("once"), False)
    mx = _as_int(a.get("max"))
    per = _as_int(a.get("per"))
    cooldown = _as_int(a.get("cooldown"), 0) or 0
    ttl = _as_int(a.get("ttl"), 0) or 0
    until_raw = a.get("until")
    deliver = str(a.get("deliver") or "").strip().lower()
    hard = a.get("hard")
    notify = a.get("notify")
    notify_text = a.get("notify_text") or a.get("notifyText") or ""
    label = a.get("label") or a.get("name") or ""

    if once:
        spec["once"] = True
    if mx is not None and mx > 0:
        spec["max"] = int(mx)
    if per is not None and per > 0:
        spec["per"] = int(per)
    if cooldown > 0:
        spec["cooldown"] = int(cooldown)
    if ttl > 0:
        spec["ttl"] = int(ttl)
    if until_raw:
        spec["until_raw"] = until_raw
    if deliver in ("last", "all", "off"):
        spec["deliver"] = deliver
    if hard is not None:
        spec["hard"] = _as_bool(hard, True)
    if notify is not None:
        spec["notify"] = _as_bool(notify, False)
    if notify_text:
        spec["notify_text"] = str(notify_text)[:120]
    if label:
        spec["label"] = str(label)[:40]
    return spec


def split_keyboard_declaration(payload: Dict[str, Any], attrs: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """从键盘载荷里**剥离**策略声明，返回 ``(干净的载荷, 规格)``。

    * 标签属性 → 内容级默认；
    * `payload["kirai"]` → 内容级覆盖；
    * 每个按钮的 `"kirai"` → 该按钮专属（放进 `spec["buttons"]`）。

    ⚠ 剥离是**必须**的：官方 schema 里没有这些字段，原样发出去可能报
    `40034029 键盘参数错误`。剥完的载荷与官方字段**完全一致**。
    """
    if not isinstance(payload, dict):
        return payload, {}
    out = dict(payload)
    spec = spec_from_attrs(attrs)
    spec["buttons"] = {}

    content_spec = out.pop("kirai", None)
    if isinstance(content_spec, dict):
        spec.update(spec_from_attrs(content_spec))

    content = out.get("content")
    if isinstance(content, dict):
        rows = content.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                buttons = row.get("buttons")
                if not isinstance(buttons, list):
                    continue
                for btn in buttons:
                    if not isinstance(btn, dict):
                        continue
                    one = btn.pop("kirai", None)
                    if isinstance(one, dict):
                        bid = str(btn.get("id") or "")
                        if bid:
                            spec["buttons"][bid] = spec_from_attrs(one)
    return out, spec


class ButtonPolicyStore:
    """策略账本：注册 → 计数判定 → 查账。全部内存操作 + 原子落盘。"""

    FORMAT = 1

    __slots__ = ("path", "max_entries", "_pol", "_by_msg", "_by_sid_btn", "_by_token",
                 "_seq", "_dirty", "defaults")

    def __init__(self, path: Optional[str] = None, max_entries: int = DEFAULT_MAX_POLICIES,
                 defaults: Optional[dict] = None):
        self.path = path
        self.max_entries = int(max_entries)
        self._pol: "OrderedDict[str, dict]" = OrderedDict()
        self._by_msg: Dict[str, str] = {}
        self._by_sid_btn: Dict[str, List[str]] = {}
        self._by_token: Dict[str, str] = {}
        self._seq = 0
        self._dirty = False
        #: 全局默认（来自插件配置）：deliver / hard / notify / notify_text / ttl / once
        self.defaults = dict(defaults or {})
        if path:
            self.load()

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def register(self, sid: str, spec: Optional[Dict[str, Any]], *,
                 now: Optional[float] = None, token: bool = False,
                 button_ids: Optional[List[str]] = None) -> Optional[dict]:
        """为一条**即将发出的**消息登记策略。没声明就返回 None（零开销路径）。"""
        if not spec:
            return None
        now = now if now is not None else time.time()
        # 合并全局默认
        merged: Dict[str, Any] = {}
        for k in ("deliver", "hard", "notify", "notify_text"):
            if k in self.defaults:
                merged[k] = self.defaults[k]
        if self.defaults.get("ttl"):
            merged["ttl"] = self.defaults["ttl"]
        if self.defaults.get("once"):
            merged["once"] = True
        merged.update({k: v for k, v in spec.items() if v not in (None, "", {}, [])})

        until = None
        if merged.get("until_raw"):
            until = parse_until(merged.pop("until_raw"), now=now)
        if until is None and merged.get("ttl"):
            until = now + float(merged["ttl"])
        elif until is not None:
            merged.pop("ttl", None)

        self._seq += 1
        key = f"{sid}|{self._seq}"
        pol = {
            "key": key,
            "sid": str(sid),
            "created": now,
            "until": float(until) if until else 0.0,
            "max": _as_int(merged.get("max")) or 0,
            "per": _as_int(merged.get("per")) or 0,
            "once": bool(merged.get("once") or False),
            "cooldown": _as_int(merged.get("cooldown")) or 0,
            #: 默认 **all** = 原来正常的方式（每次有效点击都转给模型）。
            #  "last"（只在截止那一次）/ "off"（都不转）留给用户按需选。
            "deliver": str(merged.get("deliver") or "all"),
            "hard": _as_bool(merged.get("hard"), True),
            "notify": _as_bool(merged.get("notify"), False),
            "notify_text": str(merged.get("notify_text") or "")[:120],
            "label": str(merged.get("label") or "")[:40],
            "buttons": merged.get("buttons") or {},
            #: 本条消息里**所有**按钮 id（兜底匹配用；含没写逐按钮策略的）
            "button_ids": [str(b) for b in (button_ids or []) if str(b)],
            "total": 0,
            "users": {},
            "msgs": [],
            "closed": False,
            "closed_reason": "",
            "closed_at": 0.0,
            "notified": False,
            "deliver_override": None,
        }
        pol["deliver_override"] = self._effective_deliver(pol)
        if token:
            tok = self._b36(self._seq)
            pol["token"] = tok
            self._by_token[tok] = key
        self._pol[key] = pol
        self._index(pol)
        self._dirty = True
        self._trim()
        return pol

    def bind_message(self, key: str, message_id: Optional[str]) -> None:
        """把**发送回执的消息 id** 绑到策略上（点击时就能精确匹配）。"""
        if not key or not message_id:
            return
        pol = self._pol.get(key)
        if pol is None:
            return
        mid = str(message_id)
        if mid in pol["msgs"]:
            return
        pol["msgs"].append(mid)
        if len(pol["msgs"]) > 8:
            pol["msgs"].pop(0)
        self._by_msg[mid] = key
        self._dirty = True

    # ------------------------------------------------------------------ #
    # 匹配
    # ------------------------------------------------------------------ #
    def resolve(self, *, message_id: str = "", sid: str = "", button_id: str = "",
                data: str = "") -> Tuple[Optional[dict], str]:
        """找到这次点击对应的策略（找不到返回 ``(None, 原 data)``）。

        ⚠ 不管找不找得到策略，**data 里的 token 前缀都会被剥掉** ——
        保证模型看到的永远是它自己写的原值。
        """
        clean = strip_token(data)
        tok = token_of(data)
        if tok:
            key = self._by_token.get(tok)
            if key and key in self._pol:
                return self._pol[key], clean
        if message_id:
            key = self._by_msg.get(str(message_id))
            if key and key in self._pol:
                return self._pol[key], clean
        if sid and button_id:
            for key in self._by_sid_btn.get(f"{sid}|{button_id}", []):
                pol = self._pol.get(key)
                if pol is not None and not pol["closed"]:
                    return pol, clean
            for key in self._by_sid_btn.get(f"{sid}|{button_id}", []):
                pol = self._pol.get(key)
                if pol is not None:
                    return pol, clean
        return None, clean

    # ------------------------------------------------------------------ #
    # 判定（核心，O(1)）
    # ------------------------------------------------------------------ #
    def decide(self, pol: dict, uid: str, *, now: Optional[float] = None) -> Dict[str, Any]:
        """判定一次点击并**就地计数**。返回判定结果（不抛异常）。"""
        now = now if now is not None else time.time()
        uid = str(uid or "")
        verdict: Dict[str, Any] = {
            "accepted": False, "reason": "ok", "closing": False, "hard": bool(pol.get("hard")),
            "notify": bool(pol.get("notify")), "notify_text": pol.get("notify_text") or "",
            "label": pol.get("label") or "", "total": pol.get("total", 0),
            "remaining": None, "user_count": 0, "closed": bool(pol.get("closed")),
            "deliver": self._effective_deliver(pol),
        }
        if pol.get("closed"):
            verdict["reason"] = pol.get("closed_reason") or "closed"
            self._fill_counts(pol, uid, verdict)
            return verdict
        until = float(pol.get("until") or 0)
        if until and now > until:
            self._close(pol, "expired", now)
            verdict["reason"] = "expired"
            verdict["closed"] = True
            self._fill_counts(pol, uid, verdict)
            return verdict

        users = pol.setdefault("users", {})
        rec = users.get(uid)
        cd = int(pol.get("cooldown") or 0)
        if cd and rec and (now - float(rec[2])) < cd:
            verdict["reason"] = "cooldown"
            self._fill_counts(pol, uid, verdict)
            return verdict
        mx = int(pol.get("max") or 0)
        if mx and int(pol.get("total") or 0) >= mx:
            verdict["reason"] = "full"
            self._close(pol, "full", now)
            verdict["closed"] = True
            self._fill_counts(pol, uid, verdict)
            return verdict
        if rec:
            if pol.get("once"):
                verdict["reason"] = "repeat"
                self._fill_counts(pol, uid, verdict)
                return verdict
            per = int(pol.get("per") or 0)
            if per and int(rec[0]) >= per:
                verdict["reason"] = "over_user"
                self._fill_counts(pol, uid, verdict)
                return verdict

        # ---- 接受 ---- #
        if rec:
            rec[0] += 1
            rec[2] = now
        else:
            users[uid] = [1, now, now]
            if len(users) > MAX_USERS_PER_POLICY:
                try:
                    users.pop(next(iter(users)))
                except Exception:
                    pass
        pol["total"] = int(pol.get("total") or 0) + 1
        self._dirty = True
        self._pol.move_to_end(pol["key"])
        if mx and int(pol["total"]) >= mx:
            self._close(pol, "full", now)
            verdict["closing"] = True
            verdict["closed"] = True
        verdict["accepted"] = True
        verdict["reason"] = "ok"
        self._fill_counts(pol, uid, verdict)
        return verdict

    def _effective_deliver(self, pol: dict) -> str:
        """计算真正生效的告知方式。

        * 默认 `all`：每次**有效**点击都转给模型（与旧版行为一致）；
        * `last`：只在"触发截止的那一次"转（**用户显式选择**才用）；
          若该策略没有任何全局结束点（无 max、无截止）⇒ 退成 `all`；
        * `off`：一次都不转（模型用查账工具自己看）。
        """
        mode = str(pol.get("deliver") or "all")
        if mode == "last" and not int(pol.get("max") or 0) and not float(pol.get("until") or 0):
            return "all"
        return mode

    @staticmethod
    def _fill_counts(pol: dict, uid: str, verdict: Dict[str, Any]) -> None:
        total = int(pol.get("total") or 0)
        mx = int(pol.get("max") or 0)
        rec = (pol.get("users") or {}).get(str(uid))
        verdict["total"] = total
        verdict["remaining"] = max(0, mx - total) if mx else None
        verdict["user_count"] = int(rec[0]) if rec else 0
        verdict["closed"] = bool(pol.get("closed"))

    def _close(self, pol: dict, reason: str, now: float) -> None:
        if pol.get("closed"):
            return
        pol["closed"] = True
        pol["closed_reason"] = reason
        pol["closed_at"] = now
        self._dirty = True

    def close(self, key: str, reason: str = "manual", *, now: Optional[float] = None) -> bool:
        pol = self._pol.get(key)
        if pol is None:
            return False
        self._close(pol, reason, now if now is not None else time.time())
        return True

    def reset(self, key: str, *, now: Optional[float] = None) -> bool:
        """清空计数（重开一轮），并重新开启。"""
        pol = self._pol.get(key)
        if pol is None:
            return False
        pol["total"] = 0
        pol["users"] = {}
        pol["closed"] = False
        pol["closed_reason"] = ""
        pol["closed_at"] = 0.0
        pol["notified"] = False
        pol["created"] = now if now is not None else time.time()
        self._dirty = True
        return True

    def extend(self, key: str, *, max_add: int = 0, minutes: int = 0,
               now: Optional[float] = None) -> bool:
        """加名额 / 延长时间。"""
        pol = self._pol.get(key)
        if pol is None:
            return False
        now = now if now is not None else time.time()
        if max_add:
            pol["max"] = int(pol.get("max") or 0) + int(max_add)
            if int(pol["max"]) > int(pol.get("total") or 0):
                pol["closed"] = False
                pol["closed_reason"] = ""
        if minutes:
            base = float(pol.get("until") or 0) or now
            pol["until"] = max(base, now) + int(minutes) * 60.0
            if float(pol["until"]) > now:
                pol["closed"] = False
                pol["closed_reason"] = ""
            pol["notified"] = False
        self._dirty = True
        return True

    # ------------------------------------------------------------------ #
    # 查账
    # ------------------------------------------------------------------ #
    def list_for(self, sid: str, *, active_only: bool = False,
                 now: Optional[float] = None) -> List[dict]:
        now = now if now is not None else time.time()
        out = []
        for pol in self._pol.values():
            if pol.get("sid") != sid:
                continue
            if active_only and (pol.get("closed") or
                                (float(pol.get("until") or 0) and now > float(pol["until"]))):
                continue
            out.append(pol)
        out.sort(key=lambda p: float(p.get("created") or 0), reverse=True)
        return out

    def find(self, sid: str, button_id: str = "", message_id: str = "",
             label: str = "", *, now: Optional[float] = None) -> Tuple[Optional[dict], List[dict]]:
        """按"当前会话 + 按钮/消息/名字"定位**对应那一本账**。

        返回 ``(命中, 候选列表)``：唯一命中就给策略；否则给候选让模型自己挑。
        """
        now = now if now is not None else time.time()
        if message_id:
            key = self._by_msg.get(str(message_id))
            if key and key in self._pol:
                return self._pol[key], []
        cands = self.list_for(sid, now=now)
        if button_id:
            _hit = [p for p in cands
                    if button_id in (p.get("buttons") or {})
                    or button_id in (p.get("button_ids") or [])]
            cands = _hit or cands
        if label:
            cands = [p for p in cands if str(p.get("label") or "") == label] or cands
        if len(cands) == 1:
            return cands[0], []
        return None, cands[:10]

    def stats(self, pol: dict, *, names: Optional[Dict[str, str]] = None,
              limit: int = 20) -> Dict[str, Any]:
        """把一本账整理成"给模型看"的结构（名字由调用方解析）。"""
        users = pol.get("users") or {}
        rows = []
        for uid, rec in users.items():
            nm = (names or {}).get(str(uid)) or ""
            rows.append({"uid": str(uid), "name": nm or f"未知用户({uid})",
                         "count": int(rec[0]), "first": float(rec[1]), "last": float(rec[2])})
        rows.sort(key=lambda r: (-r["count"], r["first"]))
        mx = int(pol.get("max") or 0)
        return {
            "key": pol.get("key"), "sid": pol.get("sid"), "label": pol.get("label") or "",
            "total": int(pol.get("total") or 0),
            "remaining": max(0, mx - int(pol.get("total") or 0)) if mx else None,
            "max": mx or None, "per": int(pol.get("per") or 0) or None,
            "once": bool(pol.get("once")), "cooldown": int(pol.get("cooldown") or 0) or None,
            "until": float(pol.get("until") or 0) or None,
            "closed": bool(pol.get("closed")), "closed_reason": pol.get("closed_reason") or "",
            "created": float(pol.get("created") or 0),
            "people": len(rows), "users": rows[:max(1, limit)],
            "users_total": len(rows),
            "deliver": self._effective_deliver(pol),
            "notify": bool(pol.get("notify")), "hard": bool(pol.get("hard")),
            "msgs": list(pol.get("msgs") or []),
        }

    # ------------------------------------------------------------------ #
    # 截止巡检（给 watcher 用）
    # ------------------------------------------------------------------ #
    def due_for_close(self, *, now: Optional[float] = None) -> List[dict]:
        """到点但还没通知过、且确实有人参与的策略（用于给模型一条"已截止"）。"""
        now = now if now is not None else time.time()
        out = []
        for pol in self._pol.values():
            until = float(pol.get("until") or 0)
            if not until or now <= until or pol.get("notified"):
                continue
            if int(pol.get("total") or 0) <= 0:
                # 没人点过 ⇒ **静默关闭**（标记已处理 + closed，不打扰模型）
                pol["notified"] = True
                self._close(pol, "expired", now)
                self._dirty = True
                continue
            self._close(pol, "expired", now)
            pol["notified"] = True
            self._dirty = True
            out.append(pol)
        return out

    # ------------------------------------------------------------------ #
    # 索引 / 持久化
    # ------------------------------------------------------------------ #
    def _index(self, pol: dict) -> None:
        for mid in pol.get("msgs") or []:
            self._by_msg[str(mid)] = pol["key"]
        ids = set(pol.get("button_ids") or []) | set(pol.get("buttons") or {})
        for bid in ids:
            lst = self._by_sid_btn.setdefault(f"{pol['sid']}|{bid}", [])
            if pol["key"] not in lst:
                lst.insert(0, pol["key"])
                del lst[12:]

    def _rebuild_index(self) -> None:
        self._by_msg.clear()
        self._by_sid_btn.clear()
        for pol in self._pol.values():
            self._index(pol)
            tok = pol.get("token")
            if tok:
                self._by_token[str(tok)] = pol["key"]

    def _trim(self) -> None:
        while len(self._pol) > self.max_entries:
            key, _ = next(iter(self._pol.items()))
            self._pol.pop(key, None)
            self._dirty = True
        for mid, key in list(self._by_msg.items()):
            if key not in self._pol:
                self._by_msg.pop(mid, None)

    @staticmethod
    def _b36(n: int) -> str:
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        n = int(max(0, n))
        if n == 0:
            return "0"
        out = ""
        while n:
            n, r = divmod(n, 36)
            out = digits[r] + out
        return out

    @property
    def dirty(self) -> bool:
        return self._dirty

    def save(self) -> bool:
        """原子落盘（阻塞式，调用方丢线程）。"""
        if not self.path or not self._dirty:
            return False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            payload = {
                "version": self.FORMAT,
                "seq": self._seq,
                "policies": {k: v for k, v in self._pol.items()},
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
            return True
        except Exception:
            return False

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        self._seq = int(data.get("seq") or 0)
        pols = data.get("policies")
        if not isinstance(pols, dict):
            return
        for key, pol in pols.items():
            if not isinstance(pol, dict):
                continue
            pol.setdefault("key", str(key))
            pol.setdefault("sid", "")
            pol.setdefault("total", 0)
            pol.setdefault("users", {})
            pol.setdefault("buttons", {})
            pol.setdefault("button_ids", list(pol.get("buttons") or {}))
            pol.setdefault("msgs", [])
            pol.setdefault("closed", False)
            pol.setdefault("closed_reason", "")
            pol.setdefault("deliver", "all")
            pol.setdefault("hard", True)
            pol.setdefault("notify", False)
            pol.setdefault("notify_text", "")
            pol.setdefault("label", "")
            pol.setdefault("until", 0.0)
            pol.setdefault("created", 0.0)
            pol.setdefault("notified", False)
            pol["deliver_override"] = self._effective_deliver(pol)
            self._pol[str(key)] = pol
        self._rebuild_index()
        self._dirty = False


# --------------------------------------------------------------------------- #
# token 小工具（可选：把策略 id 隐式带在 `action.data` 前缀里）
# --------------------------------------------------------------------------- #
TOKEN_MARK = "~"


def token_of(data: str) -> str:
    """取出 `~ab12|原data` 里的 token（没有则空串）。"""
    text = str(data or "")
    if not text.startswith(TOKEN_MARK):
        return ""
    head, sep, _rest = text.partition("|")
    if not sep:
        return ""
    tok = head[len(TOKEN_MARK):]
    return tok if tok.isalnum() and len(tok) <= 8 else ""


def strip_token(data: str) -> str:
    """剥掉 token 前缀，保证模型看到的是它自己写的原值。"""
    text = str(data or "")
    if not text.startswith(TOKEN_MARK):
        return text
    _head, sep, rest = text.partition("|")
    return rest if sep else text


def embed_token(payload: dict, tokens: Dict[str, str], *, max_len: int = 100) -> int:
    """把 token 写进各按钮的 `action.data` 前缀；返回写入个数。

    只在**不超长**时写（`action.data` 官方硬限制 100 字符）；
    非字符串 data 一律不动（保持原样）。
    """
    n = 0
    content = (payload or {}).get("content")
    rows = (content or {}).get("rows") if isinstance(content, dict) else None
    if not isinstance(rows, list):
        return 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        for btn in (row.get("buttons") or []):
            if not isinstance(btn, dict):
                continue
            bid = str(btn.get("id") or "")
            tok = tokens.get(bid)
            action = btn.get("action")
            if not tok or not isinstance(action, dict):
                continue
            data = action.get("data")
            if not isinstance(data, str) or data.startswith(TOKEN_MARK):
                continue
            cand = f"{TOKEN_MARK}{tok}|{data}"
            if len(cand) <= max_len:
                action["data"] = cand
                n += 1
    return n


# --------------------------------------------------------------------------- #
# 正文里的判定注记（省 token：默认极简）
# --------------------------------------------------------------------------- #
_REASON_TEXT = {
    "full": "名额已满",
    "expired": "已过期",
    "closed": "已截止",
    "repeat": "此人之前已点过",
    "over_user": "此人次数已达上限",
    "cooldown": "连点（未计入）",
    "manual": "已手动截止",
    "ok": "",
}


def render_note(pol: dict, verdict: Dict[str, Any], *,
                names: Optional[Dict[str, str]] = None,
                with_names: bool = True) -> str:
    """生成挂在正文后面的短注记（模型据此接话）。"""
    reason = str(verdict.get("reason") or "ok")
    total = int(verdict.get("total") or 0)
    remain = verdict.get("remaining")
    parts: List[str] = []
    if reason == "ok":
        if verdict.get("closing"):
            mx = int(pol.get("max") or 0)
            parts.append(f"✅ 这是最后一个名额（{total}/{mx}），按钮已截止")
        else:
            if pol.get("max"):
                parts.append(f"第 {total}/{int(pol['max'])} 次" +
                             (f"，剩 {remain}" if remain else ""))
            else:
                parts.append(f"第 {total} 次")
        if verdict.get("user_count"):
            parts.append(f"此人第 {int(verdict['user_count'])} 次")
    else:
        txt = _REASON_TEXT.get(reason, reason)
        parts.append(txt if reason in ("cooldown",) else f"{txt}（未计入）")
        if pol.get("max"):
            parts.append(f"计数 {total}/{int(pol['max'])}")

    if with_names and (verdict.get("closing") or reason in ("full", "expired", "closed", "manual")):
        rows = []
        for uid, rec in (pol.get("users") or {}).items():
            nm = (names or {}).get(str(uid))
            rows.append((str(nm or ""), int(rec[0]), float(rec[1])))
        rows.sort(key=lambda r: r[2])
        shown = [r[0] for r in rows[:SUMMARY_NAMES] if r[0]]
        if shown:
            more = len(rows) - len(shown)
            parts.append("名单：" + "、".join(shown) + (f" 等 {more} 人" if more > 0 else ""))
    return "（" + "；".join(p for p in parts if p) + "）" if parts else ""


def render_stats_text(st: Dict[str, Any], *, now: Optional[float] = None,
                      name_of=None) -> str:
    """把 `stats()` 的字典渲染成一段给模型看的**紧凑**中文。"""
    now = now if now is not None else time.time()
    label = st.get("label") or st.get("key") or "按钮"
    head = f"【按钮账】{label}（{st.get('key')}）"
    bits = [f"已点 {st.get('total', 0)} 次"]
    if st.get("max"):
        bits.append(f"名额 {st['total']}/{st['max']}，剩 {st.get('remaining', 0)}")
    else:
        bits.append("未设总名额")
    if st.get("per"):
        bits.append(f"每人限 {st['per']} 次")
    if st.get("once"):
        bits.append("每人限一次")
    if st.get("cooldown"):
        bits.append(f"连点保护 {st['cooldown']}s")
    until = st.get("until")
    if until:
        left = int(until - now)
        bits.append(f"截止 {time.strftime('%m-%d %H:%M', time.localtime(until))}"
                    + (f"（剩 {left}s）" if left > 0 else "（已过）"))
    if st.get("closed"):
        rt = st.get("closed_reason") or "closed"
        bits.append("状态：已截止（" + (_REASON_TEXT.get(rt) or rt) + "）")
    else:
        bits.append("状态：进行中")
    lines = [head + "｜" + "｜".join(bits)]
    users = st.get("users") or []
    if users:
        lines.append(f"参与 {st.get('people', len(users))} 人（按次数排序，最多列 20）：")
        for r in users:
            nm = (name_of(r["uid"]) if callable(name_of) else None) or r.get("name") or \
                 f"未知用户({r['uid']})"
            t = time.strftime("%H:%M", time.localtime(r["last"]))
            lines.append(f"- {nm} × {r['count']}（最后 {t}）")
    else:
        lines.append("还没有人点。")
    return "\n".join(lines)


__all__ = [
    "ButtonPolicyStore", "parse_until", "spec_from_attrs",
    "split_keyboard_declaration", "render_note", "render_stats_text",
    "token_of", "strip_token", "embed_token",
]
