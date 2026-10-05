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
    client: Any, attr_name: str, handler: Callable[..., Any], allow_shadow: bool = False
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
        return "already"
    setattr(handler, _MARK, True)
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

    if force_mention:
        is_mentioned, source = True, "forced"
    elif is_group:
        is_mentioned, source = detect_mention(body, mention_mode, self_ids)
    else:
        is_mentioned, source = True, "dm"

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

    chain_fn = getattr(adapter, "_message_chain", None)
    if not callable(chain_fn):
        return None, "no-chain-builder"

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
            chain=chain_fn(body, is_group=is_group, target_id=target_id),
        ),
        timestamp=ts,
    )
    return event, source


def collect_self_ids(client: Any) -> list:
    """Best-effort read of the bot's own id from a botpy client."""
    robot = getattr(client, "robot", None)
    if isinstance(robot, dict):
        rid = robot.get("id")
    elif robot is not None:
        rid = getattr(robot, "id", None)
    else:
        rid = None
    return [rid] if rid is not None else []
