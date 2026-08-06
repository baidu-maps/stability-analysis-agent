#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标准化报告输出 schema 与模板。

定义 LLM 分析输出的固定结构，确保报告质量一致。
"""

from __future__ import annotations
from typing import Optional


# 完整的结构化报告模板（在 full 模式下追加到 prompt 末尾）
STRUCTURED_REPORT_INSTRUCTION = """\
请严格按照以下固定结构输出分析报告（不要改变章节顺序或遗漏章节）：

## 1. 故障基本信息

| 字段 | 值 |
|------|---|
| 故障类型 | (如: 空指针解引用 / 释放后使用 / 死锁 / 栈溢出 等) |
| 信号/异常 | (如: SIGSEGV / SIGABRT / EXC_BAD_ACCESS 等) |
| 崩溃线程 | (线程名/TID, 是否主线程) |
| 平台 | (iOS / Android / macOS / Linux / HarmonyOS) |
| 崩溃模块 | (崩溃发生的 .so/.dylib/.framework 名) |

## 2. 三级根因定位

| 一级根因(大类) | 二级根因(机制) | 三级根因(具体原因) |
|----------------|----------------|---------------------|
| (如: 空指针解引用) | (如: 对象生命周期错误) | (如: 异步回调持有裸指针，owner已析构) |

## 3. 证据链

列出支撑根因判断的所有证据，每条引用原始日志片段：

1. **[证据类型]**: 证据描述
   > 原始日志片段引用

2. **[证据类型]**: ...

## 4. 置信度与证据等级

- **整体置信度**: 高 / 中 / 低（必须使用中文，禁止输出 HIGH/MEDIUM/LOW）
- **证据等级**: (直接用中文描述证据强弱与缺口，例如「指令与寄存器联合证据充分」或「证据链不完整，缺少……」；禁止输出 Tier 1/2/3 等编号前缀)
- **评估理由**: (为何给出此置信度)

## 5. 责任归属

- **责任方**: 应用代码 / 三方SDK / 系统框架
- **责任模块**: (具体 .so / .framework / 包名)
- **归属依据**: (通过什么证据判定责任方)

## 6. 修复建议

### 6.1 直接修复（代码级）
(具体代码修改建议)

### 6.2 防御性措施（架构级）
(长期改进建议)

## 7. 需补充材料

- [ ] 是否需要符号文件（当前是否已符号化）
- [ ] 是否需要完整源码
- [ ] 是否需要复现步骤/环境信息
- [ ] 是否需要其他线程的完整调用栈
"""


# 简化版报告要求（用于 gen_prompt_only 模式，不强制 LLM 格式）
REPORT_STRUCTURE_HINT = """\
分析输出建议包含以下要素：
- 三级根因（大类→机制→具体原因）
- 证据链（引用日志原文）
- 置信度（高 / 中 / 低，使用中文）
- 责任归属（应用/SDK/系统）
- 修复方向
"""


def get_report_instruction(mode: str = "full", deterministic_facts: str = "") -> str:
    """获取报告格式指令。

    Args:
        mode: "full" (LLM 分析模式) / "gen_prompt_only" (仅生成 prompt)
        deterministic_facts: 已确认事实段落（由 DeterministicAnalyzer 产出）

    Returns:
        追加到 prompt 末尾的格式指令文本
    """
    parts = []

    if deterministic_facts:
        parts.append(deterministic_facts)

    if mode == "full":
        parts.append(STRUCTURED_REPORT_INSTRUCTION)
    else:
        parts.append(REPORT_STRUCTURE_HINT)

    return "\n\n".join(parts)
