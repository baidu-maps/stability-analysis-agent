# 发个大模型提示词的拼装逻辑

本文档说明：在崩溃分析流程中，**最终发给大模型（LLM）的完整提示词**是如何由多块内容按顺序拼装而成的，以及每块内容的来源与逻辑。

---

## 1. 概述

提示词采用**单一数据源、按需注入**的设计：

- **崩溃相关**：来自解析与代码上下文（`prompt_data`），固定参与拼装。
- **规则与经验模式**：来自向量数据库的规则命中 / 模式召回，渲染为「规则与经验模式参考」段落（`memory_context`）。
- **分析指导与输出要求**：来自向量库的「指导片段」表，或兜底从 `default_guidance_blocks.json` 加载，经占位符替换后得到 `guidance_text`。

无向量库或未命中任何规则/模式时，仍可通过默认 JSON 生成完整提示词，保证可用。

---

## 2. 拼装顺序与各段含义

最终提示词按以下顺序拼接（对应 `_build_ai_analysis_prompt`）：

| 顺序 | 段落 | 内容来源 | 说明 |
|------|------|----------|------|
| 1 | 开头一句 | 固定 | 「请基于以下崩溃分析信息，生成详细的代码修复建议。」 |
| 2 | **## 崩溃摘要** | `prompt_data["crash_summary"]` | 由 CodeContentProvider 产出，包含 file / function / line / stack_address / error_type / thread_id 等。 |
| 3 | **## 崩溃上下文** | `prompt_data["crash_contexts"]` | 多帧信息：每帧 address、resolved_function、resolved_file、resolved_line、crash_reason、thread_type。 |
| 4 | **## 代码上下文** | `prompt_data["code_contexts"]` | 每段：file_path、line_number、function_name、code_snippet、surrounding_lines、imports。 |
| 5 | **## 规则与经验模式参考**（可选） | `memory_context` | 由 `_render_memory_context` 生成，见下文第 4 节。 |
| 6 | 固定过渡句（可选） | 固定 | 仅当存在 memory_context 时追加：「**重要提示**: 请参考上述历史相似案例…」 |
| 7 | **## 分析指导** | `guidance_text` | 由 `_get_guidance_for_prompt` 得到，见下文第 3 节。 |
| 8 | 结尾一句 | 固定 | 「请基于以上信息，提供专业的崩溃分析和修复建议。」 |

即：**固定开头 + 崩溃摘要 + 崩溃上下文 + 代码上下文 + 规则与经验模式参考（可选）+ 分析指导 + 固定结尾**。

---

## 3. 分析指导（guidance_text）的获取逻辑

「分析指导」整段（含任务说明、分析步骤、多线程要点、单例 Release 说明、输出要求与格式、关键约束等）**不再**在 CodeContentProvider 里写死，而是由 **指导片段** 按需拼接而成。

### 3.1 调用链

1. 在组装提示词前，先执行 **`_collect_memory_context`**，得到本轮的 `rule_hits`、`pattern_hits` 等。
2. 从中抽出 `rule_ids`、`pattern_ids`，再调用 **`_get_guidance_for_prompt(rule_ids, pattern_ids, prompt_data)`** 得到 `guidance_text`。
3. **`_build_ai_analysis_prompt(prompt_data, memory_context, guidance_text)`** 中，将 `guidance_text` 填入「## 分析指导」下。

### 3.2 _get_guidance_for_prompt 内部逻辑

- **有向量库时**：调用 `vector_db_analyzer.get_guidance_blocks(rule_ids, pattern_ids)`，按规则/模式命中查询表 `analysis_guidance_blocks`，得到若干条指导片段（含兜底：`pattern_id`/`rule_id` 为空的通用片段）；按 `priority`、`block_type` 排序后，将每条 `content` 用 `\n\n` 拼接成一大段。
- **无向量库或查询结果为空**：从 **`default_guidance_blocks.json`** 加载默认片段列表（查找路径：仓库内 `configs/default_guidance_blocks.json` 或当前工作目录下 `configs/default_guidance_blocks.json`），同样按顺序拼接各条 `content`。
- **占位符替换**：对上述拼接后的整段文本做替换：
  - `{{crash_function_name}}` → 从 `prompt_data["crash_func"]["name"]` 或 `prompt_data["crash_summary"]["function"]` 取，缺省为「崩溃函数」。
  - `{{related_funcs_desc}}` → 从 `prompt_data["related_fun"]` 取前若干项函数名拼接（如「`funcA`、`funcB` 等」），无则为「同一类中的其他函数」。

