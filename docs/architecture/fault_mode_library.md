# 三级故障模式库 & 增强分析体系设计文档

## 概述

本文档描述 Stability Analysis Agent 的 8 项增强设计，参考华为 DFX Skills 最佳实践，
在现有 RAG 基础设施之上引入三级故障模式库、证据分级、调用栈分层等能力。

## 架构图

```
Crash Log
    ↓
[01 Parse] → parse_result
    ↓
[02 Symbolize] → resolved_stack
    ↓
[04a CrashDiagnosis]
    ├─ 寄存器/Maps/栈摘要诊断
    ├─ DeterministicAnalyzer → deterministic_facts
    └─ DisassemblyGate（按需）→ disassembly
    ↓
[03 CodeContext] → code_context
    ↓
[03.5 RAG 增强检索]
    ├─ ModuleKnowledgeRouter → 选择性知识加载
    ├─ FaultModeMatcher → 三级故障模式匹配
    ├─ EvidenceGrader → 证据分级评估
    ├─ StackLayerClassifier → 调用栈分层
    └─ 原有规则/向量检索
    ↓
[04 LLM] ← 注入 04a.prompt_section_zh（含已确认事实）+ 报告格式约束
```

## 1. 三级故障模式库

### 设计理念

根因只有一个，但通过三级分类逐层细化：

| 级别 | 回答的问题 | 示例 |
|------|-----------|------|
| L1 | 出了什么类型的故障？ | 空指针解引用 |
| L2 | 通过什么机制出的？ | 对象生命周期错误 |
| L3 | 具体是哪行代码/操作导致的？ | UAF，异步回调持有裸指针 |

### 数据存储

复用现有 `RuleStore`（SQLite crash_rules 表），通过 `conclusion_type = "fault_mode"` 区分：

```json
{
  "rule_id": "fm_nullptr_lifecycle_uaf",
  "conclusion_type": "fault_mode",
  "conclusion_payload": {
    "pattern": "null_deref_uaf",
    "root_cause_l1": "空指针解引用",
    "root_cause_l2": "对象生命周期错误",
    "root_cause_l3": "释放后使用（UAF），对象在回调执行前被析构",
    "evidence_tier": 3,
    "fix_direction": "使用weak_ptr/weak引用；析构时注销回调",
    "responsibility": "application"
  }
}
```

### 数据文件

`rag/seed_data/fault_mode_library.json` — 61 条规则，覆盖 9 个一级根因类别。

### 匹配引擎

`rag/fault_mode_matcher.py` — `FaultModeMatcher` 类，复用 `_evaluate_condition()` 逻辑。

---

## 2. 证据分级体系

### 五级证据等级（内部编号；面向开发者展示时只用中文描述）

| 内部等级 | 含义（展示文案） | 置信度 |
|----------|------------------|--------|
| 1 | 检测器明确报告（ASan/TSan/GWP-ASan） | 高 |
| 2 | 指令+寄存器+地址联合证据 | 高 |
| 3 | 多项栈特征一致 | 中 |
| 4 | 单一模块/函数特征 | 低 |
| 5 | 推测性结论（证据不足） | 低 |

LLM 报告与 prompt 注入文案中 **禁止** 输出 `Tier N` / `HIGH`/`MEDIUM`/`LOW`，应使用上表「含义」与「置信度」列的中文。

### 实现

`rag/evidence_grader.py` — `EvidenceGrader.grade()` 方法，返回 `EvidenceGrade` 含 tier + confidence_label + evidence_chain。

---

## 3. 调用栈分层

### 三层分类

| 层级 | 含义 | 作用 |
|------|------|------|
| 崩溃帧 | #00，触发信号位置 | 可能是 allocator/GC（延迟崩溃） |
| 首个非运行时 | 跳过 libc/abort/Runtime | 实际触发者 |
| 首个应用帧 | 确认属于应用产物 | 业务根因所在 |

### 实现

`rag/stack_layer_classifier.py` — 基于模块名正则 + 函数名正则分类。

---

## 4. 选择性知识加载

### 模块→知识域映射

`rag/seed_data/module_knowledge_mapping.json` — 14 条映射规则。

### 路由逻辑

`rag/module_knowledge_router.py` — 从调用栈提取 module 列表，匹配映射表，输出相关 knowledge_domain，
供向量检索时做 `where` 过滤。

---

## 5. 崩溃前日志/时序分析

### 支持格式

- Android logcat
- iOS syslog
- HarmonyOS HiLog
- 通用时间戳日志

### 实现

`tools/log_timeline_extractor.py` — `LogTimelineExtractor` + `BusinessFlowAnalyzer`。

**接线（crash 旁路）**：`tools/timeline_diagnosis/` → `04e_log_timeline.json`

- 输入：`01.raw_content` / 原始 crash log（不改 01 schema）
- 产出：时序条目 + 业务操作路径（lifecycle/点击/页面等）+ `prompt_section_zh`
- CLI：**默认仅当检测到 logcat / HiLog / ASI 等业务日志信号时**自动落盘；
  `--force-timeline-analysis` 强制尝试（含弱 `generic_timestamp`）
- 精简 tombstone / 纯栈 dump / 仅 Date-Time 行：`analyzed=false` 或不跑，不臆造业务路径
- **ANR 专用 workflow** 当前不接 04d/04e（仅主产 04c；mixed 可有辅轨 04a）

---

## 6. 报告输出标准化

### 固定章节

1. 故障基本信息（表格）
2. 三级根因定位（表格）
3. 证据链（带原始日志引用）
4. 置信度与证据等级
5. 责任归属
6. 修复建议
7. 需补充材料

