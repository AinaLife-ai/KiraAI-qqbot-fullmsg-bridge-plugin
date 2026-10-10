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
import re

import json
from typing import Any, Optional

try:
    from core.chat.message_elements import BaseMessageElement, ElementType, Text
except Exception:  # pragma: no cover - 极端情况下退化成 object，功能降级但不崩
    BaseMessageElement = object  # type: ignore
    ElementType = None  # type: ignore
    Text = None  # type: ignore


#: ★ 2026-10-10：给"指令按钮"（action.type=2）默认补 `action.enter = true`。
#:
#: 官方字段语义（《消息按钮》文档 + 官方 SDK 说明）：
#:   * `action.enter`（bool，**仅单聊 + 手机QQ 8983+** 支持）：点击按钮后
#:     **直接自动发送 data**，不用用户再按一次发送；
#:   * 默认 false ⇒ 点击只把 `@bot data` 插进输入框，等用户自己按发送
#:     （用户实测反馈："点了没反应，像是坏的"）。
#:
#: ⇒ 默认打开（可用显式 `"enter": false` 覆盖）。群里或低版本客户端点了也只是
#:   "插进输入框"，与官方默认行为一致，**不会更糟**。
_AUTO_ENTER = True


def set_auto_enter(enabled: bool) -> None:
    """插件配置注入（`keyboard_auto_enter`）；幂等，热改立即生效。"""
    global _AUTO_ENTER
    _AUTO_ENTER = bool(enabled)


#: ★ 2026-10-10：回调按钮（`action.type=1`）**可选降级**成指令按钮（`type=2` + enter）。
#:
#: 为什么需要：回调按钮要求**平台能把 INTERACTION_CREATE 推给机器人**。
#: 若平台侧那条路不通（典型表现：客户端点按钮提示「请求第三方失败」），
#: 按钮就点不动。这个开关把它换成"点一下就自动发送 data"的指令按钮 ——
#: 立刻可用（单聊点一下即发；群里仍只是插进输入框），且随时可关。
#: 默认 **关**：尊重模型/用户的原始意图，只在明确需要时才降级。
_CB_TO_COMMAND = False


def set_callback_to_command(enabled: bool) -> None:
    """插件配置注入（`keyboard_callback_to_command`）；幂等，热改立即生效。"""
    global _CB_TO_COMMAND
    _CB_TO_COMMAND = bool(enabled)


def callback_to_command_enabled() -> bool:
    return _CB_TO_COMMAND


#: 键盘上限（官方：内联键盘行列超限报 40034029）
MAX_KEYBOARD_ROWS = 5
MAX_BUTTONS_PER_ROW = 5
MAX_BUTTON_DATA = 100
#: 官方《消息按钮》里 render_data.style 的取值（其它值平台会报 305007 样式参数错误，
#: 或按默认样式渲染 —— 型号/客户端表现不一）：0 灰线框 / 1 蓝线框 / 3 白底红字 / 4 蓝底白字。
#: 官方《发送群聊消息》schema（2026-10 抓取）原文：
#: `0 灰色线框，1 蓝色线框，3 白色背景+红色字体，4 蓝色背景+白色字体`。
#: ⇒ 四种都合法，**我们绝不擅自改模型的样式值**（只统计异常值供排查）。
OFFICIAL_BUTTON_STYLES = (0, 1)
#: 频道文档里的样式值 ⇒ 归一到群聊可用的 {0,1}
STYLE_ALIASES = {3: 1, 4: 1, 2: 1, 5: 0, 6: 0}
#: 客户端不支持该 action 时的默认 toast（官方标"必填"，我们兜个默认值）
DEFAULT_UNSUPPORT_TIPS = "当前版本暂不支持该按钮，请升级手机QQ"

#: markdown 正文建议上限（官方：单条建议 ≤ 4000 字符）
MAX_MARKDOWN_CHARS = 4000


#: 键盘元素在"纯文本渲染"里的占位：**零宽空格**。
#:
#: ★ 为什么必须有它（2026-10-10 踩到）：核心发送前会检查
#:   `if not content and not media_elements: 报「不能发空消息」` ——
#:   如果键盘元素渲染成空串，**只有键盘的消息会被核心直接拒发**。
#:   零宽空格不可见（Python 不把它当空白，strip() 去不掉），
#:   既能过核心的空检查，用户也看不到任何字符。
KB_TEXT_PLACEHOLDER = "\u200b"

