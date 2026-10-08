"""C2C 流式消息（`stream_messages`）—— 让私聊回复**一边生成一边长出来**。

## 这是官方能力，KiraAI 核心没有

官方接口（`POST /v2/users/{openid}/stream_messages`）：

    input_mode   replace：ContentRaw 是**当前全量正文**（平台要求："须以上游已下发前缀开头"）
    input_state  1=生成中  10=生成结束
    index        分片序号，从 0 递增
    content_type text | markdown
    content_raw  当前全量文本
    msg_id       被动回复消息 ID（与 event_id 二选一）
    stream_msg_id 首片由服务端返回，**后续分片必须携带**
    msg_seq      同一条流的所有分片**共用一个**（只有 index 递增）

腾讯官方 Node SDK（`src/streaming.ts`：节流默认 500ms、最低 300ms；限流 50002/HTTP 429
时指数退避并**推进 index**）与 QQ 官方推荐的 Hermes 都是这套。

## 两条输入（"提速"的来源）

1. **token 预览**（快）：提速器插件在 LLM 客户端里用 `chat_stream()` 收 SSE
   （见 `/var/minis/shared/acc_bg_20260929/stream_engine.py`）。我们在**同一个
   `chat_stream` 上旁听**（只观察、不额外发起任何调用），把已成型文字**实时**投到
   QQ 的流式消息上 ⇒ 用户 ~0.5s 就能看到字，而不必等整段 `<msg>` 生成完。
   没有提速器时这条自然静默（核心回复路径不调 `chat_stream`），零副作用。
2. **权威分段**（准）：框架真正要发的那几段（提速器抢发的段 + 框架补发的剩余段）。
   它们经框架发送层到达，我们把它们**接进同一条流**，最终显示的文字与框架语义一致。

预览负责"快"，权威段负责"准"；同一条消息用 replace 反复改写，预览被权威内容覆盖后自动一致。

## ★ 两条硬规则（2026-10-09 修正 —— 之前的两处真问题）

* **累积，不能覆盖**：权威段是"一条条独立消息"，但写进流里必须是**累加**的全文
  （replace 契约要求新正文以已下发内容为前缀）⇒ 之前写成 `sess.text = 本段`，
  多段回复会把前一段顶掉、甚至构成非法请求。现在拆成 `base`（权威累积）+
  `preview`（当前段预览），发出去的永远是 `base + preview`。
* **绝不 sleep 拖慢发送**：节流只用"跳过本次更新"，**不用 `await sleep`** ——
  否则提速器抢发得越快我们反而越慢（本末倒置）。

## 兼容与安全

1. **只碰"纯文本私聊"**：`msg_type=0`、无 markdown / 键盘 / 媒体 / 引用；其余一律原路返回。
2. **失败立刻放手**：任何一步出错 ⇒ 本次回退普通发送（消息绝不丢）；连续失败 2 次对该
   会话冷却；平台明确无权限 ⇒ 全局停用。
3. **返回值与普通发送同形**（`{"id", "ext_info"}`）⇒ 框架上层（展示态 id、引用索引、
   `_sent_message_ids`）完全照旧，核心一行都不用改。
4. 片数上限 30、最小间隔 500ms、空闲 2.5s 自动补 `input_state=10`。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Optional

#: 低于这个长度不流式（"嗯""好"这种一两个字，流式反而更慢更怪）
MIN_CHARS = 8
#: 空闲多久自动补 `input_state=10`（收尾帧）
IDLE_DONE_SECONDS = 2.5
#: 一条流最多发多少片（保险丝；超了收尾并回退普通发送）
MAX_FRAMES = 30
#: 两片之间的最小间隔（官方建议 500ms，最低 300ms）。**只用于"跳过更新"，不 sleep。**
MIN_FRAME_INTERVAL = 0.5
#: 命中限流（HTTP 429 / err_code 50002）时的重试次数（官方 MAX_FLUSH_RETRIES=3）
MAX_RATE_LIMIT_RETRIES = 3
#: 连续失败几次后对该目标冷却，不再尝试流式
FAILURE_COOLDOWN = 2
COOLDOWN_SECONDS = 600.0

STATE_GENERATING = 1
STATE_DONE = 10

#: 一个 `<msg ...>` 开标签
_MSG_OPEN = re.compile(r"<msg(?:\s[^>]*)?>")
#: 正文标签（`<text>` / `<markdown>`），可能带属性
_TEXT_OPEN = re.compile(r"<(text|markdown)(?:\s[^>]*)?>")
#: 工具调用痕迹 —— 一旦出现，这一轮**不展示预览**（那不是给用户看的文字）
_TOOL_HINT = re.compile(r"<tool|tool_call|invoke")


def display_text_of(raw: str) -> str:
    """从**流式累积的原始 XML**里抽出"此刻可以安全展示的那部分文字"。

    ## 三条保守规则（宁可少显示，也绝不把半个标签或工具调用甩给用户）

    1. 只取**最后一个 `<msg>` 之后**的内容（前面几段的文字由"权威分段"负责显示，
       这里重复取会变成重复文字）；
    2. 只取最后一个 `<text>` / `<markdown>` 开标签之后的文字；没有就返回空；
    3. 遇到**未闭合的 `<`** 直接截断到它之前（半截标签绝不外传），并丢掉工具调用。

    纯函数，单测逐条钉死。
    """
    if not raw:
        return ""
    last_msg = None
    for m in _MSG_OPEN.finditer(raw):
        last_msg = m
    if last_msg is None:
        return ""
    seg = raw[last_msg.end():]
    if not seg or _TOOL_HINT.search(seg):
        return ""
    last_text = None
    for m in _TEXT_OPEN.finditer(seg):
        last_text = m
    if last_text is None:
        return ""
    body = seg[last_text.end():]
    cut = body.find("<")            # 从这里开始是闭合标签或半截标签 ⇒ 丢掉
    if cut >= 0:
        body = body[:cut]
    return body.replace("\r", "").strip()


class _Session:
    """一条流式消息的状态。"""

    __slots__ = ("target", "msg_id", "seq", "index", "stream_msg_id", "base",
                 "preview", "last_sent_text", "last_frame_at", "done",
                 "content_type", "frames", "adapter", "flusher",
                 "authoritative_seen")

    def __init__(self, target: str, msg_id: str, seq: int, content_type: str,
                 adapter: Any = None):
        self.adapter = adapter
        self.target = target
        self.msg_id = msg_id
        self.seq = seq
        self.index = 0
        self.stream_msg_id: Optional[str] = None
        #: 权威分段的累积全文（框架真正发出去的那几段）
        self.base = ""
        #: 当前段的预览（token 观察者给的，尚未被权威内容覆盖）
        self.preview = ""
        self.last_sent_text = ""
        self.last_frame_at = 0.0
        self.done = False
        self.content_type = content_type
        self.frames = 0
        self.flusher: Optional[asyncio.Task] = None
        self.authoritative_seen = False

    @property
    def text(self) -> str:
        """要写进流式消息的**全量正文**（必须以已下发内容为前缀）。"""
        return (self.base + self.preview).strip("\n")


class _NoSession:
    preview = ""


_NO_SESSION = _NoSession()


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
        self._last_ext_info: Optional[dict] = None
        #: 平台层面明确不可用（权限/未开通）⇒ 全局停用，不再浪费请求
        self._disabled_reason: str = ""

    # ------------------------------------------------------------------ #
    # 对外 A：权威分段（框架真正要发的文本）
    # ------------------------------------------------------------------ #
    def eligible(self, target: str, kwargs: dict) -> Optional[str]:
        """这次发送能不能走流式？返回内容类型（text/markdown）或 None。

        **判据刻意保守**（宁可不流式，也不要改变既有行为）。
        """
        if not self.enabled or self._disabled_reason:
            return None
        if int(kwargs.get("msg_type") or 0) != 0:
            return None
        if kwargs.get("markdown") or kwargs.get("keyboard") or kwargs.get("media"):
            return None
        if kwargs.get("message_reference"):
            return None
        text = kwargs.get("content")
        if not isinstance(text, str) or len(text.strip()) < MIN_CHARS:
            return None
        if not self._cooled_down(target):
            return None
        return "text"

    async def maybe_stream(self, adapter: Any, client: Any, target: str,
                           kwargs: dict) -> Optional[dict]:
        """把这次发送接进流式消息。**返回 None 表示"没接管"**（调用方照常发送）。"""
        content_type = self.eligible(target, kwargs)
        if content_type is None:
            return None
        text = str(kwargs.get("content") or "")
        msg_id = str(kwargs.get("msg_id") or "")
        if not msg_id:                       # 主动发送没有 msg_id ⇒ 流式发不出去
            msg_id = self._reply_id_of(adapter, target)
        if not msg_id or client is None:
            return None

        sess = self._ensure(adapter, target, msg_id, content_type)
        if sess is None:
            return None
        # ★★ 累积而不是覆盖（replace 契约：新正文必须以已下发内容为前缀）。
        #    权威段之间是"先后两段" ⇒ 累加；若这次给的本身就是全量
        #    （故障重发 / 框架补发整段）⇒ 以它为准。
        if not sess.authoritative_seen or text.startswith(sess.base):
            sess.base = text
        else:
            sess.base = (sess.base + text) if sess.base else text
        sess.authoritative_seen = True
        sess.preview = ""                    # 这一段已有权威版本，预览作废

        try:
            resp = await self._flush(sess, client, force=True)
        except Exception as exc:
            self._note_failure(target, exc)
            return None                      # ★ 放手：调用方照常普通发送
        if resp is None and self._frame_cap_hit(sess):
            self._log_once("cap", "[QQBOT-BRIDGE] 流式消息片数达上限（%d），本条回退普通发送",
                           MAX_FRAMES)
            self._retire(sess)
            return None
        self._arm_idle(client, target, sess)
        if "first" not in self._logged:
            self._logged.add("first")
            self._log_info(
                "[QQBOT-BRIDGE] 私聊流式消息已启用：这一轮的回复会写成**同一条会生长的消息**"
                "（官方 stream_messages；群里/图文/语音照旧走普通发送）"
            )
        return {"id": sess.stream_msg_id, "ext_info": self._last_ext_info}

    # ------------------------------------------------------------------ #
    # 对外 B：token 预览（旁听提速器的 chat_stream，**同步、非阻塞**）
    # ------------------------------------------------------------------ #
    def observe_raw(self, adapter: Any, client: Any, target: str, msg_id: str,
                    raw: str) -> None:
        """token 观察者调用：更新预览帧（发送交给后台节流任务）。

        **必须同步非阻塞**：这个函数是在提速器的 `async for chunk in chat_stream(...)`
        循环里被调用的 —— 在这里 await 任何网络请求都会**拖慢模型出字**，
        与"提速"的目的正好相反。
        """
        if not self.enabled or self._disabled_reason or client is None:
            return
        if not self._cooled_down(target):
            return
        try:
            text = display_text_of(raw)
        except Exception:
            return
        if not text or text == self._sessions.get(target, _NO_SESSION).preview:
            return
        sess = self._ensure(adapter, target, msg_id, "text")
        if sess is None or sess.done:
            return
        sess.preview = text
        self._ensure_flusher(client, sess)

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
    def _reply_id_of(self, adapter: Any, target: str) -> str:
        try:
            reply_ids = self.plugin._adapter_attr(adapter, "_direct_reply_ids")
        except Exception:
            reply_ids = None
        if isinstance(reply_ids, dict):
            return str(reply_ids.get(target) or "")
        return ""

    def _ensure(self, adapter: Any, target: str, msg_id: str,
                content_type: str) -> Optional[_Session]:
        sess = self._sessions.get(target)
        if sess is not None and (sess.done or sess.msg_id != msg_id):
            self._retire(sess)
            sess = None
        if sess is None:
            if len(self._sessions) > 64:          # 保险丝：不会无限涨
                for old in list(self._sessions)[:32]:
                    self._retire(self._sessions[old])
            sess = _Session(target, msg_id, self._next_seq(), content_type, adapter)
            self._sessions[target] = sess
        return sess

    def _retire(self, sess: _Session) -> None:
        self._sessions.pop(sess.target, None)
        task = self._tasks.pop(sess.target, None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if sess.flusher is not None and not sess.flusher.done():
            sess.flusher.cancel()

    def _frame_cap_hit(self, sess: _Session) -> bool:
        return sess.frames >= MAX_FRAMES

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
        if "fail" not in self._logged:
            self._logged.add("fail")
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

    async def _flush(self, sess: _Session, client: Any, force: bool = False) -> Optional[dict]:
        """把**当前全量正文**写进流式消息（无变化 / 间隔未到则跳过，**绝不 sleep**）。"""
        text = sess.text
        if not text:
            return None
        if text == sess.last_sent_text:
            return None                       # 内容没变 ⇒ 不必重复发
        if not force and sess.frames and \
                time.monotonic() - sess.last_frame_at < MIN_FRAME_INTERVAL:
            return None                       # ★ 直接跳过（sleep 会拖慢发送）
        if self._frame_cap_hit(sess):
            return None
        await self._send_frame(client, sess, text, STATE_GENERATING)
        sess.frames += 1
        sess.last_sent_text = text
        return {"id": sess.stream_msg_id, "ext_info": self._last_ext_info}

    def _ensure_flusher(self, client: Any, sess: _Session) -> None:
        if sess.flusher is not None and not sess.flusher.done():
            return
        try:
            sess.flusher = asyncio.get_running_loop().create_task(
                self._preview_loop(client, sess))
        except Exception:
            sess.flusher = None

    async def _preview_loop(self, client: Any, sess: _Session) -> None:
        """后台把预览按 500ms 节流写进流式消息（不占用提速器的循环）。"""
        try:
            while not sess.done:
                await asyncio.sleep(MIN_FRAME_INTERVAL)
                if sess.text == sess.last_sent_text and not sess.preview:
                    return
                try:
                    await self._flush(sess, client)
                except Exception as exc:
                    self._note_failure(sess.target, exc)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _send_frame(self, client: Any, sess: _Session, text: str, state: int) -> Any:
        from botpy.http import Route

        api = getattr(client, "api", None)
        http = getattr(api, "_http", None)
        if http is None:
            raise RuntimeError("no http client")
        route = Route("POST", "/v2/users/{openid}/stream_messages", openid=sess.target)
        last: Optional[Exception] = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            idx = sess.index
            body = {
                "input_mode": "replace",
                "input_state": state,
                "index": idx,
                "content_type": sess.content_type,
                "content_raw": text,
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
                await asyncio.sleep(delay)     # 限流退避（官方做法），与"节流跳过"不同
                sess.index = idx + 1           # 官方做法：重试要推进 index
                continue
            rid = resp.get("id") if isinstance(resp, dict) else getattr(resp, "id", None)
            if rid and not sess.stream_msg_id:
                sess.stream_msg_id = str(rid)
            info = resp.get("ext_info") if isinstance(resp, dict) else None
            if isinstance(info, dict):
                self._last_ext_info = info
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
        try:
            client = sess.adapter.get_client() if sess.adapter is not None else None
        except Exception:
            client = None
        flusher = sess.flusher
        self._retire(sess)
        if flusher is not None and not flusher.done() and flusher is not asyncio.current_task():
            flusher.cancel()
        if client is None or not sess.text:
            return
        try:
            await self._send_frame(client, sess, sess.text, STATE_DONE)
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
