"""L1 工具层：撤回 / 禁言 / 禁言查询 / 机器人群内状态。

为什么单独一层
------------
这一层的工具**只用 `client.api._http` + `Route` 直发官方接口**，不依赖任何补丁、
不依赖世代落点、不依赖 core 的私有属性 —— 因此：

* **2.x / 3.0 / 未来版本都能用**；
* 即使其它层因为版本变动被降级，这一层照常工作；
* 单元测试可以直接用一个假 client 驱动，不需要真框架。

平台事实（决定每个工具的行为）
---------------------------
| 接口 | 权限 |
|---|---|
| `DELETE /v2/groups/{gid}/messages/{mid}` | 10 QPS；**2 分钟内**；群管可撤他人，普通成员只能撤自己 |
| `POST /v2/groups/{gid}/restrict_chat_setting` | 60 QPM；**需群管理员**；最长 30 天；单次 ≤ 20 人 |
| `GET  /v2/groups/{gid}/restrict_chat_setting` | 30 QPM；**需群管理员** |
| `GET  /v2/groups/{gid}/bot_state` | 30 QPM；**白名单（11253）** |

注意：这里**刻意不做** `send_action` 兜底 —— 那是 NapCat 私有协议，官方 client 没有这个方法
（实测确认），写了只会误导。
"""

from __future__ import annotations

from typing import Optional

try:
    from core.utils.tool_utils import BaseTool
except Exception:  # pragma: no cover
    class BaseTool:  # type: ignore
        def __init__(self, *a, **kw):
            pass


#: 官方错误码 → 人话（给模型看，避免它反复重试同一个注定失败的调用）
ERROR_HINTS = {
    "11253": "该接口只对白名单（内邀）机器人开放，当前机器人没有权限，请不要再重试",
    "40061001": "请求参数无效",
    "40062003": "无操作权限：机器人不是群管理员，或目标用户不是普通成员",
    "40064004": "已超出消息撤回时限（官方规定只能撤回 2 分钟内发送的消息）",
    "40054002": "机器人被禁言，请等待解禁后再发送",
    "40054003": "机器人不是群成员",
    "40034100": "主动消息发送超过频控限制",
    "40034101": "机器人非群成员",
    "40034105": "主动消息发送失败：无权限",
    "50065001": "消息撤回失败，请稍后重试",
    "50055001": "消息发送异常，请稍后重试",
    "850018": "群被禁言或机器人被禁言",
}


def humanize_error(exc: Exception) -> str:
    """把官方错误翻译成人话；找不到就回原始文本（截断）。"""
    text = str(exc)
    for code, hint in ERROR_HINTS.items():
        if code in text:
            return f"{hint}（{code}）"
    return text[:200] or type(exc).__name__


class _ApiTool(BaseTool):
    """L1 工具的公共基类：负责拿 client、拿 target、兜异常。"""

    #: 子类声明是否只能群聊用
    group_only = True

    def _adapter(self, event):
        ada_name = getattr(getattr(event, "session", None), "adapter_name", None)
        if not ada_name:
            return None
        try:
            return self.ctx.adapter_mgr.get_adapter(ada_name)
        except Exception:
            return None

    def _client(self, event):
        ada = self._adapter(event)
        if ada is None:
            return None
        try:
            return ada.get_client()
        except Exception:
            return None

    def _target_id(self, event) -> str:
        session = getattr(event, "session", None)
        return str(getattr(session, "session_id", "") or "")

    @staticmethod
    def _is_group(event) -> bool:
        fn = getattr(event, "is_group_message", None)
        if callable(fn):
            try:
                return bool(fn())
            except Exception:
                pass
        session = getattr(event, "session", None)
        return str(getattr(session, "session_type", "")) == "gm"

    async def _request(self, event, method: str, path: str,
                       _params: Optional[dict] = None,
                       _json: Optional[dict] = None, **path_kwarps):
        """发一个官方 openapi 请求。

        `path_kwarps` 是**路径参数**（如 `group_openid`），
        `_params` 给 GET 的 query string，`_json` 给 POST/PUT 的请求体。
        """
        client = self._client(event)
        if client is None:
            raise RuntimeError("QQ 官方机器人未连接（client 为空），请稍后再试")
        api = getattr(client, "api", None)
        if api is None:
            raise RuntimeError("当前适配器不是 QQ 官方机器人，本工具不可用")
        http = getattr(api, "_http", None)
        if http is None:
            raise RuntimeError("QQ 官方机器人 HTTP 通道不可用，请稍后再试")
        from botpy.http import Route  # 延迟导入：没有 botpy 时插件仍可加载

        route = Route(method, path, **path_kwarps)
        if _params is not None:
            return await http.request(route, params=_params)
        if _json is not None:
            return await http.request(route, json=_json)
        return await http.request(route)


