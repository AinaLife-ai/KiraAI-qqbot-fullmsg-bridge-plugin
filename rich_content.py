"""markdown / 键盘 的载体元素与标签（插件自建，不依赖核心是否有这两种元素）。

为什么要插件自建元素
------------------
KiraAI 2.x 与 3.0 的 `ElementType` 里**都没有** markdown / keyboard。
但发送链路只需要一个"标记"，真正的渲染由发送侧补丁（`api_send.py`）完成。

这样做的两个好处：
1. 不碰核心（不往 `ElementType` 里塞东西），两版都能跑；
2. 即使核心未来加了同名元素，也不冲突（我们只在自己的 `_text_content` 补丁里识别）。

标签注册
--------
`<markdown>正文</markdown>` / `<keyboard>{json}</keyboard>`
都是 `<msg>` 的子标签，靠 `TagSet.register()` 进提示词。
核心机制：`message_manager` 在 ON_LLM_REQUEST 阶段建 `TagSet`，
插件往里注册，最后 `tag_set.to_prompt()` 拼进 `format` 提示词 —— **两版完全一致**。
"""

from __future__ import annotations

import json
from typing import Optional

try:
    from core.chat.message_elements import BaseMessageElement, ElementType, Text
except Exception:  # pragma: no cover - 极端情况下退化成 object，功能降级但不崩
    BaseMessageElement = object  # type: ignore
    ElementType = None  # type: ignore
    Text = None  # type: ignore


#: 键盘上限（官方：内联键盘行列超限报 40034029）
MAX_KEYBOARD_ROWS = 5
MAX_BUTTONS_PER_ROW = 5
MAX_BUTTON_DATA = 100
#: markdown 正文建议上限（官方：单条建议 ≤ 4000 字符）
MAX_MARKDOWN_CHARS = 4000


class MarkdownText(BaseMessageElement):  # type: ignore[misc]
    """一条"要按 markdown 发送"的正文。

    它只承载文本；**是否真的走 msg_type=2 由发送侧决定**（见 api_send.py），
    这样"平台没有 markdown 权限"时可以在最后一刻退回纯文本。
    """

    if ElementType is not None:
        type = ElementType.Text      # 复用 Text 类型，避免核心不认识
    else:  # pragma: no cover
        type = None

    def __init__(self, text: str):
        self.text = text

    @property
    def repr(self) -> str:
        return f"[Markdown] {self.text}"


class KeyboardMarker(BaseMessageElement):  # type: ignore[misc]
    """一条内联键盘（keyboard）载荷，与同一条 `<msg>` 里的文本并列。"""

    if ElementType is not None:
        type = ElementType.Text
    else:  # pragma: no cover
        type = None

    def __init__(self, keyboard: dict):
        self.keyboard = keyboard

    @property
    def repr(self) -> str:
        return "[Keyboard]"


# --------------------------------------------------------------------------- #
# 键盘校验
# --------------------------------------------------------------------------- #
class KeyboardError(ValueError):
    pass


