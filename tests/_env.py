"""测试环境的**路径解析**（可移植，2026-10-08 加）。

## 为什么需要

原来每个套件里都写死了绝对路径：

    ROOT = "/var/minis/workspace/qqbot_bridge_review"
    sys.path.insert(0, "/var/minis/workspace/qqbot_bridge_review/kira-core")
    sys.path.insert(0, "/tmp/botpy_src/botpy-master")

而开发用的那个 workspace **会被系统清理**（另一台机器 / 换会话 / 清理后）
⇒ 整套测试直接 `ModuleNotFoundError: No module named 'main'` 全红，
和代码本身一点关系都没有。**测试必须能在任何机器上跑**，所以路径统一在这里解析：

* 插件根目录 = 本文件的上一级（**跟着仓库走**，不写死）；
* KiraAI 核心：优先 `$KIRA_CORE`，否则按世代找常见位置
  （`/var/minis/shared/kira30` = 3.0，`/var/minis/shared/kira_fw` = 2.x）；
* qq-botpy：优先 `$BOTPY_PATH`，否则用**已安装的那个包**的上一级目录；
* 对照用的第三方插件（accelerator / xml_tag_fixer / …）：
  优先 `$KIRA_PEERS/<名字>`，找不到就返回 None（调用方**跳过**该节，别判失败）。

找不到就返回一个**必然不存在**的路径 —— 让调用方自己 skip，
而不是抛异常把整套测试带崩。
"""
from __future__ import annotations

import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
BRIDGE = HERE.parent                      # 插件仓库根（含 main.py）

_MISSING = pathlib.Path("/nonexistent/kira-core")


def _gen_of(path: pathlib.Path) -> str:
    """3.0 的结构标志：`core/adapter/src/qq_official/im.py`（2.x 没有这个文件）。"""
    try:
        return "3" if (path / "core/adapter/src/qq_official/im.py").exists() else "2"
    except Exception:
        return "?"


#: 各世代的常见落地位置（按顺序找）
_CANDIDATES = {
    "3": ["/var/minis/shared/kira30", "/var/minis/shared/kira_fw_v3",
          "/var/minis/workspace/kira30"],
    "2": ["/var/minis/shared/kira_fw", "/var/minis/shared/kira_v2",
          "/var/minis/workspace/kira_fw"],
}


def core_root(gen: str | None = None) -> pathlib.Path:
    """KiraAI 核心源码根。

    :param gen: ``"2"`` / ``"3"`` —— 只认该世代；``None`` = 优先用 ``$KIRA_CORE``。
    """
    env = os.environ.get("KIRA_CORE")
    if env:
        p = pathlib.Path(env)
        if (p / "core").exists() and (gen is None or _gen_of(p) == gen):
            return p
    order = [gen] if gen else ["3", "2"]
    for g in order:
        for c in _CANDIDATES.get(str(g), []):
            p = pathlib.Path(c)
            if (p / "core").exists():
                return p
    return _MISSING


def bridge_root() -> str:
    """插件仓库根（字符串，方便 `f"{ROOT}/x"` 这种老写法）。"""
    return str(BRIDGE)


def botpy_parent() -> str:
    """`qq-botpy` 的父目录（可以 `sys.path.insert` 的那种）。"""
    env = os.environ.get("BOTPY_PATH")
    if env and pathlib.Path(env).exists():
        return env
    try:
        import botpy  # type: ignore

        return str(pathlib.Path(botpy.__file__).resolve().parent.parent)
    except Exception:
        return "/nonexistent/botpy"


def peers() -> pathlib.Path:
    """对照用的第三方插件源码目录（不存在时调用方应跳过）。"""
    env = os.environ.get("KIRA_PEERS")
    for c in ([env] if env else []) + ["/var/minis/shared/peer_plugins",
                                       "/var/minis/shared", str(BRIDGE.parent)]:
        if c and (pathlib.Path(c) / "compat_accelerator").exists():
            return pathlib.Path(c)
    return _MISSING


def peer_plugin(name: str):
    """某个对照插件的 `main.py`；找不到返回 None。"""
    for base in (peers(),):
        p = base / name / "main.py"
        if p.exists():
            return p
    return None


def ref_dir() -> pathlib.Path:
    """同类插件参考仓库（gmp / gmv / qfm …）；不存在时调用方应跳过。"""
    env = os.environ.get("KIRA_REFS")
    for c in ([env] if env else []) + ["/var/minis/shared/ref_repos", "/tmp/ref_repos"]:
        if c and pathlib.Path(c).is_dir():
            return pathlib.Path(c)
    return _MISSING


def peers_dir() -> pathlib.Path:
    """S 版 / Z 版对照仓库（`sustained` / `zchat`）；不存在时跳过。"""
    env = os.environ.get("KIRA_PEERS2")
    for c in ([env] if env else []) + ["/var/minis/shared/peers2", "/tmp/peers2"]:
        if c and pathlib.Path(c).is_dir():
            return pathlib.Path(c)
    return _MISSING


def bootstrap() -> None:
    """把插件根与 botpy 加进 sys.path（老的测试习惯）。"""
    for p in (bridge_root(), botpy_parent()):
        if p and p not in sys.path:
            sys.path.insert(0, p)
