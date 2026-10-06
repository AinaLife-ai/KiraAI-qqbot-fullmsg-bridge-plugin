# -*- coding: utf-8 -*-
"""「读取用户发来的文件」工具。

为什么不是自动注入正文（设计取舍，重要）
--------------------------------------
先看真实边界——核心 `QQOfficialAdapter._content_elements` 已经把事件里的
`attachments` 变成了 `Image / Record / Video / File` 元素，所以：

| 类型 | 模型能拿到什么 | 结论 |
|---|---|---|
| 图片 | `Image(url)` —— 核心会惰性下载给视觉模型 | ✅ 本来就能用 |
| 语音 | `Record(url)` —— 我们另做了 ASR 文本化 | ✅ 本来就能用 |
| 视频 | `Video(url)` | ✅ 本来就能用 |
| **普通文件** | `File(url)`，而它的 `repr` **只有 `[File 文件名]`** | ❌ **内容读不到** |

所以缺的只有**普通文件**这一段。

为什么不用"自动把内容塞进正文"：
1. 事件构造是**同步**的（`build_event` → `_message_chain`），而下载是网络 IO；
   异步补写会**晚于**消息链构造，正文其实已经定型了（改不到）。
2. 强行同步下载 = **阻塞消息处理链**，群里发个大文件就会卡住所有消息 —— 不可接受。
3. 绝大多数文件模型根本不需要读。

⇒ 改成**按需工具**：模型看到 `[File 报价单.xlsx]` 后，如果真需要，自己调这个工具来读。
零阻塞、零浪费，且**默认开启**（不需要任何权限）。

本模块只依赖：`event.message.chain`（公共结构）+ 核心的 `get_file_content`
（取不到就退回 httpx，保证 2.x / 3.0 都能用）。
"""

from __future__ import annotations

import re
from typing import Any

try:
    from core.utils.tool_utils import BaseTool
except Exception:  # pragma: no cover
    class BaseTool:  # type: ignore
        def __init__(self, *a, **kw):
            pass

#: 单个文件最大读取字节
MAX_BYTES = 2 * 1024 * 1024          # 2 MB
#: 返回给模型的文本上限
MAX_TEXT_CHARS = 8000
#: 下载超时（秒）
FETCH_TIMEOUT = 20.0

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv", ".log",
    ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go",
    ".rs", ".rb", ".php", ".sh", ".bash", ".zsh", ".sql", ".xml", ".yml",
    ".yaml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties",
    ".html", ".htm", ".css", ".scss", ".less", ".vue", ".svelte", ".gitignore",
}
BINARY_SUFFIXES = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".exe", ".dll",
    ".so", ".apk", ".ipa", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".mp3", ".mp4", ".wav", ".silk", ".flac", ".m4a", ".aac", ".ogg",
}


def _suffix_of(name: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]{1,8})$", (name or "").strip())
    return m.group(1).lower() if m else ""


def _human_size(num: Any) -> str:
    try:
        n = int(num)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return ""


async def _fetch(url: str) -> bytes | None:
    """下载字节。优先用核心的工具（跟随重定向/代理更稳），失败退回 httpx。"""
    try:
        from core.utils.network import get_file_content
        return await get_file_content(url, timeout=FETCH_TIMEOUT)
    except Exception:
        pass
    try:
        import httpx
        async with httpx.AsyncClient(follow_redirects=True, timeout=FETCH_TIMEOUT) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception:
        return None


def _collect_files(event) -> list:
    """从事件的消息链里挑出**普通文件**元素（图片/语音/视频不归这里管）。"""
    out = []
    chain = getattr(getattr(event, "message", None), "chain", None)
    items = getattr(chain, "message_list", None)
    if items is None and isinstance(chain, (list, tuple)):
        items = chain
    for ele in items or []:
        # 只认 File：媒体元素（Image/Record/Video）由核心自己的路径处理
        if type(ele).__name__ != "File":
            continue
        url = str(getattr(ele, "file", "") or "")
        if not url:
            continue
        out.append({
            "name": str(getattr(ele, "name", "") or "") or url.rsplit("/", 1)[-1],
            "size": getattr(ele, "size", None),
            "mime": getattr(ele, "mime", None),
            "url": url,
        })
    return out


class ReadAttachedFileTool(BaseTool):
    """让模型按需读取用户发来的文本类文件。"""

    name = "read_qq_attached_file"
    description = (
        "读取用户刚发给机器人的文件内容（当前这条消息里带的文件）。"
        "适合文本类文件：txt、md、json、csv、代码、配置等。"
        "不需要管理员权限。"
        "注意：只能读**当前这条消息**附带的文件；图片、语音、视频不用调这个工具"
        "（它们的内容模型已经能直接看到）。"
        "如果是压缩包、Office 文档、PDF 等二进制文件，本工具只会告诉你文件名和大小。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_name": {
                "type": "string",
                "description": "可选。要读的文件名（消息里有多个文件时用来指定）；留空读第一个",
            },
        },
    }

    async def execute(self, event, *args, file_name: str = "", **kwargs) -> str:
        files = _collect_files(event)
        if not files:
            return ("当前这条消息里没有可读取的文件（图片、语音、视频不需要用本工具，"
                    "模型已经能直接看到它们）。")
        picked = None
        if file_name:
            want = str(file_name).strip().lower()
            for f in files:
                if want in f["name"].lower():
                    picked = f
                    break
            if picked is None:
                names = "、".join(f["name"] for f in files)
                return f"没找到名为“{file_name}”的文件。当前消息里的文件有：{names}"
        else:
            picked = files[0]

        name = picked["name"]
        suffix = _suffix_of(name)
        meta = []
        size = _human_size(picked.get("size"))
        if size:
            meta.append(f"大小 {size}")
        if picked.get("mime"):
            meta.append(f"类型 {picked['mime']}")

        if suffix in BINARY_SUFFIXES:
            return (f"“{name}”是二进制文件（{'，'.join(meta) or '类型未知'}），"
                    "无法直接读成文字。可以告诉用户：需要它转成文本（例如把文档另存为 txt/md），"
                    "或者让用户直接把关键内容粘贴过来。")

        raw = await _fetch(picked["url"])
        if raw is None:
            return f"读取“{name}”失败：下载不到内容（链接可能已过期），请让用户重发一次。"
        if len(raw) > MAX_BYTES:
            return (f"“{name}”太大（{_human_size(len(raw))}），只读取前一部分。\n"
                    + self._decode(raw[:MAX_BYTES], name))
        return self._decode(raw, name)

    @staticmethod
    def _decode(raw: bytes, name: str) -> str:
        for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            return f"“{name}”的内容不是文本，无法读取。"
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n…（内容过长，已截断）"
        return f"“{name}”的内容：\n{text}"
