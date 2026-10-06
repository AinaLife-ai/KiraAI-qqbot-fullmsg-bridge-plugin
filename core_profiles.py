"""核心世代探测与落点收敛（KiraAI v2 / v3 双兼容）。

为什么需要这个文件
------------------
KiraAI 3.0 把官方 bot 适配器**重写**了：

* 2.x：`QQOfficialAdapter` 自己持有 `_send_message` / `_text_content` /
  `_message_chain` / `_group_reply_ids` …（适配器 = 落点）
* 3.0：IM 相关的一切搬进 `QQOfficialIMCapability`，
  `adapter.get_capability(IMCapability)` 才拿得到（能力对象 = 落点），
  adapter 上只剩 `send_group_message` / `send_direct_message` 两个转发壳。

结果就是：**同一行 `adapter._send_message` 在 2.x 能拿到、在 3.0 拿到 None**。
桥接如果写死 adapter，装到 3.0 上会静默失效（实测：`check_adapter_capabilities`
缺失 `_message_chain`/`_remember_reply_id` ⇒ 整体停用）。

本模块把"落点"探测出来，让上层只认 `profile.xxx`，不再关心世代。

设计约束
--------
* **零导入失败**：所有 `import core.xxx` 都在 try 里，认不出就是 `unknown`。
* **零副作用**：只读属性，不改任何东西。
* **结构性判断**：不看版本号字符串（3.0 的 VERSION 是 `v3.0.0-alpha.2`，
  未来还会变），而是看"有没有那个能力对象 / 那个方法"。
"""

from __future__ import annotations

from typing import Any

#: 世代常量
GEN_V2 = "v2"
GEN_V3 = "v3"
GEN_UNKNOWN = "unknown"


class CoreProfile:
    """一次探测的结果。所有字段只读，探测后不再变。"""

    __slots__ = (
        "generation",
        "event_target",
        "send_target",
        "im_capability",
        "im_capability_cls",
        "parser_target",
        "core_owns_fullmsg",
        "detail",
    )

    def __init__(
        self,
        generation: str,
        event_target: Any = None,
        send_target: Any = None,
        im_capability: Any = None,
        im_capability_cls: Any = None,
        parser_target: Any = None,
        core_owns_fullmsg: bool = False,
        detail: str = "",
    ):
        self.generation = generation
        #: 构造/改写事件时该往谁身上挂（v2 = adapter，v3 = 不造，None）
        self.event_target = event_target
        #: 发送链落点（v2 = adapter，v3 = IMCapability）
        self.send_target = send_target
        #: 3.0 的 IMCapability 实例（2.x 为 None）
        self.im_capability = im_capability
        #: 3.0 的 IMCapability 类（权限判定要用）
        self.im_capability_cls = im_capability_cls
        #: 该补解析器的地方（v2 = botpy ConnectionState，v3 = None，核心自己装了）
        self.parser_target = parser_target
        #: 核心是否已自带"全量群消息"支持（v3 = True ⇒ 桥接不要重复接管）
        self.core_owns_fullmsg = core_owns_fullmsg
        #: 供日志展示的一句话说明
        self.detail = detail

    # ------------------------------------------------------------------ #
    @property
    def is_v2(self) -> bool:
        return self.generation == GEN_V2

    @property
    def is_v3(self) -> bool:
        return self.generation == GEN_V3

    @property
    def is_known(self) -> bool:
        return self.generation != GEN_UNKNOWN

    def __repr__(self) -> str:  # pragma: no cover - 仅日志
        return (
            f"CoreProfile(gen={self.generation}, send={type(self.send_target).__name__}, "
            f"fullmsg={self.core_owns_fullmsg}, detail={self.detail!r})"
        )


def _im_capability_cls():
    """3.0 的 IMCapability；2.x 没有 ⇒ None。"""
    try:
        from core.adapter.capabilities import IMCapability  # type: ignore

        return IMCapability
    except Exception:
        return None


def _has(obj: Any, name: str) -> bool:
    return obj is not None and callable(getattr(obj, name, None))


