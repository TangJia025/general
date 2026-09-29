"""昇腾 CI 失败取证与归因（三步法的第 2/4/5 步实现）。

与 npu_ci_failure_analysis.py 的分工：
  第 1 步（GitHub 侧真相） + 失败日志提取  → npu_ci_failure_analysis.py（已有）
  第 2 步（集群侧真相）                    → forensics.cluster_forensics
  第 4 步（历史问题定位归因）              → forensics.issue_knowledge
  第 5 步（根因 + 修复建议输出）           → forensics.report
  衔接两端的驱动入口                       → npu_ci_forensics.py

设计约束（来自真实取证的教训，勿绕过）：
  - 集群身份必须自检：runner 标签后缀**不是**可靠的集群判别依据（Liqo 会把虚拟节点 pod
    反射进共享 namespace，同一 a3-800i-*-cn12-001 标签能从 3 个不同 kubeconfig 看到）。
    拿错集群的证据去解释 CI 失败，比没有证据更糟。
  - 取证失败必须显式记「未取证」，不得用推测填充结论。
"""
