"""在**提速器用的那个 `chat_stream` 上旁听**，把 token 实时投到私聊流式消息上。

## 为什么是这里（这一层才是"提速"的来源）

KiraAI 的回复路径是**非流式**的（`core/agent/agent_executor.py` 用
`await model.chat(request)`），核心没有把 token 增量暴露给插件。生态里真正在流式的是
**提速器插件**：它 patch `ProviderManager.get_model_client`，把返回的 client 包一层
`LLMClientProxy`，在 `chat()` 里改用 `client.chat_stream()` 收 SSE，并把已成型的
`<msg>` 段立刻经框架发送层发出来（`/var/minis/shared/acc_bg_20260929/stream_engine.py`）。

而我们**不动 LLM 客户端的 chat()**，只在**它调用的那个 `chat_stream` 上旁听**：

    LLMClientProxy.chat_stream  →  self._wrapped.chat_stream(request, **kwargs)
                                   ^^^^^^^^^^^^^^^^^^^ 我们包的就是这一层

⇒ 一次 LLM 调用还是**一次**（不会重复请求、不额外消耗 token），
   我们只是拿到每个 chunk 的增量文本，顺手把"当前已成型的那句话"投给 QQ。

## 三条铁律

1. **纯透传**：chunk 原样 `yield` 出去，时序、异常、结束语义都不改；
   观察代码只做"记文本"，**绝不 await 网络**（那会拖慢出字，与提速目的相反）。
2. **身份匹配**：只对"当前这一轮那个 LLMRequest 对象"生效（`request is` 判等），
   ⇒ 人设生成器等**其它** `chat_stream` 调用不会被误投到用户会话里。
3. **没有提速器也安全**：核心的回复路径不调 `chat_stream` ⇒ 这条通道自然静默，
   流式退回到"分段驱动"（框架发一段我们写一段），行为完全正常。
"""
from __future__ import annotations

import time
from typing import Any, Optional

#: 打在我们包装函数上的标记（幂等 + 还原时认得出来）
MARK = "_kira_bridge_chat_stream"
#: 一轮最多保留几个 request 上下文（每轮一个，正常 1~3 个）
MAX_TURNS = 8
#: 上下文最长存活时间（秒）—— 兜底清理，避免 map 无限涨
TURN_TTL = 300.0


class _Turn:
    """一次"进入 LLM 请求"的上下文（把 request 对象和会话目标绑在一起）。"""

    __slots__ = ("request", "adapter", "target", "msg_id", "client", "raw",
                 "tool_turn", "created", "saw_text")

    def __init__(self, request: Any, adapter: Any, target: str, msg_id: str,
                 client: Any):
        self.request = request
        self.adapter = adapter
        self.target = target
        self.msg_id = msg_id
        self.client = client
        self.raw = ""
        self.tool_turn = False
        self.saw_text = False
        self.created = time.time()


class LLMStreamBridge:
    """旁听 `chat_stream` ⇒ 驱动 C2C 流式消息的"预览"帧。"""

    def __init__(self, plugin: Any, logger: Any, enabled: bool = True):
        self.plugin = plugin
        self.logger = logger
        self.enabled = bool(enabled)
        self._orig: dict = {}          # id(client) -> (client, 原方法)
        self._turns: dict = {}         # id(request) -> _Turn
        self._logged: set = set()

    # ------------------------------------------------------------------ #
    # 安装 / 还原
    # ------------------------------------------------------------------ #
    def installed(self, client: Any) -> bool:
        return getattr(getattr(client, "chat_stream", None), MARK, False)

    def install(self, client: Any) -> bool:
        """把实例的 `chat_stream` 包一层（幂等；已包过返回 False）。"""
        if not self.enabled or client is None:
            return False
        current = getattr(client, "chat_stream", None)
        if not callable(current):
            return False
        if getattr(current, MARK, False):
            return False
        bridge = self

        async def chat_stream(request, **kwargs):
            turn = bridge.turn_for(request)
            async for chunk in current(request, **kwargs):
                if turn is not None:
                    try:
                        bridge._on_chunk(turn, chunk)
                    except Exception:
                        pass          # 观察失败绝不影响模型调用
                yield chunk

        setattr(chat_stream, MARK, True)
        setattr(chat_stream, "_kira_bridge_orig", current)
        try:
            client.chat_stream = chat_stream
        except Exception:
            return False
        self._orig[id(client)] = (client, current)
        while len(self._orig) > 32:       # 客户端被重建时不留垃圾
            self._orig.pop(next(iter(self._orig)))
        if "installed" not in self._logged:
            self._logged.add("installed")
            self._log_info(
                "[QQBOT-BRIDGE] 已在 LLM 流式通道上旁听（chat_stream 透传，不额外发请求）——"
                "提速器收到的 token 会实时投到私聊的流式消息上"
            )
        return True

    def restore(self) -> int:
        """还原全部包装（幂等）。"""
        count = 0
        for _key, (client, orig) in list(self._orig.items()):
            try:
                client.chat_stream = orig
                count += 1
            except Exception:
                pass
        self._orig.clear()
        self._turns.clear()
        return count

    # ------------------------------------------------------------------ #
    # 轮次上下文
    # ------------------------------------------------------------------ #
    def begin_turn(self, request: Any, adapter: Any, target: str, msg_id: str,
                   client: Any = None) -> None:
        """ON_LLM_REQUEST 时登记"这一轮属于哪个私聊会话"。"""
        if not self.enabled or request is None or not target:
            return
        now = time.time()
        for key in [k for k, t in list(self._turns.items())
                    if len(self._turns) >= MAX_TURNS or
                    now - getattr(t, "created", now) > TURN_TTL]:
            self._turns.pop(key, None)
        self._turns[id(request)] = _Turn(request, adapter, target, msg_id, client)
        # 提前把包装装好（等真正开始流式就来不及了）
        if client is not None:
            self.install(client)

    def turn_for(self, request: Any) -> Optional[_Turn]:
        """按**对象身份**找上下文（人设生成器等其它调用不会被误认）。"""
        if request is None:
            return None
        turn = self._turns.get(id(request))
        if turn is None or turn.request is not request:
            return None          # id 复用保护：必须是同一个对象
        return turn

    def end_turn(self, request: Any) -> None:
        if request is not None:
            self._turns.pop(id(request), None)

    def clear(self) -> None:
        self._turns.clear()

    # ------------------------------------------------------------------ #
    # 观察
    # ------------------------------------------------------------------ #
    def _on_chunk(self, turn: _Turn, chunk: Any) -> None:
        """每个 chunk：累积文本 + （非工具轮）把"已成型的那句话"投给流式消息。"""
        delta = getattr(chunk, "delta_text", None)
        if isinstance(delta, str) and delta:
            turn.raw += delta
        # 工具调用出现 ⇒ 这一轮不是给用户看的最终回复（与提速器同一条规矩）
        if getattr(chunk, "tool_calls_delta", None):
            turn.tool_turn = True
            return
        if turn.tool_turn or not turn.raw:
            return
        manager = getattr(self.plugin, "c2c_stream", None)
        if manager is None or not manager.enabled:
            return
        turn.saw_text = True
        manager.observe_raw(turn.adapter, turn.client, turn.target, turn.msg_id, turn.raw)

    # ------------------------------------------------------------------ #
    def _log_info(self, msg: str, *a: Any) -> None:
        try:
            self.logger.info(msg, *a)
        except Exception:
            pass