def detect(adapter: Any) -> CoreProfile:
    """探测一个适配器实例属于哪一代，并给出各层落点。

    探测顺序（先严后宽，任何一步异常都不影响下一步）：

    1. **v3**：`adapter.get_capability(IMCapability)` 拿到对象，且该对象上有
       `_handle_group_message`（这是 3.0 事件构造器，2.x 绝不会有）。
    2. **v2**：adapter 自己就是落点 —— 同时具备 `_send_message` 与 `_message_chain`。
    3. **unknown**：都不满足 ⇒ 上层只跑 L1（纯工具层，不需要任何落点）。
    """
    if adapter is None:
        return CoreProfile(GEN_UNKNOWN, detail="adapter is None")

    # ---- 1) 3.0 ----
    cap_cls = _im_capability_cls()
    get_cap = getattr(adapter, "get_capability", None)
    if cap_cls is not None and callable(get_cap):
        try:
            im = get_cap(cap_cls)
        except Exception:
            im = None
        if im is not None and _has(im, "_handle_group_message"):
            return CoreProfile(
                GEN_V3,
                event_target=None,          # 3.0 不造事件，只做增量
                send_target=im,
                im_capability=im,
                im_capability_cls=cap_cls,
                parser_target=None,        # 3.0 自己装了 parser
                core_owns_fullmsg=True,     # 3.0 原生支持全量群消息
                detail="3.0 capability layout (IMCapability)",
            )

    # ---- 2) 2.x ----
    if _has(adapter, "_send_message") and _has(adapter, "_message_chain"):
        return CoreProfile(
            GEN_V2,
            event_target=adapter,
            send_target=adapter,
            im_capability=None,
            im_capability_cls=None,
            parser_target=_connection_state_cls(),
            core_owns_fullmsg=False,
            detail="2.x flat layout (adapter owns everything)",
        )

    # ---- 3) 认不出 ----
    return CoreProfile(
        GEN_UNKNOWN,
        detail=(
            "unrecognised adapter layout "
            f"(cls={type(adapter).__name__}, "
            f"has _send_message={_has(adapter, '_send_message')}, "
            f"has _message_chain={_has(adapter, '_message_chain')})"
        ),
    )


def _connection_state_cls():
    """2.x 要补解析器的位置：`botpy.connection.ConnectionState`。"""
    try:
        import botpy.connection as _conn  # noqa: WPS433

        return _conn.ConnectionState
    except Exception:
        return None


def is_allowed(adapter: Any, profile: CoreProfile, target_id: str, is_group: bool) -> bool:
    """权限判定 —— 两版签名不同，这里抹平。

    * 2.x：`adapter._is_allowed(target_id, is_group=…)`
    * 3.0：`adapter.is_allowed(target_id, capability_type=IMCapability, permission=...)`

    任何一步探测失败都返回 True（**放行**）——桥接的定位是"让消息进得来"，
    权限过滤是核心的职责；这里只做"能否构造事件"的前置判断，宁可放行也不要把
    消息误杀（核心自己还会再判一次）。
    """
    if adapter is None:
        return True

    # 3.0 优先（有 get_capability 的一定是 3.0 布局）
    if profile is not None and profile.is_v3 and profile.im_capability_cls is not None:
        fn = getattr(adapter, "is_allowed", None)
        if callable(fn):
            permission = "im.group.receive" if is_group else "im.direct.receive"
            try:
                return bool(fn(target_id, capability_type=profile.im_capability_cls,
                                permission=permission))
            except Exception:
                return True

    # 2.x
    fn = getattr(adapter, "_is_allowed", None)
    if callable(fn):
        try:
            return bool(fn(target_id, is_group=is_group))
        except TypeError:
            try:
                return bool(fn(target_id, is_group))
            except Exception:
                return True
        except Exception:
            return True

    return True


def message_types_of(adapter: Any, profile: CoreProfile) -> list:
    """取"本适配器支持的消息元素类型"。

    * 3.0：`im._supported_elements`（首选），失败退回 `adapter.message_types`
      （deprecated property，会打 DeprecationWarning，但能读）
    * 2.x：`adapter.message_types` 是普通属性
    """
    if profile is not None and profile.is_v3 and profile.im_capability is not None:
        value = getattr(profile.im_capability, "_supported_elements", None)
        if isinstance(value, list):
            return list(value)
    value = getattr(adapter, "message_types", None)
    if isinstance(value, list):
        return list(value)
    return []


def default_message_types() -> list:
    """兜底（与两版核心的官方适配器声明一致）。"""
    return ["text", "img", "at", "reply", "record", "file", "video", "emoji"]
