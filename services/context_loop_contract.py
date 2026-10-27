"""Unified context-loop contract: schema, parser, prompt sections, and assembler."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from services.agent_output_parser import parse_agent_decision
from services.agent_schema import (
    CONTEXT_PRIORITIES,
    CONTEXT_REQUEST_TYPES,
    AgentDecision,
    ContextRequest,
)

__all__ = [
    "CONTEXT_PRIORITIES",
    "CONTEXT_REQUEST_TYPES",
    "AgentDecision",
    "ContextRequest",
    "parse_agent_decision",
    "build_round0_must_provide_lines",
    "build_round0_output_format_lines",
    "build_repair_code_requirement_lines",
    "build_round_task_section",
    "build_investigation_state_section",
    "build_json_format_reminder",
    "inject_extra_code_context",
    "format_evidence_items_markdown",
    "assemble_loop_prompt",
    "prompt_has_json_contract",
]


def build_round0_must_provide_lines(*, agent_loop: str) -> List[str]:
    """Lines under '**必须提供**' for analysis mode."""
    lines: List[str] = []
    if agent_loop == "context_loop":
        lines.extend([
            "1. 先判断是否需要 Agent 继续补充上下文。",
            (
                "2. 如果还需要 Agent 自动补充上下文（`agent_can_fetch_more=true`）："
                "不要输出最终分析报告、修复方案或修复代码；"
                "只输出需要 Agent 补充什么内容，以及为什么需要这些内容。"
            ),
            (
                "   每个请求必须说明：请求类型、请求符号、为什么当前证据不足、"
                "补充该内容后希望验证什么假设、优先级。"
            ),
            (
                "3. 如果 Agent 不应再继续自动拉取上下文（`agent_can_fetch_more=false`），"
                "再输出完整崩溃分析报告：结论置信度、关键证据、相关代码分析、修复或排查建议。"
            ),
            (
                "   `agent_can_fetch_more=false` 仅表示停止 Agent 多轮补上下文，"
                "不代表证据已齐全；仍缺的信息应写入「人工补充证据」。"
            ),
            "4. 首轮应尽量一次性列出所有高价值补充请求（按优先级排序），避免逐轮试探式追加。",
        ])
    else:
        lines.extend([
            "1. **结论置信度**：高 / 中 / 低，并说明判断依据。",
            "2. **关键证据清单（使用分项序号）**：引用上文可见日志字段、线程栈帧、file:line 或代码语句。",
            "3. 崩溃直接原因与可能根因；不确定处必须标注为推断，并说明缺失证据。",
            "4. 相关函数/模块分析：说明为什么相关、是否需要修改、还需要验证什么。",
            "5. 修复建议：若证据充分，可给出最小修复方案；若证据不足，只给排查建议或人工确认点。",
            (
                "6. 如需给出代码，只输出有充分上下文且确需修改的函数；"
                "不要补写未给出的实现或编造源码片段中没有的 API。"
            ),
        ])
    return lines


def build_repair_code_requirement_lines() -> List[str]:
    """自动改码时，模型必须给出可落盘的完整函数。"""
    return [
        "### 修复代码（自动改码时必填）",
        "若末尾 JSON 的 `next_action.kind` 为 `propose_fix`，必须另起「修复代码」。",
        "`propose_fix` 只打开改码阶段。这一阶段只写入「修复代码」里的完整函数定义。",
        "只写 `propose_fix` 或只写修复步骤，不会写文件，也不会创建评审。",
        "每个要修改的、源码中已经存在的函数，各给一个完整定义（签名加完整函数体），放在独立代码围栏里。",
        "每个围栏前必须单独一行写源文件名，例如：### `BMKSignalHandler.m`；两个文件不要放进同一个围栏。",
        "围栏语言与源文件一致：`.m` / `.mm` 用 `objc`，`.c` 用 `c`，`.cpp` / `.cc` / `.h` 用 `cpp`。",
        "新增的文件级 `static` 变量和源码中尚不存在的 helper，写在同一围栏内、放在第一个被替换函数之前。",
        "函数名必须与源码中已有定义一致。可以把 `__attribute__((visibility(\"hidden\")))` 与原来的返回类型",
        "改成 `static void` 这类新签名；写入时会从旧声明头整段替换，不要在新函数前保留旧的属性行。",
        "若根因是信号上下文中的死锁或卡死，替换函数不得再调用分析点名的非 async-signal-safe 接口：",
        "挂起其他线程、ObjC 消息、malloc、NSThread、callStackSymbols、callStackReturnAddresses。",
        "崩溃 PC 从 ucontext 读取；地址范围在注册时算好；崩溃记录用预先打开的 fd 加 write。",
        "只删掉挂起调用、把上述调用留在信号 handler 里，不能当作最终修复。",
        "禁止用 `...`、`// ...`、`省略`、`其他逻辑保持不变` 代替函数体。修复建议里的步骤说明不会被写入文件。",
    ]


def build_round0_output_format_lines(
    *,
    agent_loop: str,
    apply_ai_fixes: bool = False,
) -> List[str]:
    """Lines under '## 输出格式' for context_loop mode."""
    if agent_loop != "context_loop":
        return []
    lines = [
        "### 情况 A：还需要 Agent 补充上下文时",
        "只输出以下内容，不要输出最终结论、修复方案或代码：",
        "",
        "### Agent 将回填的内容（按 type 选择请求）",
        (
            "- `function`：返回**函数定义处完整源码**（含签名与函数体）。"
            "模板类方法优先写全 `DemoList<TArgs>::Method`（如 `DemoList<DemoLayer*, DemoLayer*>::RemoveAll`）；"
            "也可写 `DemoList::RemoveAll`，Agent 会自动搜索模板实现。"
        ),
        (
            "- `field`：返回**所属类的成员声明**（优先头文件），请写 `ClassName::member`；"
            "若需查看**类型/容器本身的类结构**，可写 `field: TypeName`（如 `DemoList`），"
            "将返回 `class/struct` 声明块。"
            "若无声明则返回同类中的初始化语句；**不包含**调用链或并发读写路径。"
        ),
        "- `references`：返回符号在所属类相关文件中的**读写/引用位置**（文件:行 + 片段）。",
        "- `callers`：返回**调用方函数名 + 调用点片段**。",
        (
            "- `grep`：在 code_roots 内做文本/正则搜索（`symbol`=pattern；"
            "可选 `file`=path_glob 限定目录）。"
        ),
        (
            "- `read_file`：读取指定源码文件片段（`file` 必填；"
            "可选 `line_number`/`line_end` 指定行范围）。"
        ),
        (
            "- 若希望验证调用链、`RemoveAll()` 后是否仍被访问、原子读写细节，"
            "应使用 `references` 或 `function`，不要全部塞进 `field`。"
        ),
        "",
        "### 需要 Agent 补充的上下文",
        "- 当前还不能形成最终结论的原因：[简要说明缺少哪些关键证据]",
        "- 下一轮需要 Agent 补充的内容：",
        "  1. `[符号]`（function/class/field/references/callers/grep/read_file）：",
        "     - 原因：[为什么当前证据不足]",
        "     - 希望验证：[补充后要验证的根因假设]",
        "     - 优先级：[critical/high/normal/low]（禁止 medium）",
        "     - 请求类型说明：[该 type 将回填什么；若需多种证据请拆成多个请求]",
        "",
        "```json",
        "{",
        '  "agent_can_fetch_more": true,',
        '  "context_requests": [',
        "    {",
        '      "type": "function",',
        '      "symbol": "ClassName::FunctionName",',
        '      "expected_return_form": "function_source",',
        '      "reason": "说明为什么需要该上下文，以及补充后要验证什么假设",',
        '      "fulfillment_note": "可选：补充说明期望拿到什么形态的证据",',
        '      "priority": "high"',
        "    },",
        "    {",
        '      "type": "read_file",',
        '      "file": "include/util/DemoTempl.h",',
        '      "expected_return_form": "file_snippet",',
        '      "reason": "读取头文件中的模板实现（不要用 type=file）",',
        '      "priority": "high"',
        "    },",
        "    {",
        '      "type": "grep",',
        '      "symbol": "Resource::release",',
        '      "expected_return_form": "grep_matches",',
        '      "reason": "跨文件搜索释放路径",',
        '      "priority": "high"',
        "    },",
        "    {",
        '      "type": "read_file",',
        '      "file": "src/resource.cpp",',
        '      "line_number": 10,',
        '      "line_end": 80,',
        '      "expected_return_form": "file_snippet",',
        '      "reason": "读取候选文件片段",',
        '      "priority": "normal"',
        "    }",
        "  ]",
        "}",
        "",
        "`expected_return_form` 必须与 `type` 一致：",
        "`function`→`function_source`；`class`→`class_declaration`；`field`→`member_declaration`；",
        "`references`→`read_write_references`；`callers`→`caller_snippets`；",
        "`grep`→`grep_matches`；`read_file`→`file_snippet`。",
        "禁止：`type=file`（改用 `read_file`）、`priority=medium`（改用 `normal`）。",
        "```",
        "",
        "### 情况 B：不再需要 Agent 补充上下文时",
        "输出完整崩溃分析报告，并在末尾输出 `agent_can_fetch_more=false` JSON。",
        "证据充分且要自动改码时，JSON 增加 "
        '`"next_action": {"kind": "propose_fix"}`。',
        "`propose_fix` 只打开改码阶段，不能代替完整函数。"
        "没有完整函数就不会写文件，也不会创建评审。",
    ]
    if apply_ai_fixes:
        lines.append("")
        lines.extend(build_repair_code_requirement_lines())
    lines.append("")
    return lines


def _append_apply_fix_task_lines(lines: List[str]) -> None:
    """把自动改码的完整函数要求放到本轮任务末尾。"""
    lines.append(
        "- 若输出最终分析且 `next_action.kind` 为 `propose_fix`，必须写「修复代码」。"
    )
    lines.append(
        "- 每个要改的已有函数各给一个完整定义，围栏与源文件一致"
        "（`.m` 用 `objc`，`.c` 用 `c`，`.cpp` 用 `cpp`）。"
        "每个围栏前必须有文件名标题，例如：### `BMKSignalHandler.m`。"
    )
    lines.append(
        "- 新增文件级 static 和源码中没有的 helper 放在同一围栏、第一个被替换函数之前。"
    )
    lines.append(
        "- 函数名必须是源码里已有的名字。"
        " `__attribute__((visibility(\"hidden\")))` 可以换成 `static void`，写入时从旧声明头整段替换。"
    )
    lines.append("- 只写修复步骤不会写文件，也不会创建评审。")
    lines.append(
        "- 信号上下文死锁不得保留 ObjC、malloc、挂起线程和 callStackSymbols。"
    )
    lines.append(
        "- 崩溃 PC 从 ucontext 读取，记录用预先打开的 fd 和 write；只删 suspend 不能当作最终修复。"
    )


def build_round_task_section(
    *,
    is_final_round: bool,
    early_final_reason: Optional[str] = None,
    apply_ai_fixes: bool = False,
) -> str:
    """Per-round task block injected before the crash analysis task section."""
    lines: List[str] = ["## 本轮任务"]
    if is_final_round:
        if early_final_reason == "all_requests_blocked":
            lines.append(
                "- 上一轮 `context_requests` 中的请求均已由 Agent 处理过（重复、拒绝或不可用），"
                "无法继续补充新上下文。"
            )
        elif early_final_reason == "invalid_schema":
            lines.append(
                "- 上一轮 `context_requests` 未产出任何**有效**请求（类型/优先级不合法或列表为空）。"
            )
            lines.append(
                "- 合法 `type` 仅限：`function` / `class` / `field` / `references` / `callers` / "
                "`grep` / `read_file` / `memory_pattern` / `verification_log` / `trace_snippet`。"
                "不要使用 `file`（请改用 `read_file`，路径放在 `file` 字段）。"
            )
            lines.append(
                "- 合法 `priority` 仅限：`low` / `normal` / `high` / `critical`（不要使用 `medium`）。"
            )
            lines.append("- 本轮必须直接输出最终分析；不得再请求 Agent 补充上下文。")
            lines.append(
                "- 若要自动改码，末尾 JSON 增加 "
                '`"next_action": {"kind": "propose_fix", "reason": "..."}`，'
                "并在「修复代码」给出完整函数定义。只写步骤不会写文件，也不会创建评审。"
            )
        else:
            lines.append("- 当前已经达到允许的最大多轮次数，本轮必须输出最终分析。")
        lines.append("- 不得再请求 Agent 补充上下文；请将仍缺失的信息列为人工补充证据或排查建议。")
        lines.append(
            "- 末尾必须输出 Agent 上下文获取结束 JSON："
            '`{"agent_can_fetch_more": false, "context_requests": []}`。'
        )
        lines.append(
            "- 说明：`agent_can_fetch_more=false` 表示 Agent 自动化不再拉取上下文，"
            "不等于所有证据已齐全；仍缺的信息应写入「人工补充证据」。"
        )
    else:
        lines.append("- 先判断是否仍需要 Agent 继续补充上下文。")
        lines.append(
            "- 若仍需补充：只输出需要 Agent 补充什么及原因；不要输出最终分析报告、修复方案或修复代码。"
        )
        lines.append(
            "- 若证据足够或剩余缺口只能人工补充：输出完整最终分析，并将 `agent_can_fetch_more` 置为 false。"
        )
        lines.append(
            "- 不得重复请求「其它代码上下文」中状态为已定位 / 未定位 / 此前已尝试未定位 / 已拒绝的 symbol。"
        )
        lines.append(
            "- 只有当「其它代码上下文」中的新增源码直接引出新的高价值函数、字段或引用时，"
            "才继续提出下一轮 `context_requests`。"
        )
    if apply_ai_fixes:
        _append_apply_fix_task_lines(lines)
    return "\n".join(lines)


def build_investigation_state_section(state: Optional[Dict[str, Any]] = None) -> str:
    """Render compact hypothesis/action state without embedding source text."""
    value = state if isinstance(state, dict) else {}
    hypotheses = value.get("hypotheses") if isinstance(value.get("hypotheses"), list) else []
    action = value.get("next_action") if isinstance(value.get("next_action"), dict) else {}
    claim = value.get("verification_claim") if isinstance(value.get("verification_claim"), dict) else {}
    capabilities = value.get("verification_capabilities") if isinstance(value.get("verification_capabilities"), list) else []
    reproduction = value.get("reproduction_plan") if isinstance(value.get("reproduction_plan"), dict) else {}
    plan = value.get("investigation_plan") if isinstance(value.get("investigation_plan"), list) else []
    lines = ["## 根因调查状态"]
    if hypotheses:
        lines.append("当前假设（仅作为待验证状态，不是确定事实）：")
        for item in hypotheses[:5]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- `{item.get('id')}` [{item.get('status', 'open')}] "
                f"confidence={item.get('confidence', 0)}: {item.get('statement')}"
            )
    else:
        lines.append("当前尚未登记结构化根因假设；请在最终分析前明确根因与证据关系。")
    if action:
        lines.append(f"上轮建议动作: `{action.get('kind')}` {action.get('target') or ''}；{action.get('reason') or ''}")
    if claim:
        lines.append(f"验证声明: {claim.get('statement')}; 最低等级={claim.get('minimum_level', 'L1')}")
    if capabilities:
        lines.append("可用验证能力（仅可选择已声明 check_id，不得生成命令）：")
        for item in capabilities[:8]:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('check_id')}`: {item.get('description') or item.get('kind')}; {item.get('verification_level', 'L1')}")
    if reproduction:
        lines.append(f"已选择复现计划: `{reproduction.get('check_id')}` purpose={reproduction.get('purpose')}")
    if plan:
        lines.append("受控调查候选（需通过正常 context request 协议执行）：")
        for item in plan[:6]:
            if isinstance(item, dict):
                lines.append(f"- `{item.get('kind')}` {item.get('target') or ''} [{item.get('priority', 'normal')}]；{item.get('reason') or ''}")
    lines.append("可选地在 JSON 中返回 `hypotheses` 和 `next_action`，系统会将其纳入跨轮调查状态。")
    return "\n".join(lines)


def build_json_format_reminder(
    *,
    is_final_round: bool = False,
    apply_ai_fixes: bool = False,
) -> str:
    """Compact JSON contract reminder appended to every loop-round prompt."""
    if is_final_round:
        text = (
            "## 输出契约（续轮提醒）\n"
            "本轮为最终轮：末尾必须输出 "
            '`{"agent_can_fetch_more": false, "context_requests": []}`。'
        )
    else:
        text = (
            "## 输出契约（续轮提醒）\n"
            "若仍需 Agent 补充上下文，末尾输出 JSON（`agent_can_fetch_more` 必须为 boolean）：\n"
            "```json\n"
            '{"agent_can_fetch_more": true, "context_requests": [{"type": "function", '
            '"symbol": "Class::Method", "reason": "...", "priority": "high"}]}\n'
            "```\n"
            "若证据已足够，输出完整分析并在末尾输出 "
            '`{"agent_can_fetch_more": false, "context_requests": []}`。'
        )
    if apply_ai_fixes:
        text += (
            "\n若 `next_action.kind` 为 `propose_fix`，正文必须有「修复代码」："
            "每个待改的已有函数一个完整定义，围栏与源文件一致（`.m` 用 objc）。"
            "没有完整函数就不会写文件，也不会创建评审。"
            "信号上下文死锁不得保留 ObjC、malloc、挂起线程和 callStackSymbols；"
            "崩溃记录用预先打开的 fd 和 write。"
        )
    return text


def prompt_has_json_contract(prompt: str) -> bool:
    text = str(prompt or "")
    return "agent_can_fetch_more" in text and "context_requests" in text


def inject_extra_code_context(base_prompt: str, extra_context_markdown: str) -> str:
    text = str(base_prompt or "")
    extra = str(extra_context_markdown or "").strip()
    if not extra:
        return text
    section = f"## 其它代码上下文\n\n{extra}\n"
    for marker in (
        "### 崩溃行关键源码",
        "### 同文件旁路候选源码",
        "### 关键同步与共享成员",
        "### 写路径函数源码",
        "### 崩溃所属类骨架",
        "# 崩溃分析任务",
    ):
        pos = text.find(marker)
        if pos >= 0:
            return text[:pos].rstrip() + "\n\n" + section + "\n" + text[pos:].lstrip()
    return text.rstrip() + "\n\n" + section


def format_evidence_items_markdown(items: Sequence[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        title = str(item.get("file") or item.get("kind") or "evidence")
        block = "\n".join([
            f"#### 证据: {title}",
            f"- evidence_id: {item.get('evidence_id')}",
            f"- source: {item.get('source')}",
            content,
        ]).strip()
        if block:
            blocks.append(block)
    if not blocks:
        return ""
    return "\n\n".join(blocks)


def _append_section_before_markers(prompt: str, section: str, markers: Sequence[str]) -> str:
    task_block = str(section or "").strip()
    if not task_block:
        return prompt
    for marker in markers:
        pos = prompt.find(marker)
        if pos >= 0:
            return prompt[:pos].rstrip() + "\n\n" + task_block + "\n\n" + prompt[pos:].lstrip()
    return prompt.rstrip() + "\n\n" + task_block


def assemble_loop_prompt(
    base_prompt: str,
    *,
    evidence_package: Optional[Dict[str, Any]] = None,
    is_final_round: bool = False,
    early_final_reason: Optional[str] = None,
    include_json_reminder: bool = True,
    investigation_state: Optional[Dict[str, Any]] = None,
    apply_ai_fixes: bool = False,
) -> Dict[str, Any]:
    """Assemble a context-loop follow-up prompt from base prompt + evidence + task + reminder."""
    extra_md = ""
    if isinstance(evidence_package, dict):
        extra_md = format_evidence_items_markdown(evidence_package.get("items") or [])
    body = inject_extra_code_context(base_prompt, extra_md)
    body = _append_section_before_markers(
        body,
        build_investigation_state_section(investigation_state),
        ("# 崩溃分析任务", "## 本轮任务"),
    )
    task_section = build_round_task_section(
        is_final_round=is_final_round,
        early_final_reason=early_final_reason,
        apply_ai_fixes=apply_ai_fixes,
    )
    body = _append_section_before_markers(body, task_section, ("# 崩溃分析任务", "## 本轮任务"))
    if include_json_reminder and not prompt_has_json_contract(body):
        reminder = build_json_format_reminder(
            is_final_round=is_final_round,
            apply_ai_fixes=apply_ai_fixes,
        )
        body = _append_section_before_markers(body, reminder, ("# 崩溃分析任务", "## 本轮任务", "## 输出契约"))
    elif include_json_reminder:
        reminder = build_json_format_reminder(
            is_final_round=is_final_round,
            apply_ai_fixes=apply_ai_fixes,
        )
        body = body.rstrip() + "\n\n" + reminder
    return {
        "content": body,
        "assembled_chars": len(body),
        "evidence_item_count": len(evidence_package.get("items") or []) if isinstance(evidence_package, dict) else 0,
    }
