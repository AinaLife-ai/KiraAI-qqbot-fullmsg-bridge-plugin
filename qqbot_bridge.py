"""QQ Official Bot -> KiraAI bridge core (pure mechanics, no KiraAI imports).

Two jobs
--------
1. **Parse table patch.** qq-botpy builds its dispatch table from ``parse_*``
   methods and has no ``parse_group_message_create``, so QQ's "full group
   message" event dies in ``gateway.on_message`` with
   ``_parser unknown event group_message_create``. We add the parser at class
   level (future connections) and instance level (the live one).

2. **Event materialisation.** @-messages, full group messages and C2C messages
   all go through :func:`build_event`, so that

   * ``is_mentioned`` matches NapCat semantics (only a real @ counts);
   * the sender nickname is the **real QQ nickname** from ``author.username``
     (KiraAI's built-in adapter sets ``nickname = user_id``, i.e. the OpenID);
   * duplicate ``msg_id`` pushes collapse (QQ re-pushes by design, and a message
     may show up on both the AT and the full-message event).

Design notes that matter for performance / robustness
-----------------------------------------------------
* **Nothing here blocks.** No sync I/O, no locks, no unbounded loops: a message
  costs a handful of dict lookups plus one bounded LRU update.
* **Raw payloads, not botpy message objects.** botpy's ``GroupMessage`` exposes
  only ``content``/``mentions``/``attachments`` and drops ``message_type`` /
  ``msg_elements`` / ``is_you`` / ``author.username``. KiraAI's own
  ``_field_value`` accepts plain dicts, so handing over the raw body keeps quote
  (103) parsing, nicknames and mention detection working.
* **Optional adapter internals degrade instead of exploding.** Every private
  KiraAI attribute is read defensively so a future core refactor produces a
  clear log line, not a per-message traceback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from typing import Any, Callable, Iterable, Optional, Sequence

EVENT_GROUP_MESSAGE = "group_message_create"
EVENT_GROUP_AT_MESSAGE = "group_at_message_create"
EVENT_C2C_MESSAGE = "c2c_message_create"

#: dedup "kind" per event -- AT is the authoritative copy of an @-message.
KIND_FULL = "fm"
KIND_AT = "at"
KIND_DM = "dm"

_MARK = "_kira_qqbot_fullmsg_bridge"

#: adapter internals we cannot work without (checked once per adapter).
REQUIRED_ADAPTER_ATTRS = ("_message_chain", "_remember_reply_id", "publish")


# --------------------------------------------------------------------------- #
# Dispatch table plumbing
# --------------------------------------------------------------------------- #
def install_class_parser(cls: type, event_name: str, force: bool = False) -> str:
    """Attach ``ConnectionState.parse_<event_name>`` (idempotent).

    Returns ``patched`` | ``already`` | ``foreign``.  ``foreign`` means someone
    else already provides a *different* implementation; we only override it when
    ``force`` is set (used for the AT/C2C parsers we deliberately take over).

    When we replace an existing parser we stash it on the new function, so
    :func:`restore_class_parser` can put it back without a process restart.
    """
    attr = "parse_" + event_name
    existing = getattr(cls, attr, None)
    if existing is not None and getattr(existing, _MARK, False):
        return "already"
    if existing is not None and not force:
        return "foreign"

    def parser(self, payload):
        # Raw gateway payload -> client handler (see module docstring).
        self._dispatch(event_name, payload)

    parser.__name__ = attr
    parser.__qualname__ = f"{cls.__name__}.{attr}"
    setattr(parser, _MARK, True)
    setattr(parser, "_kira_bridge_original", existing)
    setattr(cls, attr, parser)
    return "patched"


def restore_class_parser(cls: type, event_name: str) -> bool:
    """Undo :func:`install_class_parser` (idempotent).  True if we changed it."""
    attr = "parse_" + event_name
    current = getattr(cls, attr, None)
    if current is None or not getattr(current, _MARK, False):
        return False
    original = getattr(current, "_kira_bridge_original", None)
    try:
        if original is None:
            delattr(cls, attr)
        else:
            setattr(cls, attr, original)
    except Exception:
        return False
    return True


def inject_live_parser(state: Any, event_name: str, force: bool = False) -> bool:
    """Add/replace the parser in an already-built ``ConnectionState.parsers``.

    When we replace an existing entry we stash it, so :func:`drop_live_parser`
    can put botpy's own parser back instead of deleting the key.
    """
    parsers = getattr(state, "parsers", None)
    dispatch = getattr(state, "_dispatch", None)
    if not isinstance(parsers, dict) or dispatch is None:
        return False
    existing = parsers.get(event_name)
    if existing is not None and getattr(existing, _MARK, False):
        return False
    if existing is not None and not force:
        return False

    def parser(payload, _dispatch=dispatch, _name=event_name):
        _dispatch(_name, payload)

    setattr(parser, _MARK, True)
    setattr(parser, "_kira_bridge_original", existing)
    parsers[event_name] = parser
    return True


def drop_live_parser(state: Any, event_name: str) -> bool:
    """Undo :func:`inject_live_parser` on a live ``ConnectionState.parsers``."""
    parsers = getattr(state, "parsers", None)
    if not isinstance(parsers, dict):
        return False
    current = parsers.get(event_name)
    if current is None or not getattr(current, _MARK, False):
        return False
    original = getattr(current, "_kira_bridge_original", None)
    if original is None:
        parsers.pop(event_name, None)
    else:
        parsers[event_name] = original
    return True


def client_has_native_handler(client: Any, attr_name: str) -> bool:
    return attr_name in type(client).__dict__


def attach_client_handler(
    client: Any, attr_name: str, handler: Callable[..., Any],
    allow_shadow: bool = False, owner: Any = None,
) -> str:
    """Bind an async handler as an **instance** attribute.

    ``Client.ws_dispatch`` resolves handlers via ``hasattr``/``getattr``, so an
    instance attribute is picked up and called as ``handler(payload)``.
    Returns ``attached`` | ``already`` | ``native`` (upstream owns it; for the
    AT/C2C paths we intentionally shadow it when ``allow_shadow`` is set).
    """
    if client_has_native_handler(client, attr_name) and not allow_shadow:
        return "native"
    current = getattr(client, attr_name, None)
    if getattr(current, _MARK, False):
        # ⚠ 热重载：旧插件实例留下的 handler 必须被新实例**接替**，否则跑的还是旧代码。
        # 同一个实例重复挂载则直接跳过（幂等）。
        if getattr(current, "_kira_bridge_owner", None) == (id(owner) if owner is not None else None):
            return "already"
    setattr(handler, _MARK, True)
    setattr(handler, "_kira_bridge_owner", id(owner) if owner is not None else None)
    setattr(client, attr_name, handler)
    return "attached"


def detach_client_handler(client: Any, attr_name: str) -> bool:
    """Remove our instance-level handler so the class (native) one takes over."""
    current = getattr(client, attr_name, None)
    if getattr(current, _MARK, False):
        try:
            delattr(client, attr_name)
            return True
        except Exception:
            return False
    return False


def check_adapter_capabilities(adapter: Any) -> list:
    """Return the list of missing adapter internals (empty == usable)."""
    return [name for name in REQUIRED_ADAPTER_ATTRS if not callable(getattr(adapter, name, None))]


# --------------------------------------------------------------------------- #
# Dedup
# --------------------------------------------------------------------------- #
class MessageDedup:
    """``(scope, msg_id) -> (kind, ts)`` bounded LRU.

    * same kind inside ``ttl`` -> ``dup``
    * an ``at`` copy arriving after a ``fm`` copy -> ``at_after_fm`` (the AT one
      must win: it carries the @ semantics)
    * an ``fm`` copy arriving after an ``at`` copy -> ``dup`` (AT already did it)
    """

    __slots__ = ("ttl", "maxlen", "_seen")

    def __init__(self, ttl: float = 180.0, maxlen: int = 4096):
        self.ttl = float(ttl)
        self.maxlen = int(maxlen)
        self._seen: "OrderedDict[str, tuple]" = OrderedDict()

    def classify(self, key: str, kind: str, now: Optional[float] = None) -> str:
        now = float(now if now is not None else time.time())
        seen = self._seen
        prev = seen.get(key)
        if prev is not None and now - prev[1] > self.ttl:
            seen.pop(key, None)
            prev = None
        if prev is None:
            self._record(key, kind, now)
            return "new"
        prev_kind = prev[0]
        if prev_kind == kind:
            self._record(key, kind, now)
            return "dup"
        if prev_kind == KIND_FULL and kind == KIND_AT:
            self._record(key, kind, now)  # AT is authoritative from now on
            return "at_after_fm"
        # prev is AT (or DM) and this is the weaker full-message copy
        seen.move_to_end(key)
        seen[key] = (prev_kind, now)
        return "dup"

    def kind_of(self, key: str) -> Optional[str]:
        prev = self._seen.get(key)
        return prev[0] if prev else None

    def _record(self, key: str, kind: str, now: float) -> None:
        seen = self._seen
        if key in seen:
            seen.move_to_end(key)
        seen[key] = (kind, now)
        while len(seen) > self.maxlen:
            seen.popitem(last=False)


# --------------------------------------------------------------------------- #
# Nickname directory (automatic, zero maintenance)
# --------------------------------------------------------------------------- #
class IdentityStore:
    """Remembers ``(adapter, scope, uid) -> nickname``.

    Nicknames arrive with almost every event, so this is a *fallback* for the
    rare payload that has no ``author.username`` -- and it keeps the last known
    name across sessions.  Nothing here is maintained by hand.

    **Persistence never runs on the message path**: ``remember()`` only marks the
    store dirty; :meth:`save` (called from a worker thread by the plugin) does
    the write.
    """

    __slots__ = ("path", "max_entries", "_store", "_dirty")

    def __init__(self, path: Optional[str] = None, max_entries: int = 4000):
        self.path = path
        self.max_entries = int(max_entries)
        self._store: "OrderedDict[str, str]" = OrderedDict()
        self._dirty = False
        if path:
            self.load()

    def remember(self, adapter: str, scope: str, uid: str, nickname: Optional[str]) -> Optional[str]:
        """Record a nickname (when present) and return the best known one."""
        key = f"{adapter}|{scope}|{uid}"
        store = self._store
        if nickname:
            if store.get(key) != nickname:
                self._dirty = True
            store[key] = nickname
            store.move_to_end(key)
            while len(store) > self.max_entries:
                store.popitem(last=False)
            return nickname
        return store.get(key)

    @property
    def dirty(self) -> bool:
        return self._dirty

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._store = OrderedDict((str(k), str(v)) for k, v in data.items())
                self._dirty = False
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def save(self) -> bool:
        """Flush to disk.  Blocking by design -- call it off the event loop."""
        if not self.path or not self._dirty:
            return False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(dict(self._store), fh, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
            return True
        except Exception:
            return False


# --------------------------------------------------------------------------- #
# @ 标记解析 / 机器人自我身份
#
# 官方 bot 的群消息里，@ 是以富文本标记的形式留在 content 里的：
#     <@0A0B9F323E6AA18BF08B6901A3B2DEFC> 妹
# 而 mentions 数组只给出被 @ 者的 id / username。KiraAI 原生适配器完全不碰这两个
# 字段，于是 LLM 看到的是一串 32 位 hex —— **连"有人在叫我"都看不出来**。
#
# 这里做三件事：
#   ① 学出"机器人自己"的 OpenID（优先 mentions[].is_you；其次 bot=true 且昵称与
#      机器人名字一致；最后兜底：把"内容里有、mentions 里查不到的 @"当作自己 ——
#      平台本来就会把机器人从 mentions 里摘掉，这个反推在实践中往往成立）；
#   ② 把 <@openid> 解析成 KiraAI **标准 At 元素**：``[At 昵称(pid)]`` ——
#      名字给人看，**pid（平台下发的 openid）才是身份**；
#      ★ 只留昵称是不安全的：昵称用户随时能改，把名字改成机器人名字就能冒充。
#      KiraAI 原版就是这个约定（OneBot 路径甚至是 ``[At QQ号]``，只有 id 没有名字）。
#   ③ 一旦内容里出现了"自己的 @"，就强制 is_mentioned=True —— 这是比 mentions
#      更硬的一条唤醒证据，不依赖平台给不给 is_you。
# --------------------------------------------------------------------------- #

#: QQ 富文本 @ 标记。收到的事件里可能是 `<@openid>`，也可能是
#: `<qqbot-at-user id="openid" />`（后者是平台发送侧用的规范形式），两种都认。
AT_MARKUP_RE = re.compile(
    r'<@([0-9A-Za-z]{8,})>|<qqbot-at-user\s+id="([0-9A-Za-z]{8,})"\s*/?>'
)


def at_user_markup(pid, style: str = "legacy") -> str:
    """发送侧 @ 某人的标记 —— 客户端会渲染成**真正的 @**。

    官方《文本交互》文档给了两种写法：

    * ``new``（文档推荐）：``<qqbot-at-user id="openid" />`` —— 但**实测在群里被当纯文本
      原样显示**（用户反馈），疑与环境/版本有关；
    * ``legacy``（**默认**）：``<@openid>`` —— 文档标注"即将弃用"，可它正是**平台自己
      下发给我们**用的形态（入站 content 里的 @ 就是它），客户端一定认。

    ⇒ 默认 ``legacy``（"平台自己在用什么，我就用什么"），可用 ``at_markup_style`` 切到 ``new``。
    """
    if str(style).lower() == "new":
        return '<qqbot-at-user id="%s" />' % pid
    return "<@%s>" % pid


#: 模型会**模仿历史里出现过的 @ 标记**：群里已经发出去过 ``<qqbot-at-user id="…" />``，
#: 模型就会在正文里照样写一份。它是普通字符串、不是 At 元素，会被原样发出去变成一串文本。
OUTGOING_AT_MARKUP_RE = re.compile(
    r'<qqbot-at-user\s+id=["\']?([A-Za-z0-9_\-:]{6,})["\']?\s*/?>'
    r'|<@!?([A-Za-z0-9_\-:]{6,})>'
)


def normalize_outgoing_markup(text: str, style: str = "legacy"):
    """把正文里**模型自己写出来**的 @ 标记归一成配置的形态。

    返回 ``(新文本, 是否改动过)``。没改动过时原样返回，避免无谓的拷贝。
    """
    if not text or ("<@" not in text and "qqbot-at-user" not in text):
        return text, False

    def _sub(m):
        pid = m.group(1) or m.group(2) or ""
        return at_user_markup(pid, style) if pid else m.group(0)

    fixed = OUTGOING_AT_MARKUP_RE.sub(_sub, text)
    return fixed, fixed != text


def strip_at_markup(text: str) -> str:
    """把正文里的 @ 标记整个去掉（markdown 发不出去时的兜底）。

    宁可少一个 @，也不要把 `<qqbot-at-user id="…" />` 这种标签原样发到群里。
    """
    if not text or ("<@" not in text and "qqbot-at-user" not in text):
        return text
    return OUTGOING_AT_MARKUP_RE.sub("", text)


def _match_at_id(match) -> str:
    """两种形态取其中之一。"""
    return match.group(1) or match.group(2) or ""


class SelfIdentity:
    """机器人自己的身份（OpenID + 昵称）。由调用方持有，build_event 就地补全。"""

    __slots__ = ("openid", "name", "source")

    def __init__(self, openid=None, name=None, source=None):
        self.openid = openid
        self.name = name
        self.source = source


def extract_at_ids(text) -> list:
    """抓出 content 里所有 @ 标记的 openid（两种形态都算）。"""
    if not isinstance(text, str) or ("<@" not in text and "qqbot-at-user" not in text):
        return []
    return [oid for oid in (_match_at_id(m) for m in AT_MARKUP_RE.finditer(text)) if oid]


def mention_name_map(body: dict) -> dict:
    """mentions[] -> {openid: username}（只收带昵称的项）。"""
    out = {}
    for item in normalize_mentions(body.get("mentions")):
        mid = item.get("id")
        name = item.get("username")
        if mid is not None and name:
            out[str(mid)] = str(name)
    return out


def learn_self_from_mentions(body: dict, robot_name=None):
    """从 mentions 里认出机器人自己的 openid。返回 (openid, 来源) 或 (None, None)。"""
    mentions = normalize_mentions(body.get("mentions"))
    for item in mentions:
        if item.get("is_you") is True and item.get("id") is not None:
            return str(item["id"]), "is_you"
    if robot_name:
        for item in mentions:
            if item.get("bot") and str(item.get("username") or "") == str(robot_name) \
                    and item.get("id") is not None:
                return str(item["id"]), "bot+name"
    return None, None


def quoted_author_is_self(body: dict, self_identity=None):
    """引用消息的作者是不是机器人自己 —— 对齐 KiraAI 的 OneBot 语义。

    KiraAI 的 OneBot 路径（``core/adapter/src/qq/qq.py``）遇到 ``reply`` 段会
    ``get_msg`` 反查被引用消息的作者，若 ``user_id == self_id`` 就置
    ``is_mentioned = True`` —— **「回复机器人自己的消息」在框架里就等于被提及**。

    QQ 官方这边更省事：被引用消息的 ``author`` 直接就在 ``msg_elements[0]`` 里，
    不用额外请求接口。

    返回 ``(是否自己, 可顺便学到的 openid 或 None)``。
    """
    if not looks_like_quote(body):
        return False, None
    elements = body.get("msg_elements")
    if not isinstance(elements, list) or not elements:
        return False, None
    first = elements[0]
    author = first.get("author") if isinstance(first, dict) else None
    if not isinstance(author, dict):
        return False, None

    oid = author.get("id")
    name = str(author.get("username") or "")
    known = self_identity.openid if self_identity is not None else None

    if known is not None and oid is not None and str(oid) == str(known):
        return True, None
    if author.get("bot") is True and self_identity is not None and self_identity.name:
        if name and name == str(self_identity.name):
            return True, (str(oid) if oid is not None else None)
    return False, None


def _scene_ext_value(body: dict, key: str):
    """从 ``message_scene.ext`` 里取 ``key=value`` 形式的扩展字段（尽量宽容）。

    官方文档里它长这样::

        "message_scene": {"ext": ["msg_idx=REFIDX_xxx==", "ref_msg_idx=REFIDX_yyy=="]}

    实测口径可能有出入（URL 编码 / 多余空格 / 引号 / 字典形式），这里都兜一层，
    免得解析不出来导致「引用回复」静默失效。
    """
    scene = body.get("message_scene")
    if not isinstance(scene, dict):
        return None
    ext = scene.get("ext")
    if isinstance(ext, dict):
        items = ["%s=%s" % (k, v) for k, v in ext.items()]
    elif isinstance(ext, list):
        items = [x for x in ext if isinstance(x, str)]
    else:
        return None
    prefix = key + "="
    for item in items:
        text = item.strip()
        if not text.startswith(prefix):
            continue
        value = text[len(prefix):].strip().strip('"').strip("'")
        try:
            from urllib.parse import unquote

            value = unquote(value)
        except Exception:
            pass
        if value:
            return value
    return None


def extract_msg_idx(body: dict):
    """本条消息的 ``msg_idx`` —— 也就是"引用回复它"时该填的 REFIDX。"""
    return _scene_ext_value(body, "msg_idx")


def extract_ref_msg_idx(body: dict):
    """被引用消息的 ``ref_msg_idx``（引用消息里指向它引用的那条）。"""
    return _scene_ext_value(body, "ref_msg_idx")


def extract_sent_ref_idx(result) -> Optional[str]:
    """从发消息的响应里取 ``ext_info.ref_idx`` —— 引用"机器人自己发过的消息"要用它。"""
    if not isinstance(result, dict):
        result = getattr(result, "__dict__", None) or {}
    ext = result.get("ext_info") if isinstance(result, dict) else None
    if isinstance(ext, dict):
        ref = ext.get("ref_idx")
        return str(ref) if ref else None
    return None


#: 语音附件在官方 payload 里的 content_type（不是 mime，框架会误判成 File）
VOICE_CONTENT_TYPES = {"voice", "silk", "audio/silk", "amr", "audio/amr"}

#: 内容里的 QQ 表情标记：<faceType=6, faceId="0", ext="<base64 JSON>">
FACE_MARKUP_RE = re.compile(r'<faceType=\d+,\s*faceId="[^"]*",\s*ext="([^"]*)"\s*>')

#: 解码出来的表情描述最长保留多少字
_FACE_TEXT_MAX = 40


def _decode_face_ext(ext: str):
    """表情标记里的 ext 是 base64(JSON)，里面带可读描述（键名通常是 text）。"""
    if not ext:
        return None
    try:
        import base64 as _b64

        raw = _b64.b64decode(ext + "=" * (-len(ext) % 4))
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("text", "desc", "description", "prompt", "summary", "name", "title"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_FACE_TEXT_MAX]
    return None


def render_ark_card(body: dict):
    """把结构化卡片（message_type=3 + ark_data）渲染成一行可读文本。

    框架的 ``_content_elements`` 只认 content/attachments，卡片会被丢成
    "[Unsupported message]" —— LLM 完全不知道对方发了什么。
    """
    ark = body.get("ark_data")
    if not isinstance(ark, dict):
        return None
    fields = ark.get("fields") if isinstance(ark.get("fields"), dict) else {}

    def _pick(*keys):
        for source in (ark, fields):
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    kind = _pick("ark_name", "ark_type") or "卡片"
    title = _pick("title") or ""
    desc = _pick("desc", "prompt") or ""
    detail = " - ".join(x for x in (title, desc) if x)
    return f"[卡片: {kind}{' - ' + detail if detail else ''}]"


def _normalize_element(elem, out_notes):
    """对单个元素（顶层 body 或 msg_elements 里的条目）做富内容归一化。"""
    if not isinstance(elem, dict):
        return elem
    new, notes = normalize_rich_body(elem, enhance=True, _depth=1)
    if notes and isinstance(out_notes, list):
        out_notes.extend(notes)
    return new


def normalize_rich_body(body: dict, enhance: bool = True, _depth: int = 0):
    """把官方 bot 特有的、框架不认识的几种形态转成框架能读的样子。

    顶层 body 与 `msg_elements[]` 里的**引用元素**都会处理（``_depth`` 防递归）——
    否则"引用一条语音"时，被引用的语音不会被归一化，LLM 读不出来。

    ① **语音**：官方把 ``content_type`` 写成 ``voice``（不是 mime），框架会判成 File。
       有平台自带的 ``asr_refer_text``（腾讯免费 ASR）时直接用它当文本，不再跑本地 STT；
       没有时把附属信息归一化成音频，并用 ``voice_wav_url`` 替换 url（WAV 更好转写）。
    ② **结构化卡片**（``message_type=3``）：渲染成 ``[卡片: ...]`` 文本。
    ③ **QQ 表情标记** ``<faceType=.., faceId=.., ext="base64">``：解码 ext，渲染成 ``[表情: xxx]``。

    返回 ``(新 body, 变更说明列表)``；没有任何改动时原样返回。绝不抛异常。
    """
    if not enhance or not isinstance(body, dict):
        return body, []
    notes = []
    new = body

    try:
        # ① 语音
        attachments = body.get("attachments")
        if isinstance(attachments, list) and attachments:
            kept, extra_text, changed = [], [], False
            for att in attachments:
                if not isinstance(att, dict):
                    kept.append(att)
                    continue
                ct = str(att.get("content_type") or "").lower()
                if ct in VOICE_CONTENT_TYPES:
                    changed = True
                    asr = att.get("asr_refer_text")
                    if isinstance(asr, str) and asr.strip():
                        # 平台已经给了识别结果 → 用它当文本，跳过本地 STT
                        extra_text.append(f"[语音: {asr.strip()}]")
                        notes.append("voice-asr")
                        continue
                    att = dict(att)
                    wav = att.get("voice_wav_url")
                    if isinstance(wav, str) and wav:
                        att["url"] = wav
                        att["content_type"] = "audio/wav"
                    else:
                        att["content_type"] = "audio/silk"
                    notes.append("voice-as-audio")
                kept.append(att)
            if changed:
                new = dict(new)
                new["attachments"] = kept
                if extra_text:
                    new["content"] = (str(new.get("content") or "") + "".join(extra_text)).strip()

        # ② 结构化卡片
        try:
            _mtype = int(body.get("message_type") or 0)
        except (TypeError, ValueError):
            _mtype = 0
        if _mtype in (3, 103) or isinstance(body.get("ark_data"), dict):
            card = render_ark_card(body)
            if card:
                content = str(new.get("content") or "")
                if card not in content:
                    new = dict(new)
                    new["content"] = (content + " " + card).strip()
                    notes.append("ark-card")

        # ③ 表情标记
        content = new.get("content")
        if isinstance(content, str) and "faceType=" in content:
            replaced = 0

            def _sub(match):
                nonlocal replaced
                text = _decode_face_ext(match.group(1))
                replaced += 1
                return f"[表情: {text}]" if text else "[表情]"

            new_content = FACE_MARKUP_RE.sub(_sub, content)
            if replaced and new_content != content:
                new = dict(new)
                new["content"] = new_content
                notes.append("face-markup")
        # ④ 引用消息里的元素同样归一化
        if _depth == 0:
            elements = new.get("msg_elements")
            if isinstance(elements, list) and elements:
                fixed = [_normalize_element(elem, notes) for elem in elements]
                if fixed != elements:
                    new = dict(new)
                    new["msg_elements"] = fixed
                    notes.append("quoted-elements")
    except Exception:
        return body, []

    return new, notes


def looks_like_quote(body: dict) -> bool:
    """这条消息是不是一条「引用回复」。

    ``message_type == 103`` 是官方文档标注的引用消息；``message_reference`` 是旧格式。
    两者都不满足就按普通消息处理（避免把聊记/转发误判成引用）。
    """
    if body.get("message_reference"):
        return True
    try:
        return int(body.get("message_type") or 0) == 103
    except (TypeError, ValueError):
        return False


def split_at_markup(chain, body, self_identity=None, learn_self=True, At=None, Text=None,
                    count_self=True, _depth=0):
    """把 chain 里 ``Text`` 元素中的 ``<@openid>`` 拆成 KiraAI 标准 ``At`` 元素。

    **为什么不是纯文本替换**：昵称是用户随时能改的字段，只显示昵称的话，任何人把
    昵称改成机器人的名字就能冒充它。``At`` 的 ``pid`` 是平台下发的 openid（用户不可控），
    渲染成 KiraAI 标准格式 ``[At 昵称(pid)]`` —— 名字给人读，pid 做身份，两全。

    机器人自己的那个 ``At`` 名字带一个「（你）」后缀，避免"同名冒充"在语义上混淆。

    **引用消息里的 @ 也会被拆**（``Reply.chain`` 递归处理），但 ``count_self=False``：
    引用内容里的"自己的 @"**不算**本条消息在叫你 —— 那是被引用的历史消息，不是现在的呼唤。

    返回 ``(新 chain, 是否 @ 到自己, 新学到的 openid)``；任何一步出问题都原样返回。
    """
    if At is None or Text is None or not chain:
        return chain, False, None

    names = mention_name_map(body)
    known = self_identity.openid if self_identity is not None else None
    learned = None
    hit_self = False
    out = []

    def _text(value):
        try:
            return Text(value)
        except Exception:
            return value

    for ele in chain:
        # 引用消息：嵌套链同样拆 At（只做展示，不作为「叫自己」的判据）
        nested = getattr(ele, "chain", None)
        if nested is not None and hasattr(nested, "message_list") and _depth < 2:
            sub, _h, _l = split_at_markup(
                list(nested), body, self_identity, learn_self,
                At=At, Text=Text, count_self=False, _depth=_depth + 1,
            )
            try:
                ele.chain = type(nested)(sub)
            except Exception:
                pass

        text = getattr(ele, "text", None)
        if not isinstance(text, str) or ("<@" not in text and "qqbot-at-user" not in text):
            out.append(ele)
            continue
        pos = 0
        for match in AT_MARKUP_RE.finditer(text):
            oid = _match_at_id(match)
            if not oid:
                continue
            # 兜底学习：内容里有 @、但 mentions 里查不到 → 很可能就是机器人自己
            if known is None and learn_self and oid not in names:
                learned = oid
                known = oid
            head = text[pos:match.start()]
            if head:
                out.append(_text(head))
            is_self = known is not None and oid == known
            label = None
            if is_self:
                if count_self:
                    hit_self = True
                base = self_identity.name if self_identity is not None else None
                label = f"{base}（你）" if base else "你"
            elif oid in names:
                label = names[oid]
            try:
                out.append(At(oid, label) if label else At(oid))
            except Exception:
                out.append(_text(match.group(0)))
            pos = match.end()
        tail = text[pos:]
        if tail:
            out.append(_text(tail))
    return out, hit_self, learned


# --------------------------------------------------------------------------- #
# Mention semantics
# --------------------------------------------------------------------------- #
def normalize_mentions(raw: Any) -> list:
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            out.append(item)
        else:
            out.append(
                {
                    "id": getattr(item, "id", None),
                    "is_you": getattr(item, "is_you", None),
                    "bot": getattr(item, "bot", None),
                    "username": getattr(item, "username", None),
                }
            )
    return out


def detect_mention(body: dict, mode: str = "auto", self_ids: Iterable[Any] = ()) -> tuple:
    """Return ``(is_mentioned, source)`` for a **full-message** group event.

    NOTE: QQ's ``GROUP_AT_MESSAGE_CREATE`` documents ``mentions`` as
    "不含@机器人自身", so that path never relies on this -- it forces True.
    """
    mode = (mode or "auto").lower()
    if mode == "always":
        return True, "always"
    if mode == "never":
        return False, "never"

    mentions = normalize_mentions(body.get("mentions"))
    for item in mentions:
        if item.get("is_you") is True:
            return True, "is_you"

    ids = {str(x) for x in self_ids if x not in (None, "")}
    if ids:
        for item in mentions:
            mid = item.get("id")
            if mid is not None and str(mid) in ids and item.get("bot") is not False:
                return True, "self_id"
    return False, "none"


# --------------------------------------------------------------------------- #
# Event materialisation
# --------------------------------------------------------------------------- #
def normalize_body(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    inner = payload.get("d")
    if isinstance(inner, dict):
        return inner
    return payload


def dedup_key(body: dict, is_group: bool = True) -> str:
    """Stable dedup key ``<gm|dm>:<target>:<msg_id>`` for an event body."""
    if not isinstance(body, dict):
        return ""
    mid = body.get("id")
    if not mid:
        return ""
    if is_group:
        return "gm:%s:%s" % (body.get("group_openid") or "", mid)
    author = body.get("author") if isinstance(body.get("author"), dict) else {}
    return "dm:%s:%s" % (author.get("user_openid") or author.get("id") or "", mid)


def stable_alias(raw_id: str, width: int = 6) -> str:
    """Deterministic short alias -- never QQ's real id, just a readable handle."""
    return hashlib.sha1(str(raw_id).encode("utf-8")).hexdigest()[:width]