#: ★★★ 我们的自定义元素都**继承核心的 `Text`**（2026-10-10 修的真 bug）：
#:
#:   核心 `_text_content()` 只按 `isinstance(element, Text)` 分支渲染，
#:   **不认识**我们的元素 ⇒ 落进 `else` 拼出字面量
#:   `[Unsupported message element]` 发到 QQ ——
#:   用户截图实证：那条键盘消息在聊天里就是
#:   「想让香香干嘛……[Unsupported message element]」，按钮也没有。
#:   继承 `Text` 后：markdown 正文原样渲染、键盘只是一个不可见占位
#:   ⇒ 即便发送侧补丁没装上，最坏也只是"没有按钮"，**绝不会再出现脏文本**。
_TEXT_BASE = Text if Text is not None else BaseMessageElement  # type: ignore[misc]


def _init_text(element: Any, text: str) -> None:
    """按核心 `Text` 的形状初始化（拿不到核心时退化成纯属性赋值）。"""
    try:
        if Text is not None:
            Text.__init__(element, text)      # type: ignore[misc]
    except Exception:
        pass
    element.text = text


class MarkdownText(_TEXT_BASE):  # type: ignore[misc]
    """一条"要按 markdown 发送"的正文。

    它只承载文本；**是否真的走 msg_type=2 由发送侧决定**（见 api_send.py），
    这样"平台没有 markdown 权限"时可以在最后一刻退回纯文本
    （退回纯文本时它仍是一段正常文本，不会变成占位符）。
    """

    if ElementType is not None:
        type = ElementType.Text      # 复用 Text 类型，避免核心不认识
    else:  # pragma: no cover
        type = None

    def __init__(self, text: str):
        _init_text(self, text)

    @property
    def repr(self) -> str:
        return f"[Markdown] {self.text}"


class KeyboardMarker(_TEXT_BASE):  # type: ignore[misc]
    """一条内联键盘（keyboard）载荷，与同一条 `<msg>` 里的文本并列。"""

    if ElementType is not None:
        type = ElementType.Text
    else:  # pragma: no cover
        type = None

    def __init__(self, keyboard: dict):
        _init_text(self, KB_TEXT_PLACEHOLDER)
        self.keyboard = keyboard

    @property
    def repr(self) -> str:
        return "[Keyboard]"


def strip_kb_placeholder(text: Any) -> str:
    """去掉键盘元素留下的零宽占位（发送侧把正文转成 markdown 时用）。"""
    return str(text or "").replace(KB_TEXT_PLACEHOLDER, "").strip()


# --------------------------------------------------------------------------- #
# 键盘校验
# --------------------------------------------------------------------------- #
class KeyboardError(ValueError):
    pass


def validate_keyboard(raw: str, stats: Optional[dict] = None) -> dict:
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

    _seen_ids: set = set()
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
            # ★ 官方字段表里 `id` 是**非必填**（"按钮ID：在一个keyboard消息内设置唯一"）。
            #   旧实现缺 id 直接报错 ⇒ 模型偶尔不写就被拒；现在自动补一个唯一 id。
            bid = btn.get("id")
            if not isinstance(bid, str) or not bid.strip():
                _auto = f"b{r_i + 1}_{b_i + 1}"
                while _auto in _seen_ids:
                    _auto += "_"
                btn["id"] = _auto
                bid = btn["id"]
            _seen_ids.add(bid)
            action = btn.get("action")
            if isinstance(action, dict):
                data = action.get("data")
                if isinstance(data, str) and len(data) > MAX_BUTTON_DATA:
                    raise KeyboardError(
                        f"按钮 {bid} 的 action.data 超过 {MAX_BUTTON_DATA} 字符"
                    )
    payload_out = {"content": {"rows": rows}}
    _st = apply_button_defaults(payload_out)    # ★ 指令按钮默认 enter:true
    if isinstance(stats, dict):
        stats.update(_st)
    return payload_out


