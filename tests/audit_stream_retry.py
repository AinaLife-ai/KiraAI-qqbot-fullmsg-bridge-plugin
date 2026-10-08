"""★★ 分片上传的重试策略（逐条对齐官方实现）。

## 为什么加（用户 2026-10-09 点名）

原来 `upload_prepare` / 分片 PUT / `upload_part_finish` / 合并**一个都不重试** ——
网络抖一下整条转存就失败，用户只看到"图又发不出来"（前端退化成 alt 文字）。

## 官方参数（两家逐项一致，照抄不自创）

腾讯 Node SDK `@tencent-connect/qqbot-nodejs` 的 `retry.ts`：

    UPLOAD_RETRY_POLICY          maxRetries 2 / base 1000ms / 指数退避（prepare）
    COMPLETE_UPLOAD_RETRY_POLICY maxRetries 2 / base **2000ms** / 指数退避（合并）
    PART_FINISH_RETRY_POLICY     maxRetries 2 / base 1000ms / 指数退避
    buildPartFinishPersistentPolicy  命中 **40093001** ⇒ 持久重试（间隔 1s、
                                     时限 = prepare 的 retry_timeout，默认 120s、上限 600s）
    UPLOAD_PREPARE_FALLBACK_CODE = **40093002**（日额度）⇒ **不重试**

Hermes `gateway/platforms/qqbot/chunked_upload.py` 是同一套数字（Python 版）：
`_PART_UPLOAD_MAX_RETRIES = 2`、`_COMPLETE_UPLOAD_MAX_RETRIES = 2`、
`_COMPLETE_UPLOAD_BASE_DELAY = 2.0`、`_PART_FINISH_RETRY_INTERVAL = 1.0`、
`_PART_FINISH_DEFAULT_TIMEOUT = 120`、`_PART_FINISH_MAX_TIMEOUT = 600`。

本测试：真跑 `_upload_bytes_to_qq`（只把 `ClientSession.put` 与「直链自检」换掉），
用**脚本化的失败**逐一验证：该重试的重试、该放手的不硬撑、绝不抛异常。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR

import asyncio
import random
import sys
import time

sys.path.insert(0, _BR())

import aiohttp as _ah

_RealSession = _ah.ClientSession
PUTS = []
PUT_SCRIPT = []          # 每个元素：None=成功，否则抛的异常消息


class _FakeResp:
    def __init__(self, status=200):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _PatchedSession(_RealSession):
    def put(self, url, **kw):
        PUTS.append(len(kw.get("data", b"")))
        if PUT_SCRIPT:
            nxt = PUT_SCRIPT.pop(0)
            if nxt is not None:
                raise RuntimeError(nxt)
        return _FakeResp()


_ah.ClientSession = _PatchedSession

import md_media as M  # noqa: E402

# 直链自检要联网：测试里换成 no-op（它只做观测，不影响被测逻辑）
M._verify_public_url = lambda *a, **k: None

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


class HTTP:
    """按**路径**应答，并可用 `faults` 注入故障。

    `faults` = {路径关键字: [依次抛出?]}：每次命中该路径先 pop 一个；
    非空字符串 ⇒ 抛这个异常，None 或队列空 ⇒ 正常应答。
    （要"一直失败"就往队列里多塞几个，别让它耗尽 —— 测试里显式写清次数。）
    """

    def __init__(self, prep_resp="AUTO", faults=None):
        self.prep = prep_resp
        self.faults = {k: list(v) for k, v in (faults or {}).items()}
        self.calls = []

    async def request(self, route, **kw):
        path = getattr(route, "path", "")
        self.calls.append(path)
        for key, seq in self.faults.items():
            if key in path and seq:
                nxt = seq.pop(0)
                if nxt:
                    raise RuntimeError(nxt)
                break
        if "upload_prepare" in path:
            return prep(1_000_000, 600_000) if self.prep == "AUTO" else self.prep
        if "upload_part_finish" in path:
            return {"ok": 1}
        if "/files" in path:
            return {"file_info": "FI", "raw_url": "https://cos.example.com/x.png?sig=1"}
        return {}

    def count(self, key):
        return sum(1 for p in self.calls if key in p)


def prep(total, bs):
    n = max(1, (total + bs - 1) // bs)
    return {"upload_id": "UP1", "parts": [
        {"index": i, "block_size": str(min(bs, total - i * bs)),
         "presigned_url": f"http://cos/{i}"} for i in range(n)]}


OK_FILES = {"file_info": "FI", "raw_url": "https://cos.example.com/x.png?sig=1"}


async def main():
    print("═══ 分片上传重试（对齐官方策略）═══")
    OFFICIAL = {                        # 抄下来核对（官方文档/SDK 里的原始值）
        "retries": M._UPLOAD_RETRIES,
        "base": M._UPLOAD_BASE_DELAY,
        "complete_base": M._COMPLETE_BASE_DELAY,
        "interval": M._PART_FINISH_INTERVAL,
        "pf_timeout": M._PART_FINISH_DEFAULT_TIMEOUT,
        "pf_max": M._PART_FINISH_MAX_TIMEOUT,
        "put_timeout": M._PART_PUT_TIMEOUT,
        "budget": M._UPLOAD_SOFT_BUDGET,
    }
    M._UPLOAD_SOFT_BUDGET = 12.0        # 缩短预算，测试跑得快（真实值 35s）
    M._UPLOAD_BASE_DELAY = 0.05         # 只影响测试时长，次数/逻辑不变
    M._COMPLETE_BASE_DELAY = 0.05
    M._PART_FINISH_INTERVAL = 0.05

    global PUT_SCRIPT
    random.seed(1)
    data = random.randbytes(1_000_000)

    print("\n[1] prepare 首次失败（网络抖动）⇒ 自动重试后成功")
    http = HTTP(faults={"upload_prepare": ["Server Disconnected"]})
    PUT_SCRIPT = []
    out = await M._upload_bytes_to_qq(_client_with(http), "G1", True, data, "x.png", logger=_Log())
    check("★ 拿到 raw_url（重试救回来了）", isinstance(out, str) and out.startswith("https://"), repr(out))
    check("★ prepare 被调用 2 次（1 失败 + 1 成功）",
          http.count("upload_prepare") == 2, str(http.calls))

    print("\n[2] prepare 返回日额度 40093002 ⇒ **不重试**（省额度）+ 如实给出人话")
    log = _Log()
    http = HTTP(faults={"upload_prepare": ["400, {'code': 40093002, 'message': '超过今天发送文件容量上限'}"] * 5})
    out = await M._upload_bytes_to_qq(_client_with(http), "G1", True, data, "x.png", logger=log)
    check("★ 放弃转存（返回 None）", out is None)
    check("★★ 只调用了 1 次（不重试）", http.count("upload_prepare") == 1, str(http.calls))
    check("★ 日志里给出「明天再试」的人话",
          any("明天" in m for _lv, m in log.lines), str(log.lines[-2:]))

    print("\n[3] 分片 PUT 失败两次 ⇒ 第 3 次成功（官方 maxRetries=2 ⇒ 共 3 次）")
    http = HTTP()
    PUT_SCRIPT = ["COS PUT returned HTTP 500", "COS PUT returned HTTP 503"]
    PUTS.clear()
    out = await M._upload_bytes_to_qq(_client_with(http), "G1", True, data, "x.png", logger=_Log())
    check("★ 最终成功", isinstance(out, str), repr(out))
    check("★★ 第 1 片尝试了 3 次（2 失败 + 1 成功）", PUTS[:3] == [600000] * 3, str(PUTS))
    check("★ 第 2 片 1 次成功，两片字节数正好等于文件大小",
          len(PUTS) == 4 and PUTS[2] + PUTS[3] == len(data), str(PUTS))

    print("\n[4] 分片 PUT 一直失败 ⇒ 优雅放手（返回 None，绝不抛）")
    http = HTTP()
    PUT_SCRIPT = ["boom"] * 20
    PUTS.clear()
    log = _Log()
    try:
        out = await M._upload_bytes_to_qq(_client_with(http), "G1", True, data, "x.png", logger=log)
        raised = None
    except Exception as exc:
        out, raised = None, exc
    check("★ 没有抛异常（调用方还能原样发送）", raised is None, repr(raised))
    check("★ 返回 None（图片退化为原地址）", out is None)
    check("★ PUT 只试了 3 次就放弃（不是无限重试）", len(PUTS) == 3, str(PUTS))

    print("\n[5] upload_part_finish 命中 40093001 ⇒ 进持久重试并成功")
    http = HTTP(faults={"upload_part_finish": ["400, {'code': 40093001}"] * 2})
    PUT_SCRIPT = []
    out = await M._upload_bytes_to_qq(_client_with(http), "G1", True, data, "x.png", logger=_Log())
    check("★ 最终成功", isinstance(out, str), repr(out))
    check("★★ part_finish 被重试（首片 3 次：2 失败 + 1 成功；第 2 片 1 次 ⇒ 共 4）",
          http.count("upload_part_finish") == 4, str(http.calls))

    print("\n[6] part_finish 的非可重试错误 ⇒ 立刻放手")
    http = HTTP(faults={"upload_part_finish": ["400, {'code': 850019, 'message': '富媒体文件格式不支持'}"] * 5})
    PUT_SCRIPT = []
    out = await M._upload_bytes_to_qq(_client_with(http), "G1", True, data, "x.png", logger=_Log())
    check("★ 返回 None（不硬撑）", out is None)
    check("★★ 只调用 1 次（参数类错误重试没意义）",
          http.count("upload_part_finish") == 1, str(http.calls))

    print("\n[7] 合并（/files）失败一次 ⇒ 按官方 2s 基数重试后成功")
    http = HTTP(faults={"/files": ["Server Disconnected"]})
    PUT_SCRIPT = []
    out = await M._upload_bytes_to_qq(_client_with(http), "G1", True, data, "x.png", logger=_Log())
    check("★ 合并重试后成功", isinstance(out, str), repr(out))
    check("★ 合并被调用 2 次（1 失败 + 1 成功）", http.count("/files") == 2, str(http.calls))
    check("★ 合并退避基数是 2s（官方 COMPLETE_UPLOAD_RETRY_POLICY）",
          OFFICIAL["complete_base"] == 2.0, str(OFFICIAL["complete_base"]))

    print("\n[8] 官方参数逐项核对（防止以后被改歪）")
    check("★ prepare/分片/part_finish 重试次数 = 2（官方 maxRetries=2）",
          OFFICIAL["retries"] == 2, str(OFFICIAL["retries"]))
    check("★ 基础退避 1s（官方 baseDelayMs=1000）", OFFICIAL["base"] == 1.0, str(OFFICIAL["base"]))
    check("★ part_finish 持久重试默认 120s / 上限 600s（官方值）",
          OFFICIAL["pf_timeout"] == 120.0 and OFFICIAL["pf_max"] == 600.0,
          f"{OFFICIAL['pf_timeout']}/{OFFICIAL['pf_max']}")
    check("★ 持久重试间隔 1s（官方 intervalMs=1000）", OFFICIAL["interval"] == 1.0)
    check("★ 单次 PUT 超时 300s（官方 PART_UPLOAD_TIMEOUT_MS）",
          OFFICIAL["put_timeout"] == 300.0, str(OFFICIAL["put_timeout"]))
    check("★ 软预算 35s < 图片总预算 45s（不拖死消息）",
          0 < OFFICIAL["budget"] < M._IMAGE_BUDGET_SECONDS,
          f"{OFFICIAL['budget']} vs {M._IMAGE_BUDGET_SECONDS}")
    check("★ 日额度码 = 40093002，分片可重试码 = 40093001",
          M._DAILY_LIMIT_CODE == "40093002" and M._PART_RETRYABLE_CODE == "40093001")

    print("\n[9] 反向验证：旧代码遇到这些失败会**直接放弃**（本测试能抓到回归）")
    http = HTTP()
    PUT_SCRIPT = ["500"]
    PUTS.clear()
    out = await M._upload_bytes_to_qq(_client_with(http), "G1", True, data, "x.png", logger=_Log())
    check("★★ 旧代码（PUT 失败即 return None）会失败；新代码成功 ⇒ 测试有效",
          isinstance(out, str), repr(out))

    print(f"\n结果：{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def _client_with(http):
    c = type("C", (), {})()
    c.api = type("A", (), {})()
    c.api._http = http
    return c


sys.exit(asyncio.run(main()))