def build_event(
    adapter: Any,
    payload: Any,
    *,
    Group,
    User,
    KiraIMMessage,
    KiraMessageEvent,
    kind: str = KIND_FULL,
    is_group: bool = True,
    force_mention: bool = False,
    mention_mode: str = "auto",
    self_ids: Sequence[Any] = (),
    dedup: Optional[MessageDedup] = None,
    identities: Optional[IdentityStore] = None,
    alias_ids: bool = False,
    self_identity: Optional[SelfIdentity] = None,
    resolve_at: bool = True,
    learn_self: bool = True,
    reply_to_self_wakes: bool = True,
    enhance_rich: bool = True,
    notes: Optional[list] = None,
    At: Any = None,
    Text: Any = None,
    now: Optional[float] = None,
) -> tuple:
    """Turn one raw QQ payload into a KiraAI event.

    Returns ``(event, reason)``; ``event is None`` means dropped and ``reason``
    says why (``bad-payload`` / ``missing-ids`` / ``denied`` / ``duplicate``).
    """
    body = normalize_body(payload)
    if not isinstance(body, dict):
        return None, "bad-payload"

    author = body.get("author")
    if not isinstance(author, dict):
        author = {}
    if is_group:
        target_id = str(body.get("group_openid") or "")
        uid = str(author.get("member_openid") or author.get("id") or "")
    else:
        target_id = str(author.get("user_openid") or author.get("id") or "")
        uid = target_id
    if not target_id or not uid:
        return None, "missing-ids"

    is_allowed = getattr(adapter, "_is_allowed", None)
    if callable(is_allowed) and not is_allowed(target_id, is_group=is_group):
        return None, "denied"

    message_id = str(body.get("id") or "")
    if dedup is not None and message_id:
        verdict = dedup.classify(dedup_key(body, is_group), kind, now=now)
        if verdict == "dup":
            return None, "duplicate"

    if enhance_rich:
        body, rich_notes = normalize_rich_body(body)
        if rich_notes and isinstance(notes, list):
            notes.extend(rich_notes)

    chain_fn = getattr(adapter, "_message_chain", None)
    if not callable(chain_fn):
        return None, "no-chain-builder"
    chain = chain_fn(body, is_group=is_group, target_id=target_id)

    # ---- @ 富文本标记：拆成标准 At 元素（保留 pid），顺带认出"自己" ----
    if resolve_at:
        if self_identity is not None and self_identity.openid is None:
            oid, why = learn_self_from_mentions(body, self_identity.name)
            if oid is not None:
                self_identity.openid = oid
                self_identity.source = why
        split_chain, hit_self, learned = split_at_markup(
            chain, body, self_identity, learn_self=learn_self, At=At, Text=Text
        )
        # ⚠ split_at_markup 返回的是普通 list，**必须还原成原来的链类型** ——
        # KiraAI 的 MessageChain 才有 .message_list，插件（如 S 版 _process_media）
        # 会直接访问它；直接吐 list 会让所有插件当场 AttributeError。
        try:
            chain.message_list = list(split_chain)      # MessageChain：原地替换，保留对象
        except Exception:
            chain = split_chain
        if learned and self_identity is not None and self_identity.openid is None:
            self_identity.openid = learned
            self_identity.source = "unresolved-markup"
    else:
        hit_self = False

    # 「回复机器人自己的消息」= 被提及（对齐 OneBot 路径；官方 payload 里连作者都给了）
    reply_to_self = False
    if reply_to_self_wakes:
        reply_to_self, quoted_oid = quoted_author_is_self(body, self_identity)
        if quoted_oid and self_identity is not None and self_identity.openid is None:
            self_identity.openid = quoted_oid
            self_identity.source = "quoted-author"

    if force_mention:
        is_mentioned, source = True, "forced"
    elif is_group:
        is_mentioned, source = detect_mention(body, mention_mode, self_ids)
    else:
        is_mentioned, source = True, "dm"
    if reply_to_self and not is_mentioned:
        is_mentioned, source = True, "reply_to_self"
    if hit_self and not is_mentioned:
        # 内容里出现"自己的 @"——比 mentions 更硬的一条唤醒证据
        is_mentioned, source = True, "self_at_markup"

    nickname = str(author.get("username") or "").strip()
    if identities is not None:
        nickname = identities.remember(str(adapter.info.name), "gm" if is_group else "dm", uid, nickname) or ""
    if not nickname:
        nickname = stable_alias(uid) if alias_ids else uid

    display_id = ""
    if message_id:
        reply_ids = getattr(adapter, "_group_reply_ids" if is_group else "_direct_reply_ids", None)
        if isinstance(reply_ids, dict):
            reply_ids[target_id] = message_id
        remember = getattr(adapter, "_remember_reply_id", None)
        if callable(remember):
            try:
                display_id = remember(is_group, target_id, message_id) or ""
            except Exception:
                display_id = ""

    ts = int(now if now is not None else time.time())
    event = KiraMessageEvent(
        adapter=adapter.info,
        message_types=adapter.message_types,
        message=KiraIMMessage(
            timestamp=ts,
            group=Group(group_id=target_id, group_name=target_id) if is_group else None,
            sender=User(user_id=uid, nickname=nickname),
            is_mentioned=is_mentioned,
            message_id=display_id or message_id,
            self_id=adapter.app_id,
            chain=chain,
        ),
        timestamp=ts,
    )
    return event, source


def collect_self_ids(client: Any) -> list:
    """Best-effort read of the bot's own id from a botpy client."""
    try:
        rid, _ = collect_self_identity(client)
    except Exception:
        # botpy 的 client.robot 是个 property，未连接时 _connection 为 None 会抛
        return []
    return [rid] if rid is not None else []


def collect_self_identity(client: Any):
    """读 botpy client 上机器人自己的 (id, 昵称)。

    ``READY`` 之后 botpy 会挂上 ``client.robot``（``user.id`` / ``user.username``）。
    昵称用于把「机器人自己被 @」渲染成 ``@香里`` 而不是一串 hex。
    **全防御**：未连接时 ``client.robot`` 这个 property 会抛，不能让它冒到消息路径上。
    """
    try:
        robot = getattr(client, "robot", None)
    except Exception:
        return None, None
    if isinstance(robot, dict):
        return robot.get("id"), (robot.get("username") or robot.get("name"))
    if robot is not None:
        return getattr(robot, "id", None), getattr(robot, "name", None)
    return None, None