def apply_button_defaults(payload: dict, auto_enter: bool = None) -> dict:
    """就地给"指令按钮"（action.type=2）补 `action.enter = true`；返回统计。

    返回 ``{"enter_added": n, "callback": m}``：
      * ``enter_added``：这次补了几个按钮的 `enter`；
      * ``callback``：本条里有几个**回调按钮**（type=1）——它们需要平台能把
        互动事件推到机器人（长连接订阅了 INTERACTION 就行；若后台把"消息推送方式"
        设成 Webhook 而地址不可达，客户端点按钮会提示「请求第三方失败」）。
    """
    stats = {"enter_added": 0, "callback": 0, "converted": 0, "bad_style": 0}
    try:
        rows = ((payload or {}).get("content") or {}).get("rows") or []
    except Exception:
        return stats
    want = _AUTO_ENTER if auto_enter is None else bool(auto_enter)
    for row in rows:
        if not isinstance(row, dict):
            continue
        for btn in (row.get("buttons") or []):
            if not isinstance(btn, dict):
                continue
            # 样式合法性（只统计、**不修改** —— 避免"我们改坏"的可能）
            action_before = btn.get("action")
            rd = btn.get("render_data")
            if isinstance(rd, dict) and "style" in rd:
                try:
                    if int(rd.get("style")) not in OFFICIAL_BUTTON_STYLES:
                        stats["bad_style"] += 1
                except Exception:
                    stats["bad_style"] += 1
            # 注：`unsupport_tips` 在官方**群聊消息 schema** 里标"否"（非必填），
            #     所以**不擅自补**（保持"模型给什么就是什么"）。
            action = btn.get("action")
            if not isinstance(action, dict):
                continue
            try:
                atype = int(action.get("type"))
            except Exception:
                atype = -1
            if atype == 1:
                stats["callback"] += 1
                if _CB_TO_COMMAND:
                    # 降级：回调按钮 → 指令按钮（点一下就发）
                    action["type"] = 2
                    action.setdefault("enter", True)
                    stats["converted"] += 1
                continue
            if atype == 2 and want and "enter" not in action:
                action["enter"] = True
                stats["enter_added"] += 1
    return stats


# --------------------------------------------------------------------------- #
# 从消息链里提取 markdown / keyboard
# --------------------------------------------------------------------------- #
#: 模型有时会把标签转义着写进正文（`&lt;markdown&gt;…&lt;/markdown&gt;`），
#: 于是整条被当纯文本发出去（用户实测：`#` 和链接全都没渲染）。
_ESCAPED_MD_OPEN = re.compile(r"&lt;\s*markdown\s*&gt;", re.I)
_ESCAPED_MD_CLOSE = re.compile(r"&lt;\s*/\s*markdown\s*&gt;", re.I)
#: 也认没转义的裸标签（模型偶尔会这么写）
_RAW_MD_OPEN = re.compile(r"<\s*markdown\s*>", re.I)
_RAW_MD_CLOSE = re.compile(r"<\s*/\s*markdown\s*>", re.I)


#: markdown 的「触发字符」——没有这些就绝不可能是 markdown（快速排除用）
_MD_HINT = re.compile(r"[#\-*+.!>~`|]")


def unwrap_markdown_tags(text: str) -> str:
    """把正文里**被转义或裸写**的 `<markdown>` 标签剥掉。

    模型经常这样输出（线上实测）：

        <text>&lt;markdown&gt;
        # 标题
        ![图](url)
        &lt;/markdown&gt;</text>

    标签里的尖括号被转义成 `&lt;`，解析器就只当它是普通文字 ⇒
    **整条消息的 markdown 全不渲染**（标题、链接都变成原文）。
    这里把外层这层壳剥掉，正文照常按 markdown 发。

    ⚠ 这是**发送热路径**（每条消息都过），所以先做一次极廉价的子串检查：
    正文里既没有 `&lt;` 也没有 `<` 时直接原样返回，一次正则都不跑。
    """
    if not text:
        return text
    if "&lt;" not in text and "<" not in text:
        return text                      # ← 绝大多数消息走这里，零正则开销
    out = _ESCAPED_MD_OPEN.sub("", text)
    out = _ESCAPED_MD_CLOSE.sub("", out)
    out = _RAW_MD_OPEN.sub("", out)
    out = _RAW_MD_CLOSE.sub("", out)
    return out


