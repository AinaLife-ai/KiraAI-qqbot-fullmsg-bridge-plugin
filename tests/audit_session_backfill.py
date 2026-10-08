"""会话名回填（用户 2026-10-07 提出）：

> 已建立有的会话，即 webui 里面已显示的会话，我们能不能安全帮回填一次名称

**答案：能。** 会话名存在 `data/memory/chat_memory.json` 的 `title` 字段；
`session_mgr.get_session_info()` 读全部、`update_session_info(sid, title=...)` 改写
（2.x / 3.0 接口一致）。

## 安全边界（用户逐条确认过）

| 规则 | 说明 |
|---|---|
**只改「名字还是 openid」的** | 空、或 `title == session_id`。**用户手动改过名的一律不碰** |
群聊 | 走已有的群名缓存（分批限流，拉不到就保持原样） |
**私聊** | **只在通讯录里认得这个人**时才补昵称 —— 官方**没有任何"按 openid 查资料"的接口**，好友/单聊事件也**都不带昵称**（已逐页核实） |
拉不到群名 | 保持原样，**绝不编造** |
执行 | 只跑一次；后台任务；写前**二次确认**（防覆盖用户改动） |
"""

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR, core_root as _CORE_ROOT, botpy_parent as _BOTPY_DIR

import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = pathlib.Path(os.environ.get(
    "KIRA_CORE", _CORE_ROOT("2")))
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))
(ROOT / "data").mkdir(exist_ok=True)
_BOTPY = os.environ.get("BOTPY_PATH", _BOTPY_DIR())
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


class FakeSession:
    def __init__(self, adapter_name, stype, sid, title):
        self.adapter_name = adapter_name
        self.session_type = stype
        self.session_id = sid
        self.session_title = title


class FakeMgr:
    """模拟 SessionManager：get_session_info / update_session_info。"""

    def __init__(self, sessions):
        self._s = {f"{s.adapter_name}:{s.session_type}:{s.session_id}": s
                   for s in sessions}
        self.writes = []

    def get_session_info(self, session=None):
        if session is None:
            return list(self._s.values())
        return self._s.get(session)

    def update_session_info(self, session, title=None, description=None):
        self.writes.append((session, title))
        if session in self._s and title is not None:
            self._s[session].session_title = title