def validate_keyboard(raw: str) -> dict:
    """把模型给的 JSON 校验成合法的 `keyboard` 载荷。

    官方两种形态：
    * **短形式**：`{"id": "keyboard_id_xxx"}` —— 引用已配置好的模板按钮；
    * **长形式**：`{"content": {"rows": [{"buttons": [...]}]}}`。

    这里主要拦三类错误（官方错误码 `305007 键盘样式参数错误`、`40034029 内联键盘行/列超限`）：
    1. 不是合法 JSON / 不是对象；
    2. 行列超限（行 ≤ 5，每行按钮 ≤ 5）；
    3. 每个按钮缺 `id`，或 `action.data` 过长（超过 100 字符）。
    """
    text = (raw or "").strip()
    if not text:
        raise KeyboardError("keyboard 内容为空")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise KeyboardError(f"keyboard 不是合法 JSON：{exc.msg}") from exc
    if not isinstance(payload, dict):
        raise KeyboardError("keyboard 必须是一个 JSON 对象")

    # 短形式：引用已注册的键盘 id
    if isinstance(payload.get("id"), str) and payload["id"].strip():
        if "content" not in payload:
            return {"id": payload["id"].strip()}

    content = payload.get("content")
    if not isinstance(content, dict):
        raise KeyboardError("keyboard 需要 `content`（长形式）或 `id`（短形式）")
    rows = content.get("rows")
    if not isinstance(rows, list) or not rows:
        raise KeyboardError("keyboard.content.rows 必须是非空数组")
    if len(rows) > MAX_KEYBOARD_ROWS:
        raise KeyboardError(f"键盘最多 {MAX_KEYBOARD_ROWS} 行，收到 {len(rows)} 行")

    for r_i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise KeyboardError(f"第 {r_i + 1} 行不是对象")
        buttons = row.get("buttons")
        if not isinstance(buttons, list) or not buttons:
            raise KeyboardError(f"第 {r_i + 1} 行没有 buttons")
        if len(buttons) > MAX_BUTTONS_PER_ROW:
            raise KeyboardError(
                f"第 {r_i + 1} 行最多 {MAX_BUTTONS_PER_ROW} 个按钮，收到 {len(buttons)} 个"
            )
        for b_i, btn in enumerate(buttons):
            if not isinstance(btn, dict):
                raise KeyboardError(f"第 {r_i + 1} 行第 {b_i + 1} 个按钮不是对象")
            bid = btn.get("id")
            if not isinstance(bid, str) or not bid.strip():
                raise KeyboardError(f"第 {r_i + 1} 行第 {b_i + 1} 个按钮缺少 id")
            action = btn.get("action")
            if isinstance(action, dict):
                data = action.get("data")
                if isinstance(data, str) and len(data) > MAX_BUTTON_DATA:
                    raise KeyboardError(
                        f"按钮 {bid} 的 action.data 超过 {MAX_BUTTON_DATA} 字符"
                    )
    return {"content": {"rows": rows}}


# --------------------------------------------------------------------------- #
# 从消息链里提取 markdown / keyboard
# --------------------------------------------------------------------------- #
def split_markdown_and_keyboard(chain) -> tuple[Optional[str], Optional[dict], bool]:
    """扫描一条消息链，取出 markdown 正文与键盘载荷。

    返回 ``(markdown_text, keyboard, changed)``。

    * `markdown_text`：把 `MarkdownText` 与普通 `Text` 按原顺序拼起来
      （这样"<text>前面</text><markdown>## 标题</markdown>"也成立）；
    * `keyboard`：取第一个 `KeyboardMarker`（一条消息只支持一个键盘）；
    * `changed`：是否真的提取到了东西 —— 只有 True 时才需要走 markdown 分支。
    """
    md_parts: list = []
    keyboard = None
    has_md = False
    for ele in chain or []:
        if isinstance(ele, MarkdownText):
            has_md = True
            md_parts.append(ele.text)
        elif isinstance(ele, KeyboardMarker):
            if keyboard is None:
                keyboard = ele.keyboard
        else:
            text = getattr(ele, "text", None)
            if isinstance(text, str) and text:
                md_parts.append(text)
    md_text = "".join(md_parts).strip() if has_md else None
    if md_text and len(md_text) > MAX_MARKDOWN_CHARS:
        md_text = md_text[:MAX_MARKDOWN_CHARS]
    return md_text, keyboard, bool(has_md or keyboard)


# --------------------------------------------------------------------------- #
# 提示词里给模型看的说明（会原样进 format 提示词的 message_types 段）
# --------------------------------------------------------------------------- #
MARKDOWN_TAG_DESCRIPTION = (
    "<markdown>markdown 正文</markdown> "
    "# 用 markdown 富文本发送本条消息（支持标题/加粗/斜体/删除线/链接/图片/有序无序列表/块引用/分割线）。"
    "适合需要排版的长内容（列表、步骤、代码块、对比）。"
    "不要在正文里手写 <qqbot-at-user> 之类的平台标记，系统会自动处理 @。"
)

KEYBOARD_TAG_DESCRIPTION = (
    "<keyboard>JSON</keyboard> "
    "# 在消息下方挂内联按钮。JSON 形如 "
    '{"content":{"rows":[{"buttons":[{"id":"b1","render_data":{"label":"点我","style":1},'
    '"action":{"type":2,"data":"/签到","permission":{"type":2}}}]}]}}。'
    "最多 5 行、每行最多 5 个按钮，按钮的 action.data 不超过 100 字符。"
    "必须和 <text> 或 <markdown> 放在同一个 <msg> 里。用户点击后会以消息形式回来。"
)