def looks_like_markdown(text: str) -> bool:
    """粗判这段正文是不是 markdown（用于「标签被转义」时的兜底识别）。

    只认**明确的** markdown 特征，避免把普通聊天文本误判成 markdown
    （误判会让本该纯文本的消息变成 md，影响 @ 解析等）。

    ⚠ 同样是热路径：先用一个字符类快速排除（普通聊天不含 `# - * > ! . ~`），
    命不中就直接 False，不跑那 6 条正则。
    """
    if not text:
        return False
    if not _MD_HINT.search(text):
        return False                     # ← 普通聊天走这里，零正则开销
    if re.search(r"^#{1,6}\s+\S", text, re.M):
        return True
    if re.search(r"^\s*[-*+]\s+\S", text, re.M) or re.search(r"^\s*\d+\.\s+\S", text, re.M):
        return True
    if re.search(r"!\[[^\]]*\]\([^)]+\)", text):
        return True
    if re.search(r"^\s*>\s+\S", text, re.M):
        return True
    if re.search(r"\*\*[^*\n]+\*\*|~~[^~\n]+~~", text):
        return True
    return False


def split_markdown_and_keyboard(chain) -> tuple[Optional[str], Optional[dict], bool]:
    """扫描一条消息链，取出 markdown 正文与键盘载荷。

    返回 ``(markdown_text, keyboard, changed)``。

    * `markdown_text`：把 `MarkdownText` 与普通 `Text` 按原顺序拼起来
      （这样"<text>前面</text><markdown>## 标题</markdown>"也成立）；
    * `keyboard`：取第一个 `KeyboardMarker`（一条消息只支持一个键盘）；
    * `changed`：是否真的提取到了东西 —— 只有 True 时才需要走 markdown 分支。

    ★ 两个「容错补救」（2026-10-07 用户实测踩到）：

    1. **标签被转义**：正文写着 `&lt;markdown&gt;…&lt;/markdown&gt;`
       ⇒ 剥掉这层壳（`unwrap_markdown_tags`），否则整条 md 不渲染；
    2. **该走 md 却写在 text 里**：剥掉壳之后，如果这段正文明显是 markdown
       （标题/列表/图片/引用/加粗）而模型只用了 `<text>` 标签，
       **就按 markdown 发** —— 否则用户看到的是满屏 `#` 和 `-`。
       （判据卡得比较紧，只认明确特征，避免误伤普通聊天。）
    """
    md_parts: list = []
    keyboard = None
    has_md = False
    text_pool: list = []
    for ele in chain or []:
        if isinstance(ele, MarkdownText):
            has_md = True
            md_parts.append(unwrap_markdown_tags(ele.text))
        elif isinstance(ele, KeyboardMarker):
            if keyboard is None:
                keyboard = ele.keyboard
        else:
            text = getattr(ele, "text", None)
            if isinstance(text, str) and text:
                cleaned = unwrap_markdown_tags(text)
                md_parts.append(cleaned)
                text_pool.append(cleaned)

    md_text = "".join(md_parts).strip() if has_md else None

    # ★ 补救 2：模型只用了 <text>，但内容明显是 markdown ⇒ 也按 md 发
    if md_text is None and text_pool:
        joined = "".join(text_pool).strip()
        if looks_like_markdown(joined):
            md_text = joined
            has_md = True

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
    "★ 必须用 <markdown> 标签包裹正文，不要写在 <text> 里，也不要转义成 &lt;markdown&gt; —— "
    "否则整条消息不会按富文本渲染。"
    "★ 平台**不支持任何 HTML 标签**（<audio> <video> <img> <div> 等一律无效，会显示成文字）；"
    "需要放音频/视频请在 markdown 里给出**可点击的直链**，例如 `[▶ 点这里播放](https://…/a.mp3)`，"
    "或用 <audio> 这类标签包裹音频链接 —— 都不会出声，直链才是唯一可行做法。"
    "★ 图片必须写在 markdown 里 `![描述](图片地址)`，系统会自动把它转成公网地址。"
    "不要在正文里手写 <qqbot-at-user> 之类的平台标记，系统会自动处理 @。"
)

