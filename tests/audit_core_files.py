"""验证：核心 QQ 适配器**原生就能发文件**（决定我们的 send_qq_file 是否多余）。

依据（源码）：
  * 2.x `qq_official.py:337/362/480` —— `File`/`Image` 元素 → `_upload_file()`
    → `POST /v2/groups|users/{id}/files` → 再带 media 发消息；
  * 3.x `im.py:198/223/363/379` 同款；
  * 框架内置 `kira-ai` 插件**默认注册** `<file>` 标签（`main.py:90`），
    模型写 `<file>https://…</file>` 就会产生 `File` 元素。

若本测试通过 ⇒ 模型**本来就能发文件** ⇒ 我们再包一个 `send_qq_file` 工具是重复的。
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", "/var/minis/workspace/qqbot_bridge_review/kira-core"))
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))
_BOTPY = os.environ.get("BOTPY_PATH", "/tmp/botpy_src/botpy-master")
if os.path.isdir(_BOTPY):
    sys.path.insert(0, _BOTPY)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def main():
    print("=" * 72)
    print("## 核对：核心是否原生支持发文件（决定 send_qq_file 存废）")
    print("=" * 72)

    # 2.x 把发送链放在 qq_official.py；3.0 拆成了 im.py —— 两版都读，合并判定
    base = CORE / "core/adapter/src/qq_official"
    parts = []
    for name in ("qq_official.py", "im.py"):
        f = base / name
        if f.is_file():
            parts.append(f.read_text(encoding="utf-8"))
    text = "\n".join(parts)
    print(f"\n源码：{base.relative_to(CORE.parent)}/ (qq_official.py + im.py)")

    # ---- 1. 源码层：确实有上传实现 ----
    print("\n[1] 源码层证据")
    check("★ 有 _upload_file 实现", "async def _upload_file" in text)
    check("★ 上传走官方 files 端点",
          "/v2/groups/{group_openid}/files" in text)
    check("★ 私聊走 users files 端点", "/v2/users/{openid}/files" in text)
    check("★ 发送侧把 File/Image 元素识别为媒体",
          "isinstance(element, (File, Image))" in text
          or "isinstance(element, (Reply, File, Image))" in text)

    # ---- 2. 框架内置插件：<file> 标签默认注册 ----
    print("\n[2] 框架内置 kira-ai 插件")
    ki = CORE / "core/plugin/builtin_plugins/kira-ai"
    main_py = (ki / "main.py").read_text(encoding="utf-8")
    check("★ <file> 标签默认注册（模型可直接用）",
          "build_file_tag" in main_py and "tag_set.register(build_file_tag" in main_py)
    tags_py = (ki / "tags.py").read_text(encoding="utf-8")
    check("★ FileTag 接受 URL 与本地路径",
          "http://" in tags_py and "resolve_local_send_path" in tags_py)

    # ---- 3. 端到端：File 元素真的会触发上传 ----
    print("\n[3] 端到端：chain 里放 File 元素，看是否真的调上传")
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.src.qq_official.qq_official import QQOfficialAdapter
    from core.chat.message_elements import File

    info = AdapterInfo(adapter_id="t", enabled=True, name="qq",
                       platform="QQ Official Bot",
                       config={"app_id": "a", "app_secret": "b",
                               "permission_mode": "deny_list",
                               "group_deny_list": [], "user_deny_list": []})
    if os.environ.get("KIRA_CORE_GEN") == "3":
        from core.adapter.context import AdapterContext
        adapter = QQOfficialAdapter(AdapterContext(info=info, event_queue=asyncio.Queue()))
    else:
        adapter = QQOfficialAdapter(info, asyncio.Queue())

    calls = []

    class FakeHTTP:
        async def request(self, route, **kw):
            calls.append({"method": route.method, "url": route.url, "json": kw.get("json")})
            u = route.url
            if "/upload_prepare" in u:
                return {"upload_id": "UP1", "block_size": 1024,
                        "parts": [{"index": 1, "url": "https://cos/p1"}]}
            if u.endswith("/files"):
                return {"file_info": "FILEINFO", "file_uuid": "FU1", "ttl": 3600}
            return {}

    class FakeAPI:
        def __init__(self):
            self._http = FakeHTTP()

        async def post_group_message(self, **kw):
            calls.append({"method": "POST_MSG", "json": kw})
            return {"id": "MID1"}

        async def post_c2c_message(self, **kw):
            calls.append({"method": "POST_MSG", "json": kw})
            return {"id": "MID1"}

        async def post_group_file(self, **kw):
            # ★ 核心原生的「发文件」入口（2.x 走这个）
            calls.append({"method": "post_group_file", "json": kw})
            return {"file_info": "FILEINFO", "file_uuid": "FU1", "ttl": 3600}

        async def post_c2c_file(self, **kw):
            calls.append({"method": "post_c2c_file", "json": kw})
            return {"file_info": "FILEINFO", "file_uuid": "FU1", "ttl": 3600}

    adapter.client = type("C", (), {})()
    adapter.client.api = FakeAPI()
    # _send_message 的前置守卫：需要 client 就绪 + _client_task 未结束 + 有可回复的 id
    fake_task = type("T", (), {"done": lambda self: False})()
    adapter._client_task = fake_task
    try:
        adapter._group_reply_ids = {"G1": "ROBOT1.0_realmsgid"}
    except Exception:
        pass

    from core.chat import MessageChain
    chain = MessageChain([File("https://example.com/report.pdf", name="report.pdf")])
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(adapter.send_group_message("G1", chain))
    except Exception as exc:
        print(f"      （发送抛异常，属可接受：{type(exc).__name__}: {str(exc)[:80]}）")

    urls = [c.get("url", "") for c in calls]
    hit_files = [u for u in urls if u and "/files" in u]
    hit_api = [c for c in calls if c.get("method") in ("post_group_file", "post_c2c_file")]
    check("★ chain 里的 File 元素触发了核心原生的发文件入口",
          bool(hit_files) or bool(hit_api),
          f"请求: {urls} / api: {[c.get('method') for c in calls]}")
    if hit_files:
        print(f"      官方端点: {hit_files[0]}")
    if hit_api:
        print(f"      核心入口: {hit_api[0]['method']}")

    print()
    print("=" * 72)
    if FAIL == 0:
        print("结论：核心原生支持发文件 ⇒ 我们的 send_qq_file 属**重复实现**")
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
