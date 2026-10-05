"""测试入口（无需框架）：python3 tests/run_tests.py

    python3 tests/run_tests.py          # 全部套件

套件说明：
  test_version_bump.py 版本一致性（manifest ⇄ README 标题 ⇄ 最新变更小节）
  test_consistency.py  一致性 & 静态不变量（schema ⇄ 代码 ⇄ README）
  test_bridge.py       机制自测（解析表补丁 / 事件语义 / 去重 / 性能 / 可逆性），
                       不依赖 KiraAI；若本机有 qq-botpy 会自动跑真实库的对照断言。
  smoke_real_core.py   端到端冒烟：真实 KiraAI core + 真实 qq-botpy +
                       真实 QQOfficialAdapter 全链路（没有任何桩）。
                       需要环境变量 KIRA_CORE / BOTPY_PATH 指向源码；找不到会自动跳过。
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

SUITES = ["test_version_bump.py", "test_consistency.py", "test_bridge.py", "smoke_real_core.py"]

rc = 0
for suite in SUITES:
    print(f"\n########## {suite} ##########")
    r = subprocess.call([sys.executable, str(HERE / suite)])
    rc = rc or r
print("\n" + ("ALL TEST SUITES PASSED" if rc == 0 else "SOME TEST SUITES FAILED"))
sys.exit(rc)