# --------------------------------------------------------------------------- #
# 1. 撤回消息
# --------------------------------------------------------------------------- #
class RecallQQMsgTool(_ApiTool):
    name = "recall_qq_msg"
    description = (
        "撤回一条 QQ 官方机器人会话里的消息。"
        "注意：官方只允许撤回 2 分钟内发送的消息；机器人是群管理员时可以撤回他人消息，"
        "普通成员身份只能撤回机器人自己发的消息。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "要撤回的消息 ID（消息 id，不是展示用的 qqo-xxx）",
            },
        },
        "required": ["message_id"],
    }

    async def execute(self, event, *args, message_id: str = "", **kwargs) -> str:
        target = self._target_id(event)
        if not target or not message_id:
            return "撤回失败：缺少会话或消息 ID"
        is_group = self._is_group(event)
        path = ("/v2/groups/{group_openid}/messages/{message_id}" if is_group
                else "/v2/users/{user_openid}/messages/{message_id}")
        # 路径参数名两版官方文档不一致（群用 group_openid，单聊用 user_openid）
        key = {"group_openid": target} if is_group else {"user_openid": target}
        key["message_id"] = message_id
        try:
            await self._request(event, "DELETE", path, **key)
        except Exception as exc:
            return f"撤回失败：{humanize_error(exc)}"
        return "撤回成功"


# --------------------------------------------------------------------------- #
# 2. 禁言 / 解禁
# --------------------------------------------------------------------------- #
class SetGroupMuteTool(_ApiTool):
    name = "set_qq_group_ban"
    description = (
        "禁言或解除禁言 QQ 群里的成员。"
        "需要机器人是群管理员；只能操作普通成员（不能操作群主/管理员/机器人）；"
        "最长 30 天。duration 传 0 表示立即解除禁言。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "目标成员的 member_openid"},
            "duration": {
                "type": "integer",
                "description": "禁言时长（秒）；0 表示立即解除禁言。默认 600（10 分钟），最大 2592000（30 天）",
                "default": 600,
            },
        },
        "required": ["user_id"],
    }

    MAX_SECONDS = 30 * 24 * 3600

    async def execute(self, event, *args, user_id: str = "", duration: int = 600, **kwargs) -> str:
        target = self._target_id(event)
        if not target or not user_id:
            return "禁言失败：缺少群或成员 ID"
        if not self._is_group(event):
            return "禁言失败：该操作只在群聊中可用"
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            return "禁言失败：duration 必须是整数秒"
        if duration < 0:
            return "禁言失败：duration 不能为负数"
        if duration > self.MAX_SECONDS:
            duration = self.MAX_SECONDS

        import datetime

        if duration == 0:
            member = {"op": "del", "member_openid": user_id, "mute_expire_at": ""}
            said = "已解除禁言"
        else:
            expire = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                seconds=duration
            )
            # 官方要求 RFC3339；这里给东八区的偏移，与官方示例一致
            expire = expire.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
            member = {
                "op": "add",
                "member_openid": user_id,
                "mute_expire_at": expire.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            }
            said = f"已禁言 {duration} 秒"

        try:
            await self._request(
                event, "POST", "/v2/groups/{group_openid}/restrict_chat_setting",
                group_openid=target, _json={"members": [member]},
            )
        except Exception as exc:
            return f"禁言失败：{humanize_error(exc)}"
        return said


