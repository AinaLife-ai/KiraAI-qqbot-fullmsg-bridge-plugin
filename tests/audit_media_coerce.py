"""★ 核心漏掉的 `Record` / `Video` —— 插件侧补回发送链（不改核心）。

## 线上现象（2026-10-07）

模型发 `<msg><file type="record">xxx.mp3</file></msg>`，
群里收到的却是 **`[Unsupported message element]`**。

## 根因（核心侧，两代一样）

    media_elements = [e for e in chain if isinstance(e, (File, Image))]   # 没有 Record/Video

⇒ `Record` 进不了发送链；`_text_content` 也不认识它 ⇒ 填空占位文本发出去。

## 插件侧修法（不动核心）

发送前把 `Record`/`Video` **临时换壳成 `File`**（核心白名单里有），
发完**还原**（绝不改用户的消息链）；同时用 `_kira_bridge_orig_kind` 标记
让 `_upload_file` 包装按**原始类型**给对 `file_type`（视频→2 / 语音→3）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import os
import sys

ROOT = _BR()
GEN = os.environ.get("KIRA_CORE_GEN", "3")
sys.path.insert(0, str(_CORE_ROOT("3")) if GEN == "3" else str(_CORE_ROOT("2")))
sys.path.insert(0, ROOT)
sys.path.insert(0, _BOTPY_DIR())

os.makedirs(f"{ROOT}/data", exist_ok=True)
open(f"{ROOT}/data/log.log", "a").close()

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


MEDIA = "/tmp/reco/x.mp3"
os.makedirs("/tmp/reco", exist_ok=True)
if not os.path.exists(MEDIA):
    open(MEDIA, "wb").write(b"ID3\x03\x00\x00\x00\x00\x00\x00")


async def main():
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
    from core.chat import MessageChain
    from core.chat.message_elements import Record, Text, Video

    print(f"═══ 媒体元素补全 GEN={GEN} ═══")

    if GEN == "3":
        from core.adapter.context import AdapterContext
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                           platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        ad = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
    else:
        info = AdapterInfo(adapter_id="t", enabled=True, name="qqo",
                           platform="QQ Official",
                           config={"app_id": "a", "app_secret": "b",
                                   "permission_mode": "deny_list",
                                   "group_deny_list": [], "user_deny_list": []})
        ad = QQOfficialAdapter(info, asyncio.Queue())

    sent = []

    class Route2:
        def __init__(self, *a, **k):
            self.a, self.k = a, k

    class HTTP:
        async def request(self, route, **kw):
            sent.append({"__upload__": kw})
            return {"file_info": "FI", "raw_url": "https://cos.example.com/f.png"}

    class API:
        async def post_group_message(self, **kw):
            sent.append(kw)
            return {"id": "R1", "ext_info": {"ref_idx": "X=="}}

        async def post_c2c_message(self, **kw):
            sent.append(kw)
            return {"id": "R2", "ext_info": {"ref_idx": "Y=="}}

        async def post_group_file(self, **kw):
            sent.append({"__url_upload__": kw})
            return {"file_info": "FI"}

        async def post_c2c_file(self, **kw):
            sent.append({"__url_upload__": kw})
            return {"file_info": "FI"}

    ad.client = type("C", (), {})()
    ad.client.api = API()
    ad.client.api._http = HTTP()
    ad.client._connection = None
    ad._client_task = type("T", (), {"done": lambda s: False})()

    import main as bridge_main

    class _Mgr:
        def get_adapters(self):
            return {"qqo": ad}

        def get_adapter(self, n):
            return ad if n == "qqo" else None

    class Ctx:
        adapter_mgr = _Mgr()

    p = bridge_main.QQOfficialGroupBridge(
        Ctx(), {"section_basic": {"enabled": True},
                "section_proactive": {"proactive_enabled": True}})
    p._attach(ad, "qqo", {})

    print("\n[1] Record（语音）—— 必须真的作为富媒体发出")
    sent.clear()
    ch = MessageChain([Text("试试"), Record(MEDIA, name="x.mp3", mime="audio/mpeg")])
    await ad.send_group_message("G1", ch)
    got = sent[-1] if sent else {}
    print(f"      报文：{str(got)[:130]}")
    check("★ 不再发出 [Unsupported message element]",
          "[Unsupported message element]" not in str(got.get("content") or ""),
          str(got)[:150])
    check("★ 被当作媒体发出（msg_type=7 或带 media）",
          got.get("msg_type") == 7 or got.get("media") is not None, str(got)[:150])
    check("★ 消息链已还原成 Record（不改用户内容）",
          type(ch[1]).__name__ == "Record", type(ch[1]).__name__)

    print("\n[2] Video（视频）—— 同理")
    sent.clear()
    vid = Video(MEDIA, name="v.mp4", mime="video/mp4") if "Video" in dir() else None
    if vid is not None:
        ch2 = MessageChain([Text("看看"), vid])
        await ad.send_group_message("G1", ch2)
        got2 = sent[-1] if sent else {}
        check("★ Video 也不再是占位文本",
              "[Unsupported message element]" not in str(got2.get("content") or ""),
              str(got2)[:150])
        check("★ 链已还原", type(ch2[1]).__name__ == "Video", type(ch2[1]).__name__)

    print("\n[3] 纯文本 / md 不受影响（不能误伤）")
    from rich_content import MarkdownText
    sent.clear()
    ch3 = MessageChain([MarkdownText("## 标题\n正文")])
    await ad.send_group_message("G1", ch3)
    got3 = sent[-1] if sent else {}
    check("★ 普通 md 仍按 markdown 发", got3.get("msg_type") == 2, str(got3)[:120])

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


sys.exit(asyncio.run(main()))
