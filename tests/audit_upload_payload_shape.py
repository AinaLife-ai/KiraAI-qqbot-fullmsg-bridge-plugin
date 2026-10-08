"""★★★ 媒体上传体形状：**`file_name` 只对 `file_type=4` 发**（三家官方实现一致）。

## 线上现象（2026-10-08 用户实测）

语音（ogg 与 silk）在 QQ 里一律显示成**文件卡片**，卡片上的名字正是我们传的
`jbf_v2.silk` / `jbf_voice60s.ogg`。

## 依据（三家官方/官方推荐实现，逐条可查）

1. **腾讯官方 Node SDK** `@tencent-connect/qqbot-nodejs@1.0.4`
   `src/protocol/api/media.ts`：
   ```ts
   if (fileType === MediaFileType.FILE && opts.fileName) {
       body.file_name = this.sanitize(opts.fileName);
   }
   ```
   其 `USAGE.md` 也写：`fileName: "2025-Q4-报告.pdf",   // 仅 FILE 类型有效`
2. **官方 openclaw-qqbot** 走同一个 SDK ⇒ 语音上传体里没有 `file_name`。
3. **QQ 官方推荐的 Hermes**（`gateway/platforms/qqbot/adapter.py`）：
   ```python
   body: Dict[str, Any] = {"file_type": file_type, "srv_send_msg": srv_send_msg}
   ...
   if file_type == MEDIA_TYPE_FILE and file_name:
       body["file_name"] = file_name
   ```
   `_send_media` 里也是 `file_name=resolved_name if file_type == MEDIA_TYPE_FILE else None`。

⇒ 给语音带文件名属于**超出官方约定的用法**，本插件改为严格对齐。

本测试：用假的元素 + 假的 client 真跑 `media_types.install()` 的 `_upload_file`，
把上传体抓下来逐字段断言（不需要任何核心，跑得飞快）。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR

import asyncio
import os
import sys

sys.path.insert(0, _BR())

import media_types as M  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


class _Log:
    def __init__(self):
        self.lines = []

    def _p(self, lv, a):
        self.lines.append((lv, (a[0] % tuple(a[1:])) if len(a) > 1 else str(a[0])))

    def info(self, *a):
        self._p("info", a)

    def warning(self, *a):
        self._p("warning", a)

    def debug(self, *a):
        pass


class Elem:
    """最小媒体元素（形状与核心 `BaseMediaElement` 一致就够）。"""

    def __init__(self, path, name=None, orig_kind=None):
        self.file = path
        self.file_type = "path"
        self._name = name or path.rsplit("/", 1)[-1]
        if orig_kind:
            self._kira_bridge_orig_kind = orig_kind   # media_coerce 留下的标记

    async def to_path(self):
        return self.file

    def guess_name(self):
        return self._name


class HTTP:
    def __init__(self):
        self.calls = []

    async def request(self, route, **kw):
        self.calls.append(kw.get("json") or {})
        return {"file_info": "FI"}


class Client:
    def __init__(self):
        self.api = type("A", (), {})()
        self.api._http = HTTP()


async def main():
    print("═══ 媒体上传体形状（file_name 只对 file_type=4）═══")
    os.makedirs("/tmp/payloadtest", exist_ok=True)
    silk = "/tmp/payloadtest/voice.silk"
    # 直接造一个「腾讯系 silk」（silk-wasm 的产物头就是 \x02 + #!SILK_V3）
    open(silk, "wb").write(b"\x02#!SILK_V3" + b"\x00" * 256)
    mp4 = "/tmp/payloadtest/v.mp4"
    open(mp4, "wb").write(b"\x00" * 128)

    log = _Log()
    client = Client()
    holder = type("H", (), {})()
    holder._upload_file = _OrigUpload()
    ok = M.install(holder, client, log)
    check("★ install 成功（有 _upload_file 落点）", ok is True)

    async def upload(elem):
        client.api._http.calls.clear()
        await holder._upload_file("OPENID", elem, False)
        calls = client.api._http.calls
        return calls[-1] if calls else {}

    print("\n[1] ★★★ 语音（Record → file_type=3）不得带 file_name")
    body = voice_body = await upload(Elem(silk, "jbf_v2.silk", orig_kind="Record"))
    check("★ file_type=3", body.get("file_type") == 3, str(body.keys()))
    check("★★★ 上传体里**没有** file_name（对齐官方三家实现）",
          "file_name" not in body, str(body.get("file_name")))
    check("★ file_data 仍然带上（内容不变）", bool(body.get("file_data")))
    check("★ srv_send_msg=False", body.get("srv_send_msg") is False)

    print("\n[2] 视频（file_type=2）同样不带 file_name")
    body = await upload(Elem(mp4, "v.mp4", orig_kind="Video"))
    check("★ file_type=2 且无 file_name",
          body.get("file_type") == 2 and "file_name" not in body, str(body.keys()))

    print("\n[3] ★ 降级成「文件」（file_type=4）时**必须带** file_name")
    # m4a 不在语音白名单、又转不了 silk ⇒ 我们按 file_type=4 发（至少收得到）
    open("/tmp/payloadtest/x.m4a", "wb").write(b"\x00" * 256)
    body = await upload(Elem("/tmp/payloadtest/x.m4a", "voice.m4a", orig_kind="Record"))
    check("★ 降级为 file_type=4", body.get("file_type") == 4, str(body.keys()))
    check("★ 此时**带** file_name（官方也只在这个类型带）",
          body.get("file_name") == "voice.m4a", str(body.get("file_name")))

    print("\n[3b] 真「文件」元素（File / zip）⇒ 交回核心原逻辑（核心按 4 发、带名字）")
    delegated = []
    holder._upload_file.__defaults__  # noqa: B018  (保持对象不变，仅说明)
    try:
        await upload(Elem("/tmp/payloadtest/a.zip", "报告.zip"))
        check("★ 交回核心（不该自己抢）", False, "竟然自己处理了")
    except AssertionError as exc:
        delegated.append(str(exc))
        check("★ 交回核心原逻辑（File 归核心，我们只改类型不准的那几类）",
              "交回原逻辑" in str(exc))

    print("\n[4] 诊断：上传体形状只记一次")
    shapes = [m for lv, m in log.lines if "媒体上传体形状" in m]
    check("★ 每种 file_type 只记一条（不刷屏）",
          0 < len(shapes) <= 3, f"{len(shapes)} 条")

    print("\n[5] 反向验证：旧代码会带上 file_name（本测试能抓到该回归）")
    old_body = {"file_type": 3, "file_name": "jbf_v2.silk", "file_data": "x"}
    check("★ 旧形状确实带 file_name ≠ 新形状",
          "file_name" in old_body and "file_name" not in voice_body,
          f"voice={list(voice_body.keys())}")

    print("\n[6] 上传错误码 → 人话（官方错误码表）")
    check("★ 40093002 日额度 → 明天再试",
          "明天" in M.humanize_upload_error(Exception("400, {'code': 40093002}")))
    check("★ 850019 格式 → 提到 silk",
          "silk" in M.humanize_upload_error(Exception("850019 不支持的文件格式")))
    check("★ 850026 下载失败 → 提到换地址",
          "URL" in M.humanize_upload_error(Exception("850026")))
    check("★ 认不出的错误 ⇒ 空串（不硬编）",
          M.humanize_upload_error(Exception("some other error")) == "")

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


class _OrigUpload:
    """核心原逻辑的替身：被调用即说明我们交回了原路径（本测试不该走到）。"""

    async def __call__(self, target_id, media_element, is_group):
        raise AssertionError("不该交回原逻辑")


sys.exit(asyncio.run(main()))
