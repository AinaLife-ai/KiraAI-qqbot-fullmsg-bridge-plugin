"""测试入口（无需框架）：python3 tests/run_tests.py

    python3 tests/run_tests.py          # 全部套件

套件说明：
  test_version_bump.py 版本一致性（manifest ⇄ README 标题 ⇄ 最新变更小节）
  test_consistency.py  一致性 & 静态不变量（schema ⇄ 代码 ⇄ README）
  test_bridge.py       机制自测（解析表补丁 / 事件语义 / 去重 / 性能 / 可逆性），
                       不依赖 KiraAI；若本机有 qq-botpy 会自动跑真实库的对照断言。
  test_proactive_fallback.py  msg_id 过期（40034005）→ 主动消息兜底 + 清死 id +
                       主动通道 @ 走 markdown（最小 core 桩驱动真实 main.py）。
  smoke_real_core.py   端到端冒烟：真实 KiraAI core + 真实 qq-botpy +
                       真实 QQOfficialAdapter 全链路（没有任何桩）。
                       需要环境变量 KIRA_CORE / BOTPY_PATH 指向源码；找不到会自动跳过。
                       会**按世代**断言：2.x 必须接管事件，3.0 必须让位。
  smoke_v3.py          KiraAI 3.0 专项：世代探测 / 不顶替核心处理器 / 群名 /
                       引用唤醒补洞（含反向验证）/ markdown·键盘端到端 /
                       按钮回调 / 工具注入 / 可逆性。
  audit_quality.py     质量审计：性能 / 内存有界 / 不阻塞 / 可逆性 / 功能完整性。
  audit_static.py      静态审计：未使用导入 / 裸 await / TODO 残留 /
                       重复定义 / schema⇄代码⇄README 一致 / 无功能丢失清单。
  audit_edge.py        边界复审：核心重建 payload 时键盘是否丢 / 并发 contextvar 串味 /
                       脏数据（畸形群名、缺字段互动、超限键盘）/ 异常分类是否正确。
  audit_promises.py    承诺核对：文档与 PR 里说过的行为逐条对照代码，防"说了没做"。
  audit_e2e.py         端到端链路：@ 消息全链路 / 键盘闭环 / 群名 / 群管理工具 /
                       成员事件，从入口走到出口。
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent

SUITES = ["test_version_bump.py", "test_consistency.py", "test_bridge.py",
          "test_proactive_fallback.py", "smoke_real_core.py",
          "smoke_v3.py", "audit_quality.py", "audit_static.py",
          "audit_edge.py", "audit_promises.py", "audit_e2e.py"]

rc = 0
for suite in SUITES:
    print(f"\n########## {suite} ##########")
    r = subprocess.call([sys.executable, str(HERE / suite)])
    rc = rc or r

# audit_static 不依赖任何核心，任何环境都必须通过 —— 单独再确认一次
if rc:
    print("\n(至少一个套件失败，请查看上方输出)")
print("\n" + ("ALL TEST SUITES PASSED" if rc == 0 else "SOME TEST SUITES FAILED"))
sys.exit(rc)