# --------------------------------------------------------------------------- #
# 3. 查询禁言状态
# --------------------------------------------------------------------------- #
class GetGroupMuteStateTool(_ApiTool):
    name = "get_group_mute_state"
    description = (
        "查询 QQ 群当前的禁言状态（全员禁言模式 + 正在被禁言的成员列表）。需要机器人是群管理员。"
    )
    parameters = {"type": "object", "properties": {}}

    async def execute(self, event, *args, **kwargs) -> str:
        target = self._target_id(event)
        if not target or not self._is_group(event):
            return "查询失败：该操作只在群聊中可用"
        try:
            data = await self._request(
                event, "GET", "/v2/groups/{group_openid}/restrict_chat_setting",
                group_openid=target,
            )
        except Exception as exc:
            return f"查询失败：{humanize_error(exc)}"
        if not isinstance(data, dict):
            return "查询失败：接口返回格式异常"
        global_rule = data.get("global_rule") if isinstance(data.get("global_rule"), dict) else {}
        mode = global_rule.get("mode") or "none"
        mode_text = {"none": "未开启", "always": "始终禁言", "schedule": "定时/周期禁言"}.get(
            str(mode), str(mode)
        )
        members = data.get("members") if isinstance(data.get("members"), list) else []
        lines = [f"全员禁言：{mode_text}"]
        if members:
            lines.append(f"当前被禁言成员 {len(members)} 人：")
            for m in members[:20]:
                if isinstance(m, dict):
                    lines.append(
                        f"- {m.get('username') or m.get('member_openid')} "
                        f"至 {m.get('mute_expire_at')}"
                    )
        else:
            lines.append("当前没有被禁言的成员")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 4. 机器人群内状态（白名单接口，做优雅降级）
# --------------------------------------------------------------------------- #
class GetQQBotStateTool(_ApiTool):
    name = "get_qq_bot_state"
    description = (
        "查询机器人在当前群的状态：收消息范围（all/only_mention/mention_and_context）、"
        "是否允许主动发言、机器人是不是群主/管理员。"
        "注意：该接口对非白名单机器人会返回 11253（不可用），此时如实告知用户即可。"
    )
    parameters = {"type": "object", "properties": {}}

    async def execute(self, event, *args, **kwargs) -> str:
        target = self._target_id(event)
        if not target or not self._is_group(event):
            return "查询失败：该操作只在群聊中可用"
        try:
            data = await self._request(
                event, "GET", "/v2/groups/{group_openid}/bot_state", group_openid=target
            )
        except Exception as exc:
            return f"查询失败：{humanize_error(exc)}"
        if not isinstance(data, dict):
            return "查询失败：接口返回格式异常"
        recv = str(data.get("recv_msg_setting") or "?")
        recv_text = {
            "all": "全部消息（全量模式已开，推荐）",
            "only_mention": "仅 @ 机器人",
            "mention_and_context": "@机器人及其上下文",
        }.get(recv, recv)
        role = str(data.get("member_role") or "?")
        role_text = {"member": "普通成员", "admin": "管理员", "owner": "群主"}.get(role, role)
        allow = data.get("allow_proactive_msg")
        return (
            f"收消息范围：{recv_text}\n"
            f"允许主动发言：{'是' if allow else '否'}（否 = 群主未开启「机器人主动在群聊内发言」）\n"
            f"机器人群内身份：{role_text}\n"
            f"入群时间：{data.get('joined_at') or '?'}"
        )


#: 按配置开关挑选要注册的工具
def build_tools(cfg: dict) -> list:
    tools = []
    if cfg.get("recall_enabled", True):
        tools.append(RecallQQMsgTool)
    if cfg.get("mute_enabled", True):
        tools.append(SetGroupMuteTool)
        tools.append(GetGroupMuteStateTool)
    if cfg.get("bot_state_enabled", True):
        tools.append(GetQQBotStateTool)
    return tools
