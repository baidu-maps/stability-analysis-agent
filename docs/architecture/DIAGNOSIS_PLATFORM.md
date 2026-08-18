# 统一诊断基础设施

当前 Agent 各专项工具共享 `tools/diagnosis/` 公共层：

- `models.py`：`DiagnosisResult`、`EvidenceItem`、`KnowledgeEntry`
- `knowledge.py`：结构化知识条目注册表
- `external.py`：外部 analyzer 的平台、超时、stderr 和输出文件统一记录
- `project_context.py`：有界源码/API/配置文件发现
- `repair_gate.py`：根据诊断状态和置信度控制自动修复
- `report.py`：生成 `report_manifest.json`

旧专项结果仍保留原字段，并可通过 `normalize_diagnosis_result()` 转成统一外壳。统一修复门禁默认只允许 `confirmed` 且置信度不低于 0.85 的结果进入自动修复；`probable` 必须显式允许，`preliminary` 只输出补证建议。

每次 CLI 报告完成后会生成 `report_manifest.json`，索引 `01_*`、`04c_*`、`04f_*`、`04g_*`、`04h_*` 等 JSON sidecar，便于 daemon/Web/回归脚本机器回溯。

专项诊断的算法与故障模式大量参照华为开源 `developtools_dfx_skills`（cppcrash / appfreeze / jscrash / jsleak / apifault / jank），但落在本仓库的是可移植的确定性逻辑：特征提示、栈分层、Binder 图、系统噪声门禁、JS 三级故障模式、堆根类型和 API 错误码知识。HarmonyOS 专有二进制（trace analyzer、rawheap translator）仍通过 `external.py` 适配，不在本仓库内嵌。