### 实现

`prompts/report_schema.py` — `STRUCTURED_REPORT_INSTRUCTION` 模板，在 full 模式下追加到 LLM prompt。

---

## 7. ANR/Freeze/Leak 技能框架

### 当前状态

ANR/Freeze **MVP 已接线**：强分类 `log_kind` + 专用 workflow（非完整独立产品报告 schema）：

- `tools/crash_parser/log_kind_classifier.py` — `native_crash|java_crash|anr_trace|app_freeze|watchdog|mixed_anr_crash|unknown`，写入 `01.meta_info`
- CLI / daemon 按 `log_kind`（或 `--force-anr-analysis`）路由 `anr_freeze_analysis` vs `crash_analysis`
- `workflows/anr_freeze_workflow.py` — 主轨 ANR；`mixed_anr_crash` 时辅轨 `04a` crash 诊断
- `skill_system/skill_templates/anr_freeze_analysis.py` — 故障模式 + 技能元数据
- `tools/anr_diagnosis/` — 编排（优先 `log_kind`，兼容 `anr_suspected`）
- 工具：`stack_hotspot_analyzer`、`event_handler_analyzer`（含 Binder 链）
- 产物：`04c_anr_freeze_diagnosis.json`；ANR prompt 含 `prompt_section_zh`（不改 crash 的 `05` 装配）
- EventHandler：支持鸿蒙 AppFreeze `EventHandler dump`（`Current Running: start at …, Event { task name = … }`），
  用 dump `curTime` 估算当前任务已执行时长；Binder 无显式 wait 文本时仍可标注 OS_IPC 线程数

Memory leak / OOM **阶段 A 已接线**（crash 主轨旁路，非独立 workflow）：

- `tools/crash_parser/log_kind_classifier.py` — 增加 `oom_kill` / `memory_pressure` / `mixed_oom_crash`
- `tools/memory_diagnosis/` — 日志线索 + `LEAK_FAULT_MODES` 弱匹配 → `04d_memory_pressure_diagnosis.json`
- CLI `--force-memory-analysis`；普通 native crash 不误跑
- 完整 heap snapshot diff / 独立 `memory_leak_analysis` workflow 仍属后续阶段

技能模板：

- `skill_system/skill_templates/memory_leak_analysis.py`（`status=wired_sidepath`）

---

## 8. 确定性判断前移

### 确定性规则

| 条件 | 结论 | 确定性 |
|------|------|--------|
| SIGSEGV + fault_addr < 0x1000 | 空指针 | 100% |
| SIGABRT | 主动 abort | 100% |
| SIGFPE | 除零 | 100% |
| ASan/TSan 报告存在 | 引用检测器 | 100% |
| stack overflow + 递归帧 | 栈溢出 | 100% |

### 实现

`rag/deterministic_analyzer.py` — 在 04a 诊断内执行，结果写入
`04a_crash_diagnosis.json` 的 `deterministic_facts`，并进入 `prompt_section_zh`
的「已确认事实」小节（不再在 workflow LLM 路径旁路重复注入）。

### 可选反汇编

`tools/disassembly_tool.py` 经 `tools/crash_diagnosis/disassembly_gate.py` 在 04a 内按需调用：

- **硬前置**：`library_dir` 匹配到二进制 + 可用 PC/偏移 + objdump/otool
- **跳过**：已有高置信确定性空指针/ASan/abort，或栈符号已强指向空指针
- **软触发**：无可用源码行、寄存器存在但 fault 模式模糊、分类为 code_corruption/wild_pointer 等
- **强制**：problem 传入 `force_disassembly` / `enable_disassembly`

结果写入 `04a.disassembly`；仅 `triggered=true` 时注入 prompt「反汇编辅助」小节。

### 证据罗盘（PC → 符号 → 反汇编 → 寄存器）

`tools/crash_diagnosis/evidence_compass.py` 在 04a 内汇总各层完备度：

- 字段：`04a.evidence_compass`（`layers` / `missing_evidence` / `confidence_ceiling`）
- `data_availability` 扩展：`has_symbolized_function`、`has_source_file_line`、`has_disassembly`、`pc_vs_fault`
- prompt 按固定顺序呈现位置→符号→指令→数据，并要求 AI 引用证据、标明缺证
- 无 near-null fault 但符号含 nullptr 时：确定性事实以次级置信（约 85%）对齐分类，避免「分类有、facts 空」

---

## 数据导入

所有数据通过 `rag/init_vector_db_data.py` 的 `init_fault_modes()` 函数导入，
复用现有 `RuleStore.upsert_rule()` 接口。

```bash
python3 rag/init_vector_db_data.py
```

---

## 验证

```bash
python3 cli/main.py \
  --crash-log-file examples/crash_cases/demo_basic/logs/mac/NullPtr_SIGSEGV_2026-04-08_10-43-08.crash \
  --library-dir examples/crash_cases/demo_basic/lib/mac \
  --code-roots examples/crash_cases/demo_basic/code_dir \
  --scope gen_prompt_only
```

检查 `05_memory_context.json`（或旧编号 `04_memory_context.json`）中新增字段：
- `fault_mode_matches` — 三级根因匹配
- `evidence_grade` — 证据等级
- `stack_layers` — `StackLayerClassifier` 帧角色分层（崩溃帧 / 首个非运行时 / 首个应用帧）
- `knowledge_domains` — 知识域路由结果

说明：`01_crash_log_parser.json` 线程上的 **`stack_domains`**（如 `native` / `arkts`）表示运行时域，与上述 `stack_layers` 不是同一字段。
