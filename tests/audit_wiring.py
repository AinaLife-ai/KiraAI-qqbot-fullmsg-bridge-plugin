"""★★ 接线/结构审计（固化成测试，防"文本替换插错位置"这类事故再次静默发生）。

2026-10-10 起因：我一次"收敛两份实现"的文本替换插错位置，把 `inject_tools_and_tags`
里的流式登记块替换掉了（`return False` 让后面整段跑不到），而当时的测试**全绿** ——
因为没有任何测试断言"谁该被谁调用"。本套件把这层结构关系钉死：

A. 类内/模块级**没有重复定义**（后定义的会静默覆盖前者）
B. 关键方法都在（改名/误删能被立刻发现）
C. `_attach` / `_tick` / `_watch_loop` / `inject_tools_and_tags` 里**该调的都在**
D. "输入中"两条路只剩一份实现（`_maybe_send_typing` 委托 `_typing_kick`）
E. 模块级函数没有被塞进类体（类体被截断过）
"""
import ast
import collections
import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _env import bridge_root as _BR  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


SRC = open(_os.path.join(str(_BR()), "main.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)


def _cls(name):
    return next(n for n in TREE.body
                if isinstance(n, ast.ClassDef) and n.name == name)


CLS = _cls("QQOfficialGroupBridge")
METHODS = {m.name: m for m in CLS.body
           if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
SEG = {k: ast.get_source_segment(SRC, v) or "" for k, v in METHODS.items()}

print("═══ A. 没有重复定义（重复定义会静默覆盖）═══")
_names = [m.name for m in CLS.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
_dups = [n for n, k in collections.Counter(_names).items() if k > 1]
check("★ 类内无重复方法", not _dups, str(_dups))
_mod = [n.name for n in TREE.body if isinstance(n, ast.FunctionDef)]
_dup_mod = [n for n, k in collections.Counter(_mod).items() if k > 1]
check("★ 模块级无重复函数", not _dup_mod, str(_dup_mod))

print("\n═══ B. 关键方法都在 ═══")
NEED = ["_attach", "_tick", "_watch_loop", "inject_tools_and_tags", "_attach_v3",
        "_patch_send_path", "_patch_text_content", "_ensure_media_layer",
        "_typing_kick", "_typing_skip", "_typing_cancel_pending", "_maybe_send_typing",
        "_send_typing", "_c2c_target_of", "_event_is_group", "_bare_id_from_session",
        "_dump_c2c_shape_once", "publish_synthetic_event", "_prefetch_group_names",
        "_backfill_session_titles", "_backfill_worker", "_find_adapters", "_capability_of"]
_missing = [n for n in NEED if n not in METHODS]
check("★★ 关键方法无缺失", not _missing, str(_missing))

# 模块级应有这几个（防止被塞进类体）
for fn in ("_split_media_and_rest", "_patch_notice_identity", "_find_kirai_plugin"):
    check(f"★ 模块级函数存在：{fn}",
          any(isinstance(n, ast.FunctionDef) and n.name == fn for n in TREE.body))

print("\n═══ C. 该调的都在（接线）═══")
_attach = SEG.get("_attach", "")
for x in ("install_http_guard", "_install_api_send", "_patch_send_entry", "_patch_send_path",
          "interactions.install", "_apply_extra_intents", "_prefetch_group_names",
          "_backfill_session_titles"):
    check(f"★ _attach 调了 {x}", x in _attach)

_tick = SEG.get("_tick", "")
for x in ("_sync_md_gif_mode", "_sync_ffmpeg_path", "_sync_voice_trim",
          "_sync_keyboard_enter", "_patch_notice_identity", "consume_title_dirty",
          "_prefetch_group_names", "_backfill_session_titles", "_attach("):
    check(f"★ _tick 调了 {x}", x in _tick)

_watch = SEG.get("_watch_loop", "")
for x in ("_tick()", "_flush_identities", "_flush_group_names", "_health_check_intents"):
    check(f"★ _watch_loop 调了 {x}", x in _watch)

_inj = SEG.get("inject_tools_and_tags", "")
for x in ("_maybe_send_typing(event)", "note_turn_start", "_register_c2c_turn", "_attach_v3"):
    check(f"★ inject_tools_and_tags 调了 {x}", x in _inj)
check("★★ inject_tools_and_tags 里**没有**误插的输入中解析块（它会 return 掉后面全部）",
      "认不出单聊目标" not in _inj)

print("\n═══ D. 「输入中」只剩一份实现 ═══")
_mb = SEG.get("_maybe_send_typing", "")
_kb = SEG.get("_typing_kick", "")
check("★★ _maybe_send_typing 委托 _typing_kick", 'source="llm"' in _mb and "_typing_kick" in _mb)
check("★★ _maybe_send_typing 自己不再持有门禁（frame_key / TYPING_DEBOUNCE）",
      "frame_key" not in _mb and "_TYPING_DEBOUNCE" not in _mb)
check("★ _typing_kick 里有防抖/延时/主动帧/帧数上限",
      all(x in _kb for x in ("typing_debounce_seconds", "typing_delay_seconds",
                             "typing_allow_proactive", "typing_max_frames")))

print("\n═══ E. 其它模块的接线 ═══")
_gg = open(_os.path.join(str(_BR()), "group_names.py"), encoding="utf-8").read()
check("★ 群名缓存有 title_dirty 通知（供巡检回填）",
      "_title_dirty" in _gg and "def consume_title_dirty" in _gg)
_ss = open(_os.path.join(str(_BR()), "v3_support.py"), encoding="utf-8").read()
check("★★ v3 摄入旁听里踢「输入中」", "_typing_early(message)" in _ss)
_ia = open(_os.path.join(str(_BR()), "interactions.py"), encoding="utf-8").read()
check("★ 互动事件：认 botpy 的 Interaction 对象", "botpy 的 Interaction 对象" in _ia)
_rc = open(_os.path.join(str(_BR()), "rich_content.py"), encoding="utf-8").read()
check("★ 键盘元素继承核心 Text（不再吐占位文本）", "_TEXT_BASE = Text" in _rc)
check("★ 提示词：优先回调按钮 + 正文与按钮同 <msg>",
      "优先用回调按钮" in _rc and "正文与按钮放进同一个 <msg>" in _rc)
_ap = open(_os.path.join(str(_BR()), "api_send.py"), encoding="utf-8").read()
check("★ 键盘不是 markdown 时自动升格", "自动升格成" in _ap or "升格" in _ap)

print(f"\n结果：{PASS} passed, {FAIL} failed")
_sys.exit(1 if FAIL else 0)