KEYBOARD_TAG_DESCRIPTION = (
    "<keyboard>JSON</keyboard> "
    "# 在消息**最底部**挂一排内联按钮（平台不支持把按钮写进 md 正文里 —— "
    "按钮永远单独占消息底部那一整排）。"
    "★ **推荐写法：正文与按钮放进同一个 <msg>**，正文用 <markdown> 写足排版："
    "<msg><markdown>## 标题 ｜ - 列表项 ｜ **加粗** ｜ > 引用 ｜ "
    "![图 #200px #120px](公网url)</markdown><keyboard>{…}</keyboard></msg> —— "
    "标题、列表、表格、加粗、引用、图片这些 md 格式**全都可以用**，"
    "只要把按钮放在同一条消息的最后（按钮会自动显示在底部），"
    "正文既不会被截断、按钮也照样有。"
    "★ 千万别把正文和按钮分成两个 <msg>：那样会变成「一条只有文字 + 一条只有按钮」，"
    "看起来特别割裂。（正文写成普通 <text> 也可以，系统会自动按 markdown 发，只是排版少些。）"
    "★ 按钮**优先用回调按钮**（action.type=1）：点一下直接回调后台、不碰输入框，"
    "单聊和群聊都支持；这次点击会作为一条消息回到你这里，你接着接话就行。"
    "只有你希望「点击后把指令填进输入框、由用户自己改完再发」时才用指令按钮"
    "（action.type=2；单聊里默认点一下就自动发送）。要打开网页/小程序用跳转按钮"
    "（action.type=0）。"
    "★ JSON 形如 "
    '{"content":{"rows":[{"buttons":[{"id":"b1","render_data":{"label":"点我","style":1,'
    '"visited_label":"已点"},"action":{"type":1,"data":"/签到","permission":{"type":2}}}]}]}}。'
    "最多 5 行、每行最多 5 个按钮，action.data 不超过 100 字符；"
    "按钮文字用 render_data.label（不超过 10 字符），visited_label 可写点击后的文案。"
    "★ 按钮样式（render_data.style）四种，**按语义挑、不要都用同一种**"
    "（官方《发送群聊消息》schema 原文）：0 = 灰色线框（次要/取消）；1 = 蓝色线框（普通）；"
    "3 = 白底红字（危险/删除）；4 = 蓝底白字（主推/推荐）。"
    "★ **谁能按 / 能不能限次数**（官方口径，2026-10 核实原文）："
    "action.permission.type：0 = 指定用户（配 specify_user_ids 点名）、1 = **仅管理员**、"
    "2 = 所有人（默认）——想「只让某几个人能按」就用 0+名单。"
    "action.click_limit（点击次数上限）**官方已标为弃用**、默认不限 ⇒ "
    "**平台层面没有「最多被多少人点」的接口**，要限次数只能自己记（谁点过、点几次）。"
    "★ **想让一排按钮「点一个、其余变灰」**：给这些按钮加同一个 `group_id`"
    "（官方：同一分组内有一个按钮操作后其它按钮变灰不可点击；**仅 action.type=1 回调按钮有效**）。"
    "★ 还可以给回调按钮加二次确认：action.modal = {\"content\": \"确认参加吗\""
    "（≤40 字符，不能带链接）, \"confirm_text\": \"确认\", \"cancel_text\": \"取消\"}。"
    "★ 上限：最多 5 行、每行最多 5 个按钮（合计 25 个）；action.data ≤ 100 字符；"
    "按钮必须挂在 markdown 消息上（同一条 <msg> 里写 <markdown> 即可）。"
    "★★ **限次/限人/截止（插件侧能力，官方已弃用 click_limit）**："
    "在 <keyboard> 标签上直接写属性即可，例如 "
    "<keyboard max=\"3\" once=\"1\" ttl=\"600\" label=\"报名\">{…}</keyboard>："
    "max=最多接受几次、per=每人最多几次、once=\"1\"=每人一次、"
    "ttl=多少秒后截止、until=\"22:30\"=绝对截止时间、cooldown=连点保护秒数、"
    "label=给这次活动起个名字（查账用）。"
    "默认 **deliver=\"last\"**：中途的点击**不会打扰你**，只有「名额满了/截止了」那一次"
    "会带着汇总（谁点的、共几次）发给你；想让每次点击都发给你就写 deliver=\"all\"，"
    "想完全不被打扰（自己查）写 deliver=\"off\"。"
    "逐按钮的精细策略写在按钮的 \"kirai\" 字段里，例如 "
    "{\"id\":\"b1\",\"render_data\":{\"label\":\"候补\"},\"action\":{…},\"kirai\":{\"max\":2,\"per\":1}}。"
    "★ 想查账随时调用 `qq_button_stats`（谁点了、各几次、剩几个名额、是否截止），"
    "也可以 `qq_button_close` 提前截止 / `qq_button_reset` 重开一轮 / `qq_button_extend` 加名额或延时。"
)
