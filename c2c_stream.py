"""C2C 流式消息（`stream_messages`）—— 把**一轮回复的多个分段**渲染成同一条会生长的消息。

## 这是官方能力，KiraAI 核心没有

官方接口（`POST /v2/users/{openid}/stream_messages`）：

    input_mode   replace：ContentRaw 为**当前全量正文**，须以已下发前缀开头
    input_state  1=生成中  10=生成结束
    index        分片序号，从 0 递增
    content_type text | markdown
    content_raw  当前全量文本
    msg_id       被动回复消息 ID（与 event_id 二选一）
    stream_msg_id 首片由服务端返回，**后续分片必须携带**
    msg_seq      同一条流的所有分片**共用一个** msg_seq（只有 index 递增）

腾讯官方 Node SDK 与 QQ 官方推荐的 Hermes 都实现了它（SDK 见
`src/streaming.ts`：默认节流 500ms、最低 300ms、限流码 50002/HTTP 429 时
指数退避并**推进 index**）。

## 为什么本插件要"分段驱动"而不是"逐 token"

KiraAI 的回复路径是**非流式**的（`core/agent/agent_executor.py` 用
`await model.chat(request)` 一次拿完整结果），核心**没有**把 token 增量暴露给插件。
但生态里已经有成熟的增量来源：

* 提速器插件（accelerator）会在 LLM 客户端内部改成 `chat_stream()`，
  把**已成型的 `<msg>` 段**立刻经**框架发送层**发出来（`stream_first.SegmentEmitter`）；
* 没有提速器时，框架也会按段依次调用发送层。

⇒ 对这个插件来说，**"一次发送"就是一段文本**。所以这里做的是：
**把同一个被动消息（同 msg_id / 同一轮）里的几段文本，合成一条流式消息**，
每段到达就 `replace` 一次正文，空闲后自动补 `input_state=10` 收尾。

## 兼容与安全（用户硬要求：不能引入兼容问题）

1. **只碰"纯文本私聊"**：`msg_type=0`、无 markdown / 键盘 / 媒体 / 引用；
   群里、图文、语音、按钮一律原样走原路径。
2. **失败立刻放手**：任何一步出错（含平台没开流式权限）⇒ 本次**回退成普通发送**，
   消息绝不丢失；连续失败两次后对该目标**冷却**，不再尝试。
3. **返回值与普通发送同形**（`{"id": ..., "ext_info": {...}}`）⇒ 框架上层
   （展示态 id、引用索引、`_sent_message_ids`）**完全照旧**，不用改任何代码。
4. **超时兜底**：流未及时收尾不影响任何逻辑；空闲 2.5s 自动补结束帧。
5. 段数上限（30）与最小间隔（500ms）兜住限流风险。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

#: 低于这个长度不流式（"嗯""好"这种一个字的消息，流式反而更慢更怪）
MIN_CHARS = 8
#: 空闲多久自动补 `input_state=10`（收尾帧）
IDLE_DONE_SECONDS = 2.5
#: 一条流最多发多少片（保险丝；超了收尾并回退普通发送）
MAX_FRAMES = 30
#: 两片之间的最小间隔（官方建议 500ms，最低 300ms）
MIN_FRAME_INTERVAL = 0.5
#: 命中限流（HTTP 429 / err_code 50002）时的重试次数（官方 MAX_FLUSH_RETRIES=3）
MAX_RATE_LIMIT_RETRIES = 3
#: 连续失败几次后对该目标冷却，不再尝试流式
FAILURE_COOLDOWN = 2
COOLDOWN_SECONDS = 600.0

STATE_GENERATING = 1
STATE_DONE = 10


class _Session:
    """一条流式消息的状态。"""

    __slots__ = ("target", "msg_id", "seq", "index", "stream_msg_id", "text",
                 "last_frame_at", "done", "content_type", "frames", "lock", "adapter")

    def __init__(self, target: str, msg_id: str, seq: int, content_type: str,
                 adapter: Any = None):
        self.adapter = adapter
        self.target = target
        self.msg_id = msg_id
        self.seq = seq
        self.index = 0
        self.stream_msg_id: Optional[str] = None
        self.text = ""
        self.last_frame_at = 0.0
        self.done = False
        self.content_type = content_type
        self.frames = 0
        self.lock = asyncio.Lock()


class C2CStreamManager:
    """私聊流式消息管理器（可整体关掉；关掉后行为与从前完全一致）。"""

    def __init__(self, plugin: Any, logger: Any, enabled: bool = True,
                 seq_base: int = 40000):
        self.plugin = plugin
        self.logger = logger
        self.enabled = bool(enabled)
        #: 流式消息的 msg_seq 与核心的 1..N **刻意错开**（同一条流共用一个 seq）
        self._seq_base = int(seq_base)
        self._seq_n = 0
        self._sessions: dict = {}
        self._tasks: dict = {}
        self._failures: dict = {}
        self._logged: set = set()
        #: 平台层面明确不可用（权限/未开通）⇒ 全局停用，不再浪费请求
        self._disabled_reason: str = ""

    # ------------------------------------------------------------------ #
    # 对外：尝试把这次发送变成"流式消息的一片"
    # ------------------------------------------------------------------ #
    def eligible(self, target: str, kwargs: dict) -> Optional[str]:
        """这次发送能不能走流式？返回内容类型（text/markdown）或 None。

        **判据刻意保守**（宁可不流式，也不要改变既有行为）：
        纯文本私聊消息、有一定长度、目标是 C2C。
        """
        if not self.enabled or self._disabled_reason:
            return None
        if int(kwargs.get("msg_type") or 0) != 0:
            return None
        if kwargs.get("markdown") or kwargs.get("keyboard") or kwargs.get("media"):
            return None
        if kwargs.get("message_reference"):
            return None                     # 引用消息的 id 语义不同，不掺和
        text = kwargs.get("content")
        if not isinstance(text, str) or len(text.strip()) < MIN_CHARS:
            return None
        if not self._cooled_down(target):
            return None
        return "text"

    async def maybe_stream(self, adapter: Any, client: Any, target: str,
                           kwargs: dict) -> Optional[dict]:
        """把这次发送写进流式消息。**返回 None 表示"没接管"**（调用方照常发送）。"""
        content_type = self.eligible(target, kwargs)
        if content_type is None:
            return None
        text = str(kwargs.get("content") or "")
        msg_id = str(kwargs.get("msg_id") or "")
        if not msg_id:                       # 主动发送没有 msg_id ⇒ 流式发不出去
            reply_ids = None
            try:
                reply_ids = self.plugin._adapter_attr(adapter, "_direct_reply_ids")
            except Exception:
                reply_ids = None
            if isinstance(reply_ids, dict):
                msg_id = str(reply_ids.get(target) or "")
        if not msg_id:
            return None                      # 没有被动消息 id ⇒ 流式发不出去
        if client is None:
            return None

        sess = self._sessions.get(target)
        if sess is not None and (sess.done or sess.msg_id != msg_id):
            self._sessions.pop(target, None)
            sess = None
        if sess is None:
            sess = _Session(target, msg_id, self._next_seq(), content_type, adapter)
            self._sessions[target] = sess
        sess.text = text
        sess.content_type = content_type

        try:
            if sess.frames > 0 and time.monotonic() - sess.last_frame_at < MIN_FRAME_INTERVAL:
                await asyncio.sleep(MIN_FRAME_INTERVAL -
                                    (time.monotonic() - sess.last_frame_at))
            if sess.frames >= MAX_FRAMES:
                raise RuntimeError(f"超过单片上限（{MAX_FRAMES}）")
            resp = await self._send_frame(client, sess, STATE_GENERATING)
        except Exception as exc:
            self._note_failure(target, exc)
            return None                      # ★ 放手：调用方照常普通发送
        sess.frames += 1
        self._arm_idle(client, target, sess)
        if sess.frames == 1 and "first" not in self._logged:
            self._logged.add("first")
            self._log_info(
                "[QQBOT-BRIDGE] 私聊流式消息已启用：同一轮的多个分段会写成**同一条会生长的消息**"
                "（官方 stream_messages；群里/图文/语音照旧走普通发送）"
            )
        return resp if isinstance(resp, dict) else {"id": getattr(resp, "id", None)}

    # ------------------------------------------------------------------ #
    def note_turn_start(self, target: str) -> None:
        """新一轮开始（ON_LLM_REQUEST）：把上一轮还没收尾的流补上结束帧。"""
        sess = self._sessions.get(target)
        if sess is None or sess.done:
            return
        try:
            asyncio.get_running_loop().create_task(self._finish(sess, "turn_boundary"))
        except Exception:
            pass

    async def close_all(self) -> None:
        """插件停止 / 卸载时收尾（best-effort，绝不抛）。"""
        for target in list(self._sessions):
            sess = self._sessions.get(target)
            if sess is not None and not sess.done:
                try:
                    await asyncio.wait_for(self._finish(sess, "shutdown"), timeout=5.0)
                except Exception:
                    pass
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._sessions.clear()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _next_seq(self) -> int:
        self._seq_n = (self._seq_n + 1) % 20000
        return self._seq_base + self._seq_n

    def _cooled_down(self, target: str) -> bool:
        item = self._failures.get(target)
        if not item:
            return True
        count, ts = item
        if count < FAILURE_COOLDOWN:
            return True
        if time.monotonic() - ts > COOLDOWN_SECONDS:
            self._failures.pop(target, None)
            return True
        return False

    def _note_failure(self, target: str, exc: Exception) -> None:
        count, _ = self._failures.get(target, (0, 0.0))
        self._failures[target] = (count + 1, time.monotonic())
        text = str(exc)
        key = "fail"
        if key not in self._logged:
            self._logged.add(key)
            self._log_warn(
                "[QQBOT-BRIDGE] 私聊流式消息发送失败（%s: %s）—— 本条已回退成普通发送，"
                "消息不会丢；连续失败 %d 次后会对该会话冷却 %.0f 分钟",
                type(exc).__name__, text[:160], FAILURE_COOLDOWN, COOLDOWN_SECONDS / 60,
            )
        low = text.lower()
        if any(k in low for k in ("permission", "权限", "not allowed", "11253", "invalid")):
            self._disabled_reason = text[:120]
            self._log_warn(
                "[QQBOT-BRIDGE] 该机器人似乎没有流式消息权限，已**全局停用**私聊流式"
                "（其余功能不受影响）：%s", text[:120],
            )

    async def _send_frame(self, client: Any, sess: _Session, state: int) -> Any:
        from botpy.http import Route

        api = getattr(client, "api", None)
        http = getattr(api, "_http", None)
        if http is None:
            raise RuntimeError("no http client")
        route = Route("POST", "/v2/users/{openid}/stream_messages", openid=sess.target)
        last: Exception | None = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            idx = sess.index
            body = {
                "input_mode": "replace",
                "input_state": state,
                "index": idx,
                "content_type": sess.content_type,
                "content_raw": sess.text,
                "msg_id": sess.msg_id,
                "msg_seq": sess.seq,
            }
            if sess.stream_msg_id:
                body["stream_msg_id"] = sess.stream_msg_id
            try:
                resp = await http.request(route, json=body)
            except Exception as exc:
                last = exc
                if not self._is_rate_limit(exc) or attempt >= MAX_RATE_LIMIT_RETRIES:
                    raise
                delay = 1.0 * (2 ** attempt)
                self._log_warn(
                    "[QQBOT-BRIDGE] 流式消息被限流，%.1fs 后重试（%d/%d）",
                    delay, attempt + 1, MAX_RATE_LIMIT_RETRIES,
                )
                await asyncio.sleep(delay)
                sess.index = idx + 1          # 官方做法：重试要推进 index
                continue
            rid = resp.get("id") if isinstance(resp, dict) else getattr(resp, "id", None)
            if rid and not sess.stream_msg_id:
                sess.stream_msg_id = str(rid)
            sess.index = idx + 1
            sess.last_frame_at = time.monotonic()
            return resp
        raise last if last is not None else RuntimeError("stream frame failed")

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        low = str(exc).lower()
        return "429" in low or "50002" in low or "rate limit" in low or "too many" in low

    async def _finish(self, sess: _Session, reason: str) -> None:
        if sess.done:
            return
        sess.done = True
        self._sessions.pop(sess.target, None)
        task = self._tasks.pop(sess.target, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        # ⚠ 必须用 **session 自己记的 adapter** 取 client ——
        #   上面已经把 session 从表里摘掉了，靠 target 反查会拿到 None（DONE 帧就永远发不出去）。
        try:
            client = sess.adapter.get_client() if sess.adapter is not None else None
        except Exception:
            client = None
        if client is None:
            return
        try:
            if sess.text:
                await self._send_frame(client, sess, STATE_DONE)
        except Exception as exc:
            self._log_once("done", "[QQBOT-BRIDGE] 流式消息收尾帧发送失败（忽略）：%s",
                           str(exc)[:140])

    def _arm_idle(self, client: Any, target: str, sess: _Session) -> None:
        task = self._tasks.get(target)
        if task is not None and not task.done():
            task.cancel()
        try:
            self._tasks[target] = asyncio.get_running_loop().create_task(
                self._idle_done(target, sess))
        except Exception:
            self._tasks.pop(target, None)

    async def _idle_done(self, target: str, sess: _Session) -> None:
        try:
            await asyncio.sleep(IDLE_DONE_SECONDS)
            await self._finish(sess, "idle")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_once("idle", "[QQBOT-BRIDGE] 流式消息空闲收尾失败（忽略）：%s",
                           str(exc)[:140])

    def _log_info(self, msg: str, *a: Any) -> None:
        try:
            self.logger.info(msg, *a)
        except Exception:
            pass

    def _log_warn(self, msg: str, *a: Any) -> None:
        try:
            self.logger.warning(msg, *a)
        except Exception:
            pass

    def _log_once(self, key: str, msg: str, *a: Any) -> None:
        if key in self._logged:
            return
        self._logged.add(key)
        self._log_warn(msg, *a)