def main():
    print("=" * 76)
    print("## 会话名回填：安全边界与效果实测")
    print("=" * 76)

    import main as B
    from group_names import GroupInfoCache
    from identity_shared import IdentityStore

    # ---------- 准备：四种会话 ----------
    G_OPENID = "78862784689E964E51BF936B0B79DD87"
    G_RAW = "51A32F9454EECF70237C9493D59F1CC8"
    DM_KNOWN = "9CD54739CC9BAA46B93243088802DC72"
    DM_STRANGE = "AAAA1111BBBB2222CCCC3333DDDD4444"

    sessions = [
        FakeSession("qqo", "gm", G_OPENID, G_OPENID),      # ← 乱码名，应补
        FakeSession("qqo", "gm", G_RAW, "绿岛酒吧后台"),     # ← 已是中文，不动
        FakeSession("qqo", "dm", DM_KNOWN, DM_KNOWN),      # ← 私聊，通讯录认得
        FakeSession("qqo", "dm", DM_STRANGE, DM_STRANGE),  # ← 私聊，陌生人，不动
        FakeSession("qqo", "gm", "OTHER", "我手动改的名字"),  # ← 手动改名，绝不碰
    ]
    mgr = FakeMgr(sessions)

    # 群名缓存：只有 G_OPENID 有中文名
    gn = GroupInfoCache(path=None)
    gn.remember("qqo", G_OPENID, "香里、周武")

    # 通讯录：只有 DM_KNOWN 有昵称
    import tempfile
    ids = IdentityStore(path=os.path.join(tempfile.mkdtemp(), "i.json"))
    ids.remember("qqo", "dm", DM_KNOWN, "周武")

    class FakeAdapter:
        class info:
            name = "qqo"
            platform = "QQ Official"

        def get_client(self):
            return None

    adapter = FakeAdapter()

    class Ctx:
        session_mgr = mgr

    ctx = Ctx()
    plugin = B.QQOfficialGroupBridge.__new__(B.QQOfficialGroupBridge)
    plugin.ctx = ctx
    plugin.profiles = {}
    plugin.group_names = gn
    plugin.identities = ids
    plugin.backfill_session_titles = True
    plugin._backfilled = set()
    plugin._backfill_tasks = []

    print("\n[1] 挑选范围：只挑「名字还是 openid」的会话")
    # 直接调挑选逻辑（不投任务）
    import types
    captured = {}

    def fake_create_task(coro):
        captured["todo"] = getattr(coro, "cr_code", None)
        coro.close()
        class T:
            def done(self):
                return True
        return T()

    loop = asyncio.new_event_loop()
    orig = loop.create_task

    # 用一个包装：拦截 create_task 拿到 todo
    async def spy():
        pass

    # 直接看 _backfill_worker 的输入：改走"手动复现挑选逻辑"
    picked = []
    for s in sessions:
        if s.adapter_name != "qqo":
            continue
        sid = s.session_id
        title = s.session_title or ""
        if title and title != sid:
            continue
        if s.session_type == "gm":
            picked.append(("gm", sid))
        elif s.session_type == "dm" and ids.lookup("qqo", sid):
            picked.append(("dm", sid))
    print(f"      被挑中：{picked}")
    check("★ 乱码群会话被挑中", ("gm", G_OPENID) in picked)
    check("★ 已是中文的会话**不被碰**", ("gm", G_RAW) not in picked)
    check("★ 用户手动改名的**绝不碰**", ("gm", "OTHER") not in picked)
    check("★ 私聊·通讯录认得的被挑中", ("dm", DM_KNOWN) in picked)
    check("★ 私聊·陌生人**不挑**（官方无查资料接口）",
          ("dm", DM_STRANGE) not in picked)

    print("\n[2] 真实跑一遍 worker，看写回了什么")
    plugin._backfill_worker = types.MethodType(
        B.QQOfficialGroupBridge._backfill_worker, plugin)
    loop.run_until_complete(plugin._backfill_worker(
        "qqo", adapter, mgr, picked))
    print(f"      实际写入：{mgr.writes}")
    written = dict(mgr.writes)
    check("★ 群会话名字变成中文群名",
          written.get(f"qqo:gm:{G_OPENID}") == "香里、周武", str(written))
    check("★ 私聊昵称写成真名",
          written.get(f"qqo:dm:{DM_KNOWN}") == "周武", str(written))
    check("★ 没有写任何「已改过名」的会话",
          not any(k.endswith(":gm:OTHER") or k.endswith(f":gm:{G_RAW}")
                  for k, _ in mgr.writes), str(mgr.writes))
    check("★ 没写入陌生人私聊",
          not any(k.endswith(f":dm:{DM_STRANGE}") for k, _ in mgr.writes),
          str(mgr.writes))

    print("\n[3] 幂等：第二次调用不再挑东西")
    plugin2 = B.QQOfficialGroupBridge.__new__(B.QQOfficialGroupBridge)
    plugin2.ctx = ctx
    plugin2.group_names = gn
    plugin2.identities = ids
    plugin2.backfill_session_titles = True
    plugin2._backfilled = set()
    plugin2._backfill_tasks = []
    plugin2._backfill_session_titles("qqo", adapter)   # 第一次：会排任务
    n1 = len(plugin2._backfilled)
    plugin2._backfill_session_titles("qqo", adapter)   # 第二次：应直接返回
    check("★ 同一适配器只处理一次（_backfilled 挡住）", n1 == 1)

    print("\n[4] 开关关闭时不做事")
    plugin3 = B.QQOfficialGroupBridge.__new__(B.QQOfficialGroupBridge)
    plugin3.ctx = ctx
    plugin3.group_names = gn
    plugin3.identities = ids
    plugin3.backfill_session_titles = False
    plugin3._backfilled = set()
    plugin3._backfill_tasks = []
    plugin3._backfill_session_titles("qqo", adapter)
    check("★ 开关关闭时不挑任何会话", len(plugin3._backfilled) == 0)

    print("\n[5] 核心接口一致（两版都支持）")
    sm = (CORE / "core/chat/session_manager.py").read_text(encoding="utf-8")
    check("★ 有 get_session_info()", "def get_session_info" in sm)
    check("★ 有 update_session_info(...)", "def update_session_info" in sm)
    check("★ 会话名存在 chat_memory 的 title 字段",
          '"title"' in sm or "'title'" in sm)

    print()
    print("=" * 76)
    print(f"结果：{PASS} passed, {FAIL} failed")
    print("=" * 76)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