得到的字符串即为 **guidance_text**，直接作为「## 分析指导」下的正文。

### 3.3 指导片段的数据来源小结

| 场景 | 来源 |
|------|------|
| 有向量库且命中规则/模式或存在兜底片段 | 表 `analysis_guidance_blocks`（`get_guidance_blocks`） |
| 无向量库或表中无可用片段 | 文件 `default_guidance_blocks.json` |

---

## 4. 规则与经验模式参考（memory_context）的构成

当启用向量库且存在规则命中或模式召回时，**`_render_memory_context`** 会生成「## 规则与经验模式参考」整段，即 **memory_context**。结构为（面向大模型阅读，默认不输出 score/风险/置信度 等元信息）：

1. **标题**：`## 规则与经验模式参考`
2. **规则命中**（若有）：`### 规则命中（确定性）`，以简短 bullet 列出 rule_name（可含结论要点）。
3. **经验模式召回**（若有）：`### 经验模式召回（向量）`，以简短 bullet 列出 pattern_summary / pattern_id（可含证据条数、语义签名）。
4. **修复策略候选**（若有）：`### 修复策略候选（非结论）`，以简短 bullet 列出 fix_intent。
5. **固定结尾**：`注意：规则与向量召回仅作为推理依据，不代表最终结论。`

规则与模式的来源：**`_collect_memory_context`** 内先根据 `parsed_data` / `resolved_data` / `prompt_data` 提取特征，再按配置做规则匹配（`match_rules`）；若无高置信规则命中，则用 `build_pattern_query` 生成查询文本，做向量检索（`retrieve_patterns`），并拉取证据与修复策略。最终用上述结果调用 `_render_memory_context` 得到字符串。

---

## 5. 两套入口的一致性

- **统一流程**（`ai_stability_agent.py`）：  
  `analyze_crash_with_ai` 中先做规则/向量上下文收集，再 `_get_guidance_for_prompt`，最后拼装完整提示词并调用 LLM。
- **LangGraph 流程**（`ai_stability_agent.py`）：  
  `_node_ai_analysis`（及另一处非图执行路径）中从 state 取 `rule_hits` / `pattern_hits`，同样通过 `_get_guidance_for_prompt` 得到 guidance_text，再调用 `_build_full_prompt(..., guidance_text, memory_context)`；`_build_full_prompt` 在结构上与上述顺序一致（先指导/上下文，再规则与经验模式，再输出格式等），只是把「分析指导」整段改为使用传入的 **guidance_text**。

因此，**无论走哪条入口，发个大模型的提示词都遵循同一套拼装逻辑**：崩溃数据 + 规则与经验模式参考 + 指导片段（向量库或默认 JSON）。

---

## 6. 相关代码位置

| 逻辑 | 文件 | 方法或位置 |
|------|------|------------|
| 拼装完整提示词（顺序/图模式） | `agent/ai_stability_agent.py` | `_build_full_prompt`、`_get_guidance_for_prompt` |
| 规则/向量召回与 memory_context | 同上 | `_render_memory_context` |
| 指导片段表与 API | `tools/core/rag/metadata_store.py`、`vector_database_integration.py` | `analysis_guidance_blocks`、`get_guidance_blocks`、`add_guidance_block` |
| 默认指导片段 JSON | `configs/default_guidance_blocks.json` | 兜底内容 |

CLI 将上述最终提示词通过 `TOOL_OUTPUT:ai_prompt:` 或 `TOOL_OUTPUT:ai_analysis:` 输出，并写入报告目录下按轮次子目录 **round_N/06_ai_prompt.md**（单轮为 round_0，多轮为 round_0、round_1、…；旧报告可能仍为 `05_ai_prompt.md`）。详见 [CLI 报告说明](../cli/CLI_COMMANDS_REFERENCE.md)。
