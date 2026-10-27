"""AI Fix 服务：从 AI 分析结果生成修复计划并应用到源码。

主要组件：
- CodeFixer: 主 facade，提供 generate_and_apply / apply_fix_plan
- FixResult: 结果数据类
- 模块级公共函数：extract_candidate_nodes, graph_auto_fix_allowed, signatures_match 等
"""

from __future__ import annotations

import json
import hashlib
import difflib
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# FixResult 数据类
# =============================================================================


@dataclass
class FixResult:
    """AI Fix 结果。"""

    success: bool = False
    applied: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    fix_plan: Optional[Dict[str, Any]] = None
    skipped_reason: Optional[str] = None
    summary: str = ""
    model_response_preview: str = ""
    rolled_back_files: List[str] = field(default_factory=list)
    missing_required: List[Dict[str, str]] = field(default_factory=list)
    schema_violations: List[Dict[str, Any]] = field(default_factory=list)
    edit_feedback: List[Dict[str, Any]] = field(default_factory=list)
    edits: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "success": self.success,
            "applied": self.applied,
        }
        if self.error:
            d["error"] = self.error
        if self.summary:
            d["summary"] = self.summary
        if self.skipped_reason:
            d["skipped_reason"] = self.skipped_reason
        if self.model_response_preview:
            d["model_response_preview"] = self.model_response_preview
        if self.rolled_back_files:
            d["rolled_back_files"] = list(self.rolled_back_files)
        if self.missing_required:
            d["missing_required"] = list(self.missing_required)
        if self.schema_violations:
            d["schema_violations"] = list(self.schema_violations)
        if self.fix_plan is not None:
            d["fix_plan"] = dict(self.fix_plan)
        if self.edit_feedback:
            d["edit_feedback"] = list(self.edit_feedback)
        if self.edits:
            d["edits"] = list(self.edits)
        return d


# =============================================================================
# 纯工具函数（模块级）
# =============================================================================


def _extract_simple_function_name(signature: str) -> str:
    """从签名提取函数短名（忽略返回类型与模板参数中的嵌套括号）。"""
    s = _normalize_signature_whitespace(signature)
    if not s:
        return ""
    open_idx = _parameter_list_open_index(s)
    head = s[:open_idx].strip() if open_idx >= 0 else s
    head = _strip_template_args(head)
    if "::" in head:
        head = head.split("::")[-1].strip()
    parts = head.split()
    if parts:
        head = parts[-1].strip()
    head = head.lstrip("*&")
    m = re.search(r"([~]?[A-Za-z_]\w*)$", head)
    return m.group(1) if m else head


def _strip_template_args(text: str) -> str:
    out: List[str] = []
    depth = 0
    for ch in str(text or ""):
        if ch == "<":
            depth += 1
            continue
        if ch == ">" and depth > 0:
            depth -= 1
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def _normalize_signature_whitespace(signature: str) -> str:
    """把签名里的空格/Tab/换行折叠成单空格，便于跨格式比对。"""
    return re.sub(r"\s+", " ", str(signature or "").strip())


def _parameter_list_open_index(signature: str) -> int:
    """返回函数形参列表起始 ``(`` 的下标；跳过 ``<>`` 与嵌套括号内的 ``(``。"""
    angle = 0
    paren = 0
    for i, ch in enumerate(str(signature or "")):
        if ch == "<":
            angle += 1
            continue
        if ch == ">" and angle > 0:
            angle -= 1
            continue
        if angle > 0:
            continue
        if ch == "(":
            if paren == 0:
                return i
            paren += 1
            continue
        if ch == ")":
            paren = max(0, paren - 1)
    return -1


def _normalize_signature_key(signature: str) -> str:
    s = _normalize_signature_whitespace(signature)
    s = re.sub(r"<[^<>]*(?:<[^<>]*>[^<>]*)*>", "", s)
    return s.rstrip("{").rstrip()


def sanitize_function_signature(signature: str) -> str:
    """去掉 NAMESPACE 宏、孤立 '}' 等污染，保留可定位的函数签名。"""
    s = " ".join(str(signature or "").split()).strip()
    if not s or s == "unknown function":
        return s
    changed = True
    while changed and s:
        changed = False
        s2 = re.sub(
            r"^(?:[A-Z][A-Z0-9_]*_(?:BEGIN|END)\b\s*)+",
            "",
            s,
            flags=re.I,
        ).strip()
        if s2 != s:
            s = s2
            changed = True
        s2 = re.sub(r"^}\s*", "", s).strip()
        if s2 != s:
            s = s2
            changed = True
    m = re.search(
        r"(?:^|\s)((?:template\s*<[^>]+>\s*)?"
        r"(?:[\w:\*&<>,\s]+\s+)?"
        r"(?:[\w:\.]+\s*::\s*)?~?\w+\s*(?:<[^>]*>)?\s*\([^)]*\))",
        s,
    )
    if m:
        return " ".join(m.group(1).split())
    return s


def _signature_is_plausible_for_edit(signature: str) -> bool:
    clean = sanitize_function_signature(signature)
    if not clean or clean == "unknown function":
        return False
    if re.search(r"\bNAMESPACE_[A-Z0-9_]*_(BEGIN|END)\b", clean, flags=re.I):
        return False
    if clean.startswith("}"):
        return False
    return "(" in clean and ")" in clean


def sanitize_replacement_code(replacement_code: str, signature: str = "") -> str:
    """去掉 LLM/提取器误带的 leading '}' 或宏行，避免写入后出现孤立括号。"""
    s = str(replacement_code or "").strip()
    if not s:
        return s
    func_name = _extract_simple_function_name(signature)
    lines = s.splitlines()
    while lines:
        head = lines[0].strip()
        if not head:
            lines.pop(0)
            continue
        if head == "}":
            lines.pop(0)
            continue
        if re.match(r"^[A-Z][A-Z0-9_]*_(BEGIN|END)\b", head):
            lines.pop(0)
            continue
        if func_name and func_name in head and "(" in head:
            break
        if re.match(
            r"^(?:template\s*<[^>]+>\s*)?(?:[\w:\*&<>,\s]+\s+)?"
            r"(?:[\w:\.]+\s*::\s*)?~?\w+\s*(?:<[^>]*>)?\s*\(",
            head,
        ):
            break
        if head.startswith("}"):
            lines[0] = head.lstrip("}").lstrip()
            if not lines[0].strip():
                lines.pop(0)
            continue
        break
    return "\n".join(lines).strip()


def _extracted_block_matches_signature(block: str, signature: str) -> bool:
    """校验从源码提取的函数块：签名头（可多行）对应目标，且不夹带其它顶层成员定义。"""
    block = str(block or "").strip()
    signature = str(signature or "").strip()
    if not block or not signature:
        return False
    owner, func_name = _extract_owner_and_name(signature)
    if not func_name:
        return False
    header = _extract_block_signature(block)
    if not header:
        return False
    header_owner, header_name = _extract_owner_and_name(header)
    if header_name and header_name != func_name:
        return False
    if owner and header_owner:
        o_tail = owner.split("::")[-1]
        h_tail = header_owner.split("::")[-1]
        if owner != header_owner and o_tail != h_tail:
            return False
    elif owner:
        # 目标带 Class::，块签名因 NAMESPACE 宏 / 模板特化写成短名时，只要求函数名一致。
        if not header_name or header_name != func_name:
            return False
    elif not re.search(
        rf"(?:^|[\s:*&]){re.escape(func_name)}\s*(?:<[^>]*>)?\s*\(",
        header,
    ):
        return False
    if owner:
        owner_tail = owner.split("::")[-1]
        extra_defs = re.findall(
            rf"(?:^|\n)\s*(?:[\w:\<\>\*&~\s]+\s+)?(?:[\w:]*::)?{re.escape(owner_tail)}\s*::\s*"
            rf"(?!{re.escape(func_name)}\b)([~]?[A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(",
            block,
            flags=re.M,
        )
        if extra_defs:
            return False
    return True


def _extract_owner_and_name(signature: str) -> Tuple[str, str]:
    """从签名提取 (类名, 函数名)；定位真实形参列表，避免被返回类型/参数里的 ``std::`` / ``(`` 误导。"""
    text = _normalize_signature_whitespace(signature)
    if not text:
        return "", ""
    open_idx = _parameter_list_open_index(text)
    head = text[:open_idx].rstrip() if open_idx >= 0 else text
    head = _strip_template_args(head).rstrip()
    m_owner = re.search(
        r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)::\s*([~]?[A-Za-z_]\w*)\s*$",
        head,
    )
    if m_owner:
        return m_owner.group(1), m_owner.group(2)
    m_name = re.search(r"([~]?[A-Za-z_]\w*)\s*$", head)
    if m_name:
        name = m_name.group(1)
        if name not in {"if", "for", "while", "switch", "catch", "return"}:
            return "", name
    return "", _extract_simple_function_name(text)


def edit_allowed_by_prompt_complete_body(
    signature: str,
    code_context: Optional[Dict[str, Any]],
) -> Optional[str]:
    """提示词未给出完整函数体时拒绝自动改码。缺元数据则不拦截（兼容旧报告）。"""
    if not isinstance(code_context, dict):
        return None
    diagnostics = code_context.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return None
    meta = diagnostics.get("prompt_context_meta")
    if not isinstance(meta, dict) or "included_complete_signatures" not in meta:
        return None
    allowed = meta.get("included_complete_signatures")
    if not isinstance(allowed, list):
        return None
    allowed_sigs = [str(item).strip() for item in allowed if str(item).strip()]
    if not allowed_sigs:
        return "提示词中没有完整函数体，已拒绝自动改码"
    for item in allowed_sigs:
        if signatures_match(item, signature):
            return None
    return "目标函数未在提示词中给出完整函数体，已拒绝写入"


def signatures_match(candidate_sig: str, target_sig: str) -> bool:
    """改码时匹配候选函数：兼容模板/命名空间/签名空白差异。"""
    if not candidate_sig or not target_sig:
        return False
    oa, na = _extract_owner_and_name(candidate_sig)
    ob, nb = _extract_owner_and_name(target_sig)
    if na and nb and na != nb:
        return False
    if oa and ob:
        oa_tail = oa.split("::")[-1]
        ob_tail = ob.split("::")[-1]
        if oa != ob and oa_tail != ob_tail:
            return False
    a = _normalize_signature_key(candidate_sig)
    b = _normalize_signature_key(target_sig)
    if a == b or (len(a) > 8 and a in b) or (len(b) > 8 and b in a):
        return True
    ma = re.search(r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)::\s*([~]?[A-Za-z_]\w*)\s*[\(<]", candidate_sig)
    mb = re.search(r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)::\s*([~]?[A-Za-z_]\w*)\s*[\(<]", target_sig)
    if ma and mb:
        return ma.group(2) == mb.group(2) and (
            ma.group(1) == mb.group(1)
            or ma.group(1).endswith(mb.group(1).split("::")[-1])
            or mb.group(1).endswith(ma.group(1).split("::")[-1])
        )
    if oa and ob:
        return bool(na and nb and na == nb)
    sa = _extract_simple_function_name(candidate_sig)
    sb = _extract_simple_function_name(target_sig)
    return bool(sa and sb and sa == sb)


def _nullptr_guard_only_body(replacement_code: str) -> bool:
    """函数体去掉 this/nullptr 早返回后，是否只剩空返回或什么都没有。"""
    stripped = re.sub(
        r"if\s*\(\s*(?:this\s*[!=]=\s*(?:nullptr|NULL|0)|(?:nullptr|NULL|0)\s*[!=]=\s*this|!\s*this)\s*\)\s*\{[^}]*\}",
        "",
        str(replacement_code or ""),
    )
    body = re.sub(r"^[^{]*\{", "", stripped, count=1)
    body = body.rsplit("}", 1)[0] if "}" in body else body
    statements = [item.strip() for item in re.split(r"[;\n]", body) if item.strip() and not item.strip().startswith(("//", "/*", "*"))]
    if not statements:
        return True
    return all(re.match(r"return\s*(?:false|true|nullptr|NULL|0)?\s*$", item) for item in statements)


def is_forbidden_patch(signature: str, replacement_code: str, uaf_nullptr_guard_policy: str = "strict") -> Optional[str]:
    """拒绝无效/症状级补丁（通用模式，非业务硬编码）。"""
    code = str(replacement_code or "")
    if (
        re.search(r"\bthis\s*[!=]=\s*(nullptr|NULL|nullptr|0)\b", code)
        or re.search(r"\b(nullptr|NULL|nullptr|0)\s*[!=]=\s*this\b", code)
        or re.search(r"\bif\s*\(\s*!\s*this\s*\)", code)
    ):
        sig = str(signature or "")
        if re.search(r"\)\s*const\b", sig) or (
            "::" in sig and not sig.strip().startswith("static")
        ):
            policy = uaf_nullptr_guard_policy
            if policy in ("strict", "balanced") and _nullptr_guard_only_body(code):
                return "成员函数内 this/nullptr 防护不能作为 UAF 根因修复（policy={})".format(policy)
    live_copy_err = _forbidden_live_container_copy_patch(signature, code)
    if live_copy_err:
        return live_copy_err
    return None


def _forbidden_live_container_copy_patch(signature: str, replacement_code: str) -> Optional[str]:
    """拒绝「只给指针加锁、仍跨线程拷贝 live 容器」的假修复。

    通用模式（非业务符号硬编码）：
    - 签名像按值返回容器（vector / Array / List 等）；
    - 补丁内加了锁，但仍通过 ``ptr->Getter()`` 取返回值；
    - 且未改为读取显式 snapshot/cache 成员。
    """
    sig = str(signature or "")
    code = str(replacement_code or "")
    if not sig or not code:
        return None
    # 仅约束「按值返回容器」类入口。
    if not re.search(
        r"\b(std\s*::\s*vector|QVector|QList|Array|Vector|List)\s*<",
        sig,
        re.I,
    ):
        return None
    # 若已改为读显式 snapshot/cache 成员，且不再经指针调 getter 作为主返回源，则放行。
    reads_snapshot = bool(re.search(r"\bm_\w*(?:Snapshot|snapshot|Cache|cache)\w*\b", code))
    live_ptr_getter = re.search(
        r"\b([A-Za-z_]\w*)\s*->\s*([A-Za-z_]\w*)\s*\(",
        code,
    )
    if reads_snapshot and not live_ptr_getter:
        return None
    if reads_snapshot and live_ptr_getter:
        # snapshot 赋值存在时，允许其它无关指针调用；仅当赋值右侧仍是 ptr->getter 才拒绝。
        if not re.search(
            r"=\s*[A-Za-z_]\w*\s*->\s*[A-Za-z_]\w*\s*\(",
            code,
        ):
            return None
    # 无 snapshot，却加锁后仍经裸指针 getter 取容器。
    has_lock = bool(
        re.search(r"\b(Lock|lock|lock_guard|unique_lock|scoped_lock|std::lock)\b", code)
    )
    if not has_lock:
        return None
    if re.search(r"=\s*[A-Za-z_]\w*\s*->\s*[A-Za-z_]\w*\s*\(", code) or re.search(
        r"return\s+[A-Za-z_]\w*\s*->\s*[A-Za-z_]\w*\s*\(",
        code,
    ):
        return (
            "跨线程容器读入口仍通过裸指针调用 live getter 作为返回/赋值来源；"
            "应在锁下发布/读取快照，禁止只锁指针却继续拷贝 live 容器"
        )
    return None


def _source_newline_style(source: str) -> str:
    """根据文本中是否含 CRLF 判定换行风格。"""
    text = str(source or "")
    crlf = text.count("\r\n")
    # 单独的 \r 较少见；用占比避免偶发 \r\n 误判
    lf_only = text.count("\n") - crlf
    if crlf > 0 and crlf >= lf_only:
        return "\r\n"
    return "\n"


def _convert_newlines(text: str, style: str) -> str:
    """将文本统一为指定换行风格。"""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if style == "\r\n":
        return normalized.replace("\n", "\r\n")
    return normalized


def _read_text_raw(path: Path, *, encoding: str = "utf-8") -> str:
    """读取文本并保留原始换行（禁用 universal newlines）。"""
    with path.open("r", encoding=encoding, newline="") as handle:
        return handle.read()


def _write_text_raw(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """写入文本；newline='' 避免把 ``\\n`` 再转换成 os.linesep。"""
    with path.open("w", encoding=encoding, newline="") as handle:
        handle.write(text)


def _try_read_source_raw(path: Path) -> Tuple[str, str]:
    """读取源码，返回 ``(text, encoding)``；优先 utf-8，再 gbk。"""
    try:
        return _read_text_raw(path, encoding="utf-8"), "utf-8"
    except UnicodeDecodeError:
        return _read_text_raw(path, encoding="gbk"), "gbk"


def _detect_indent_unit(block: str) -> Tuple[str, int]:
    """从代码块推断缩进：``('tab', 4)`` 或 ``('space', width)``。"""
    tab_lines = 0
    space_widths: List[int] = []
    for line in str(block or "").splitlines():
        if not line.strip():
            continue
        if line.startswith("\t"):
            tab_lines += 1
            continue
        match = re.match(r"^( +)\S", line)
        if match:
            space_widths.append(len(match.group(1)))
    if tab_lines > 0 and tab_lines >= len(space_widths):
        return "tab", 4
    if space_widths:
        from functools import reduce
        from math import gcd

        width = reduce(gcd, space_widths)
        if width <= 0:
            width = 4
        elif width % 4 == 0:
            width = 4
        elif width % 2 == 0:
            width = 2
        else:
            width = 4
        return "space", width
    return "space", 4


def _count_indent_columns(leading_ws: str, *, tab_width: int = 4) -> int:
    """把前导空白折算成列宽（Tab 按 tab_width 计）。"""
    cols = 0
    width = max(1, int(tab_width or 4))
    for char in str(leading_ws or ""):
        if char == "\t":
            cols += width
        elif char == " ":
            cols += 1
        else:
            break
    return cols


def _adapt_replacement_indent(replacement: str, old_block: str) -> str:
    """将 LLM 补丁的前导缩进对齐到原函数块的 Tab/空格风格。"""
    old = str(old_block or "")
    text = str(replacement or "")
    if not text or not old:
        return text
    style, width = _detect_indent_unit(old)
    repl_style, repl_width = _detect_indent_unit(text)
    if style == repl_style and (style == "tab" or width == repl_width):
        return text
    measure_tab = repl_width if repl_style == "space" else 4
    out_lines: List[str] = []
    for line in text.splitlines():
        match = re.match(r"^([ \t]*)(.*)$", line)
        if not match:
            out_lines.append(line)
            continue
        leading, rest = match.group(1), match.group(2)
        if rest == "":
            out_lines.append("")
            continue
        cols = _count_indent_columns(leading, tab_width=measure_tab)
        if style == "tab":
            tw = measure_tab
            out_lines.append(("\t" * (cols // tw)) + (" " * (cols % tw)) + rest)
        else:
            out_lines.append((" " * cols) + rest)
    result = "\n".join(out_lines)
    if text.endswith("\n") or text.endswith("\r\n"):
        result += "\n"
    return result


def _ensure_newline_before_replacement(prefix: str, replacement: str) -> str:
    """避免 ``*/`` 后紧贴函数签名，把行尾注释与代码挤成一行。"""
    text = str(replacement or "")
    head = str(prefix or "")
    if not text or not head:
        return text
    trimmed = head.rstrip(" \t")
    if not trimmed.endswith("*/"):
        return text
    if trimmed.endswith("\n") or trimmed.endswith("\r"):
        return text
    stripped = text.lstrip(" \t")
    if stripped.startswith(("\n", "\r", "/*", "//")):
        return text
    return "\n" + stripped


def _align_leading_newline(old_block: str, replacement: str) -> str:
    """若 old_block 含前导换行而 replacement 没有，补上以免粘连上一行。"""
    old = str(old_block or "")
    repl = str(replacement or "")
    if not old or not repl:
        return repl
    if not (old.startswith("\n") or old.startswith("\r")):
        return repl
    if repl.startswith("\n") or repl.startswith("\r"):
        return repl
    if old.startswith("\r\n"):
        return "\r\n" + repl
    if old.startswith("\r"):
        return "\r" + repl
    return "\n" + repl


def _normalize_code_for_equivalence(text: str) -> str:
    """用于判定改码是否仅为空白/注释差异，避免误报"已修改"。"""
    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    # 去除单行注释和块注释，再去除所有空白；用于识别纯格式/注释变化。
    s = re.sub(r'/\*.*?\*/', '', s, flags=re.DOTALL)
    s = re.sub(r'//[^\n]*', '', s)
    return re.sub(r"\s+", "", s)


def _extract_current_function_block(
    file_path: Path,
    signature: str,
    node: Dict[str, Any],
    original_source: str,
    *,
    tool_executor: Optional[Any] = None,
) -> Optional[str]:
    """基于当前源码重新提取目标函数体，避免依赖过期 snippet。"""
    line_no = 1
    try:
        raw_line = node.get("snippet_start_line")
        if raw_line is not None:
            line_no = max(1, int(raw_line))
    except (TypeError, ValueError):
        line_no = 1
    try:
        from services.tool_invoke import invoke_tool

        out = invoke_tool(
            "snippet_extractor",
            {
                "file_path": str(file_path),
                "line_number": line_no,
                "function_name": _extract_simple_function_name(signature),
                "max_code_length": 0,
            },
            tool_executor=tool_executor,
        )
        snippet = out.get("snippet") if isinstance(out, dict) else None
        if bool(out.get("is_complete_function")) and isinstance(snippet, list) and snippet:
            block = "\n".join(str(x) for x in snippet).strip("\n")
            block = _convert_newlines(block, _source_newline_style(original_source))
            if (
                block
                and block in original_source
                and _extracted_block_matches_signature(block, signature)
            ):
                return block
    except Exception:
        pass
    _, block = CodeFixer.replace_function_block(original_source, signature, "")
    if block and _extracted_block_matches_signature(block, signature):
        return block
    return None


def _contains_placeholder_code(text: str) -> bool:
    """检测 replacement 是否含省略/截断占位（不得整函数落盘）。"""
    s = str(text or "")
    # 中文省略号和固定短语标记
    phrase_markers = (
        "\u2026",
        "其他代码保持不变",
        "前置代码",
        "后续代码",
        "其他图层插入代码",
        "其他清理代码",
        "其他析构逻辑",
        "[其他",
        "代码省略",
        "省略",
        "[snip]",
        "write-path body truncated",
        "body truncated",
    )
    if any(mark in s for mark in phrase_markers):
        return True
    # 单行注释里的显式截断：`// ...` / `// ... 处理xxx ...`
    for ln in s.splitlines():
        stripped = ln.strip()
        if re.match(r"//\s*\.\.\.", stripped):
            return True
        if re.match(r"/\*\s*\.\.\.", stripped):
            return True
    # "..." 在非字符串字面量中视为占位；注释中的「截断式省略」已在上面拦截，
    # 普通说明性注释（如 // see docs...）仍允许。
    for m in re.finditer(r"\.{3}", s):
        start = m.start()
        line_start = s.rfind("\n", 0, start)
        prefix = s[line_start + 1 : start] if line_start >= 0 else s[:start]
        # 在引号内（如 "触发空指针崩溃..."）→ 跳过
        in_string = prefix.count('"') % 2 == 1
        if in_string:
            continue
        # 在 // 单行注释内：仅当注释以省略开头时已在上方拦截；其余说明性 ... 放行
        dslash = prefix.rfind("//")
        if dslash >= 0:
            before_slash = prefix[:dslash]
            if before_slash.count('"') % 2 == 0:
                continue
        # 在 /* */ 块注释内 → 跳过（块注释截断已由 /* ... 行规则覆盖）
        last_open = prefix.rfind("/*")
        last_close = prefix.rfind("*/")
        if last_open >= 0 and last_open > last_close:
            continue
        return True
    return False


def _count_substantive_code_lines(text: str) -> int:
    """统计实质代码行（忽略空行与纯注释行）。"""
    count = 0
    in_block = False
    for ln in str(text or "").splitlines():
        stripped = ln.strip()
        if not stripped:
            continue
        if in_block:
            if "*/" in stripped:
                in_block = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block = True
            continue
        if stripped.startswith("//"):
            continue
        count += 1
    return count


def _check_destructive_body_shrink(old_block: str, replacement: str) -> Optional[str]:
    """拒绝相对原文大幅缩水的整函数替换（防压缩片段/省略落盘）。"""
    switch_err = _check_destructive_switch_shrink(old_block, replacement)
    if switch_err:
        return switch_err
    old_n = _count_substantive_code_lines(old_block)
    new_n = _count_substantive_code_lines(replacement)
    # 短函数允许加锁等小改；长函数若缩水过半则视为截断风险。
    if old_n >= 40 and new_n < max(12, int(old_n * 0.45)):
        return (
            f"replacement 相对原文大幅缩水（原 {old_n} 实质行，现 {new_n} 行），"
            "疑似截断/省略落盘，已拒绝"
        )
    return None


def _is_valid_replacement_code(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return False
    if _contains_placeholder_code(s):
        return False
    if s.startswith("}"):
        return False
    first = next((ln.strip() for ln in s.splitlines() if ln.strip()), "")
    if first == "}" or re.match(r"^[A-Z][A-Z0-9_]*_(BEGIN|END)\b", first):
        return False
    return s.count("{") > 0 and s.count("{") == s.count("}")


def _is_within_code_roots(path: Path, code_roots: List[str]) -> bool:
    """判断路径是否落在给定代码根目录内，符号链接按解析后的路径比较。"""
    try:
        candidate = path.resolve()
    except OSError:
        candidate = path
    for root in code_roots:
        try:
            candidate.relative_to(Path(root).expanduser().resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _match_feedback(source: str, expected: str, *, file_path: str = "") -> Dict[str, Any]:
    """Return deterministic repair feedback without attempting fuzzy mutation."""
    actual = str(source or "")
    target = str(expected or "")
    occurrences = actual.count(target) if target else 0
    expected_lines = [line.strip() for line in target.splitlines() if line.strip()]
    actual_lines = actual.splitlines()
    anchor = expected_lines[0] if expected_lines else ""
    candidates = [line.strip() for line in actual_lines if line.strip()]
    best = max((difflib.SequenceMatcher(None, anchor, line).ratio() for line in candidates), default=0.0)
    nearby: List[str] = []
    if anchor:
        for index, line in enumerate(actual_lines):
            if difflib.SequenceMatcher(None, anchor, line.strip()).ratio() >= max(0.55, best - 0.05):
                nearby.extend(actual_lines[max(0, index - 2): index + 3])
                if len(nearby) >= 8:
                    break
    return {
        "file": str(file_path or ""),
        "expected_preview": target[:1200],
        "actual_nearby": "\n".join(nearby)[:1600],
        "similarity": round(float(best), 4),
        "duplicate_matches": int(occurrences),
        "already_present": bool(target and target in actual),
        "exact_match": bool(target and occurrences == 1),
    }


def _collect_allowed_member_names(
    original_source: str,
    code_context: Optional[Dict[str, Any]] = None,
) -> Set[str]:
    """合并目标文件已有 m_* 与 owner 类声明中的成员名，作为改码白名单。"""
    allowed: Set[str] = set(re.findall(r"\bm_\w+", str(original_source or "")))
    try:
        from tools.owner_class_context import resolve_owner_class_from_code_context

        owner = resolve_owner_class_from_code_context(code_context) or {}
    except Exception:
        owner = {}
    for name in owner.get("member_fields") or []:
        text = str(name or "").strip()
        if text.startswith("m_"):
            allowed.add(text)
        elif re.fullmatch(r"[A-Za-z_]\w*", text):
            # 非 m_ 前缀成员（少见）也纳入，避免误伤
            allowed.add(text)
    for ln in owner.get("skeleton") or []:
        allowed.update(re.findall(r"\bm_\w+", str(ln or "")))
    for ln in owner.get("class_body_excerpt") or []:
        allowed.update(re.findall(r"\bm_\w+", str(ln or "")))
    # graph 上 class_skeleton 节点（resolve 失败时的兜底）
    if isinstance(code_context, dict):
        graph = code_context.get("graph")
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict) or node.get("type") != "class_skeleton":
                    continue
                for name in node.get("member_fields") or []:
                    text = str(name or "").strip()
                    if text.startswith("m_"):
                        allowed.add(text)
                allowed.update(re.findall(r"\bm_\w+", str(node.get("skeleton") or "")))
    return allowed


def _validate_and_fix_member_references(
    original_source: str,
    replacement: str,
    *,
    allowed_members: Optional[Set[str]] = None,
    allow_fuzzy_rewrite: bool = False,
) -> Tuple[str, Optional[str]]:
    """检查并修正 replacement 中的成员变量引用。

    返回 (修正后的 replacement, 错误描述或 None)。
    默认（落盘门禁）拒绝任何不在白名单中的 `m_*`，避免幻觉成员被模糊改写误放行。
    仅当 allow_fuzzy_rewrite=True 且近邻唯一时，才尝试自动改名。
    """
    known = set(allowed_members or ())
    known |= set(re.findall(r"\bm_\w+", str(original_source or "")))
    repl_members = set(re.findall(r"\bm_\w+", str(replacement or "")))
    novel = repl_members - known
    if not novel:
        return replacement, None
    if not allow_fuzzy_rewrite:
        return (
            replacement,
            "成员变量 {} 不在目标文件/类声明白名单中（可能是 LLM 幻觉），已跳过".format(
                ", ".join(sorted(novel))
            ),
        )
    fixed = replacement
    truly_novel: Set[str] = set()
    ambiguous: Set[str] = set()
    for m in sorted(novel):
        candidates: List[str] = []
        for om in known:
            if m in om or om in m:
                candidates.append(om)
                continue
            if abs(len(m) - len(om)) > 4:
                continue
            from difflib import SequenceMatcher

            ratio = SequenceMatcher(None, m, om).ratio()
            if ratio >= 0.7:
                candidates.append(om)
        uniq: List[str] = []
        seen_c: Set[str] = set()
        for c in candidates:
            if c in seen_c:
                continue
            seen_c.add(c)
            uniq.append(c)
        candidates = uniq
        if len(candidates) == 1:
            fixed = re.sub(rf"\b{re.escape(m)}\b", candidates[0], fixed)
        elif not candidates:
            truly_novel.add(m)
        else:
            ambiguous.add(m)
    if truly_novel or ambiguous:
        parts: List[str] = []
        if truly_novel:
            parts.append(
                "成员变量 {} 不在目标文件/类声明白名单中（可能是 LLM 幻觉）".format(
                    ", ".join(sorted(truly_novel))
                )
            )
        if ambiguous:
            parts.append(
                "成员变量 {} 无法唯一映射到已有成员（存在多个近邻）".format(
                    ", ".join(sorted(ambiguous))
                )
            )
        return replacement, "；".join(parts) + "，已跳过"
    return fixed, None


def _validate_sync_api_consistency(original_source: str, replacement: str) -> Optional[str]:
    """拒绝把既有 Lock/Unlock 成员误写成文件中未用于该成员的 ScopedLock，或臆造无对象的 Lock()。"""
    original = str(original_source or "")
    replacement = str(replacement or "")
    if "ScopedLock" in replacement:
        for guard_name in re.findall(r"\bScopedLock\s+(\w+)\s*\(", replacement):
            if re.search(rf"\b{re.escape(guard_name)}\s*\.\s*Unlock\s*\(", replacement):
                return (
                    f"区间锁对象 {guard_name} 在析构时解锁，"
                    f"replacement 调用了 {guard_name}.Unlock()，已跳过"
                )
        for member in re.findall(r"ScopedLock\s+\w+\s*\(\s*(m_\w+)\s*\)", replacement):
            uses_lock_unlock = bool(re.search(rf"\b{re.escape(member)}\s*\.\s*Lock\s*\(", original))
            if not uses_lock_unlock:
                continue
            scoped_on_member = bool(
                re.search(rf"ScopedLock\s+\w+\s*\(\s*{re.escape(member)}\s*\)", original)
            )
            if not scoped_on_member:
                return (
                    f"成员 {member} 在源文件中以 Lock/Unlock 使用，"
                    "replacement 却使用了未用于该成员的 ScopedLock 风格，已跳过"
                )
    # 禁止臆造无接收者的 Lock()/Unlock()（即便源文件尚无 m_xxx.Lock，也不得凭空写 Lock()）
    bare_lock = bool(re.search(r"(?<!\.)\bLock\s*\(\s*\)", replacement))
    bare_unlock = bool(re.search(r"(?<!\.)\bUnlock\s*\(\s*\)", replacement))
    if bare_lock and not re.search(r"(?<!\.)\bLock\s*\(\s*\)", original):
        return (
            "replacement 使用了无接收者的 Lock()；"
            "须使用已有成员对象的加锁 API（如 m_xxx.Lock(...)/Unlock()），已跳过"
        )
    if bare_unlock and not re.search(r"(?<!\.)\bUnlock\s*\(\s*\)", original):
        return (
            "replacement 使用了无接收者的 Unlock()；"
            "须使用已有成员对象的加锁 API（如 m_xxx.Lock(...)/Unlock()），已跳过"
        )
    return None


# 改码允许调用的通用宏/关键字（非类成员方法；成员方法须出现在原文件）
_ALLOWED_CALLEE_NAMES = frozenset(
    {
        "NAVI_CHECK_RESULT_VAL",
        "NAVI_FUNC_ENTERLEAVE_CHECK",
        "V_ASSERT",
        "nullptr",
        "memset",
        "memcpy",
        "NNew",
        "NDelete",
    }
)

_FORBIDDEN_METHOD_PATTERNS = (
    r"CancelAll\w*",
    r"cancelAll\w*",
    r"AbortAll\w*",
)
_FORBIDDEN_METHOD_REGEXES = [
    re.compile(p) for p in _FORBIDDEN_METHOD_PATTERNS
]


def _collect_callable_names_from_source(source: str) -> Set[str]:
    names: Set[str] = set(_ALLOWED_CALLEE_NAMES)
    for m in re.finditer(r"->\s*([A-Za-z_]\w*)\s*\(", source):
        names.add(m.group(1))
    for m in re.finditer(r"\.\s*([A-Za-z_]\w*)\s*\(", source):
        names.add(m.group(1))
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:const)?\s*(?:\{|:\s*\n)", source):
        names.add(m.group(1))
    for m in re.finditer(r"\b([A-Za-z_]\w*)::\s*([A-Za-z_]\w*)\s*\(", source):
        names.add(m.group(2))
    return names


def _forbidden_method_error(replacement: str) -> Optional[str]:
    if re.search(r"假设|需根据实际", replacement, re.I):
        return "replacement 含假设性 API 表述，已拒绝"
    for rx in _FORBIDDEN_METHOD_REGEXES:
        m = rx.search(replacement)
        if m:
            return f"replacement 调用了禁止的方法模式 {m.group(0).rstrip('(')}()"
    return None


def _declared_receiver_types(source: str) -> Dict[str, str]:
    """Collect receiver-to-type evidence from local/member C++ declarations."""
    declarations: Dict[str, str] = {}
    pattern = re.compile(
        r"\b((?:[A-Za-z_]\w*::)*[A-Za-z_]\w*(?:\s*<[^;{}]+>)?)"
        r"\s*(?:[*&]\s*)?([A-Za-z_]\w*)\s*(?:[;=,\[])"
    )
    for match in pattern.finditer(str(source or "")):
        declarations[match.group(2)] = re.sub(r"\s+", "", match.group(1))
    return declarations


def _receiver_method_calls(source: str) -> Dict[str, Set[str]]:
    """Collect direct ``receiver.Method()`` call evidence."""
    calls: Dict[str, Set[str]] = {}
    for match in re.finditer(
        r"\b([A-Za-z_]\w*)\s*(?:->|\.)\s*([A-Za-z_]\w*)\s*\(",
        str(source or ""),
    ):
        calls.setdefault(match.group(1), set()).add(match.group(2))
    return calls


def _validate_typed_receiver_calls(
    original: str,
    old_block: str,
    replacement: str,
) -> Optional[str]:
    """Reject novel methods only when the target receiver type is reliably known."""
    declarations = _declared_receiver_types(original)
    original_calls = _receiver_method_calls(original)
    old_calls = _receiver_method_calls(old_block)
    replacement_calls = _receiver_method_calls(replacement)
    for receiver, methods in replacement_calls.items():
        target_type = declarations.get(receiver, "")
        base_type = target_type.split("<", 1)[0].split("::")[-1]
        if base_type not in {"Array", "Vector", "List", "QVector", "QList", "vector"}:
            continue
        existing = original_calls.get(receiver, set())
        novel = sorted(methods - old_calls.get(receiver, set()) - existing)
        if novel:
            return (
                "replacement 为已解析类型 {} 的接收者 {} 新增了不存在的 API 调用：{}；"
                "该接收者现有 API 证据为 {}，已跳过"
            ).format(
                target_type,
                receiver,
                ", ".join(f"{name}()" for name in novel),
                ", ".join(f"{name}()" for name in sorted(existing)) or "无",
            )
    return None


def _validate_method_calls_in_replacement(
    original: str,
    replacement: str,
    *,
    old_block: str = "",
) -> Optional[str]:
    """拒绝禁止方法，以及可可靠绑定到目标类型的新增臆造 API。"""
    forbidden = _forbidden_method_error(replacement)
    if forbidden:
        return forbidden
    typed_error = _validate_typed_receiver_calls(original, old_block, replacement)
    if typed_error:
        return typed_error
    # 常见幻觉：在无 COM/智能指针证据时臆造 AddRef/Release/Retain
    invented_life = re.findall(
        r"(?:->|\.)\s*(AddRef|Release|Retain|retain|release)\s*\(",
        str(replacement or ""),
    )
    if invented_life:
        known = _collect_callable_names_from_source(str(original or ""))
        novel = sorted({name for name in invented_life if name not in known})
        if novel:
            return (
                "replacement 调用了源文件中未出现的生命周期 API（{}），"
                "禁止臆造 AddRef/Release/Retain；应复用已有锁/同步成员，已跳过"
            ).format(", ".join(novel))
    return None


def _check_destructive_switch_shrink(old_block: str, replacement: str) -> Optional[str]:
    """拒绝大幅删减 switch/case 的 replacement（防 Update 类整段删分支）。"""
    if "switch" in old_block and "switch" not in replacement:
        return "replacement 删除了 switch 控制结构，已拒绝"
    old_cases = len(re.findall(r"\bcase\s+", old_block))
    new_cases = len(re.findall(r"\bcase\s+", replacement))
    if old_cases >= 3 and new_cases < max(1, int(old_cases * 0.5)):
        return f"replacement 删减了过多 switch 分支（原 {old_cases} 处 case，现 {new_cases} 处）"
    return None


def _extract_entry_null_guard_var(replacement: str) -> Optional[str]:
    guard_match = re.search(
        r'\{[^\S\n]*\n'
        r'\s*if\s*\(\s*(\w+)\s*==\s*(?:nullptr|nullptr|NULL|0)\s*\)\s*\{[^}]*return[^}]*\}',
        replacement,
    )
    if not guard_match:
        guard_match = re.search(
            r'\{[^\S\n]*\n'
            r'\s*if\s*\(\s*!\s*(\w+)\s*\)\s*\{[^}]*return[^}]*\}',
            replacement,
        )
    return guard_match.group(1) if guard_match else None


def _stack_symbols_from_code_context(code_context: Optional[Dict[str, Any]]) -> List[str]:
    graph = (code_context or {}).get("graph") if isinstance(code_context, dict) else None
    if not isinstance(graph, dict):
        return []
    symbols: List[str] = []
    for item in graph.get("call_chain_from_add2line") or []:
        if not isinstance(item, dict):
            continue
        for s in item.get("call_order_from_add2line") or []:
            if isinstance(s, str) and s.strip():
                symbols.append(s.strip())
        if symbols:
            return symbols
    for item in graph.get("stack_function_symbols") or []:
        if not isinstance(item, dict):
            continue
        fn = str(item.get("function") or "").strip()
        if fn:
            symbols.append(fn)
    return symbols


def _check_null_guard_only_patch(
    old_block: str,
    replacement: str,
    *,
    code_context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """同名转发栈上，拒绝仅在崩溃点整函数入口添加判空早返回的补丁。"""
    from tools._stack_symbol_utils import stack_has_same_named_trampoline

    guarded_var = _extract_entry_null_guard_var(replacement)
    if not guarded_var:
        return None
    stripped = re.sub(
        r'(\{[^\S\n]*\n)'
        r'(\s*if\s*\(\s*!?\s*\w+\s*(?:==\s*(?:nullptr|nullptr|NULL|0)\s*)?\)\s*\{[^}]*return[^}]*\}\s*\n?)',
        r'\1',
        replacement,
        count=1,
    )
    if _normalize_code_for_equivalence(stripped) != _normalize_code_for_equivalence(old_block):
        return None
    has_trampoline = stack_has_same_named_trampoline(
        _stack_symbols_from_code_context(code_context)
    )
    if has_trampoline:
        return (
            "replacement_code 在崩溃点整函数入口判空早返回；"
            "调用栈存在同名转发调用方，应优先在调用方对齐线程投递，已跳过"
        )
    # 无同名转发时不拦截入口判空（含 this==nullptr / 原函数已有 var-> 等）。
    return None


def _find_containing_code_root(path: Path, code_roots: List[str]) -> Optional[Path]:
    """返回包含 path 的最具体 code_root（最长前缀匹配）。"""
    best: Optional[Path] = None
    for root in code_roots:
        root_path = Path(root).resolve()
        try:
            path.relative_to(root_path)
        except ValueError:
            continue
        if best is None or len(str(root_path)) > len(str(best)):
            best = root_path
    return best


# =============================================================================
# 候选提取与匹配（模块级）
# =============================================================================


def _extract_candidate_nodes_from_crash_summary(code_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """graph.nodes 为空时，尝试从 crash_summary.node_id 与磁盘源码回退一个候选函数。"""
    cs = code_context.get("crash_summary", {}) if isinstance(code_context, dict) else {}
    if not isinstance(cs, dict):
        return []
    node_id = str(cs.get("node_id") or "").strip()
    file_path = ""
    signature = ""
    if node_id.startswith("func|") and node_id.count("|") >= 2:
        _, file_path, signature = node_id.split("|", 2)
    else:
        file_path = str(cs.get("file") or "").strip()
        signature = str(cs.get("function") or "").strip()
    if not file_path or not signature:
        return []
    clean_sig = signature.rstrip("{").rstrip()
    try:
        line_no = int(cs.get("crash_line_number") or 0)
    except (TypeError, ValueError):
        line_no = 0
    path = Path(file_path)
    if not path.is_file() or line_no <= 0:
        return [{
            "file": str(path),
            "signature": clean_sig,
            "snippet": [clean_sig],
            "snippet_start_line": line_no or None,
            "snippet_end_line": line_no or None,
        }]
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    start = max(0, line_no - 1)
    def_idx = start
    for i in range(start, max(-1, start - 120), -1):
        ln = lines[i].strip()
        if not ln or ln.startswith("//"):
            continue
        if "(" in ln and not ln.startswith("if") and not ln.startswith("for"):
            def_idx = i
            break
    snippet: List[str] = []
    depth = 0
    started = False
    for i in range(def_idx, min(len(lines), def_idx + 400)):
        ln = lines[i]
        snippet.append(ln.rstrip())
        depth += ln.count("{") - ln.count("}")
        if "{" in ln:
            started = True
        if started and depth <= 0 and i > def_idx:
            break
    if len(snippet) < 2:
        return []
    return [
        {
            "file": str(path.resolve()),
            "signature": clean_sig,
            "snippet": snippet,
            "snippet_start_line": def_idx + 1,
            "snippet_end_line": def_idx + len(snippet),
        }
    ]


def extract_candidate_nodes(code_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 code_context 中提取可替换的候选函数节点列表。"""
    graph = code_context.get("graph", {}) if isinstance(code_context, dict) else {}
    nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    out: List[Dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        file_path = node.get("file")
        signature = node.get("signature")
        snippet = node.get("snippet")
        if not file_path or not signature or not isinstance(snippet, list) or not snippet:
            continue
        out.append(
            {
                "file": str(Path(file_path).resolve()),
                "signature": sanitize_function_signature(str(signature)),
                "snippet": [str(line) for line in snippet],
                "snippet_start_line": node.get("snippet_start_line"),
                "snippet_end_line": node.get("snippet_end_line"),
            }
        )
    if out:
        return out
    return _extract_candidate_nodes_from_crash_summary(code_context)


def graph_auto_fix_allowed(code_context: Dict[str, Any]) -> Tuple[bool, str]:
    """依据图证据摘要判断是否允许自动改码（通用规则）。"""
    graph = code_context.get("graph", {}) if isinstance(code_context, dict) else {}
    summ = graph.get("evidence_summary") if isinstance(graph, dict) else None
    if isinstance(summ, dict):
        if summ.get("auto_fix_allowed"):
            return True, ""
        return False, str(summ.get("auto_fix_block_reason") or "图证据不足，已禁止自动改码")
    # 未符号化的崩溃行没有调用边。分析点名、且正文来自已读源码时允许改码。
    if _graph_has_read_file_candidates(code_context):
        return True, ""
    edges = graph.get("edges", []) if isinstance(graph, dict) else []
    if not isinstance(edges, list):
        return False, "无图边信息，已禁止自动改码"
    has_direct = any(isinstance(e, dict) and e.get("type") == "calls_direct" for e in edges)
    has_to = any(isinstance(e, dict) and e.get("type") == "calls_to_crash_site" for e in edges)
    cf_norm = ""
    cs = code_context.get("crash_summary", {}) if isinstance(code_context, dict) else {}
    if isinstance(cs, dict):
        cf_norm = str(cs.get("node_id") or "").rstrip().rstrip("{").rstrip()
    proven: set = set()
    for e in edges:
        if not isinstance(e, dict):
            continue
        if str(e.get("type") or "") not in ("calls_direct", "calls_to_crash_site"):
            continue
        if cf_norm and str(e.get("to_id") or "").rstrip().rstrip("{").rstrip() == cf_norm:
            proven.add(str(e.get("from_id") or "").rstrip().rstrip("{").rstrip())
    has_sv_up = False
    for e in edges:
        if not isinstance(e, dict) or e.get("type") != "use_shared_var":
            continue
        rel = str(e.get("relation") or "").lower()
        if rel not in ("write", "assign", "delete"):
            continue
        fid = str(e.get("from_id") or "").rstrip().rstrip("{").rstrip()
        if fid and fid != cf_norm and fid in proven:
            has_sv_up = True
            break
    if has_direct or has_to or has_sv_up:
        return True, ""
    return False, "仅栈序关联或证据不足，已禁止自动改码"


def _append_unique_name(names: List[str], raw: str) -> None:
    """向名单追加去空白后的函数名（就地去重）。"""
    name = str(raw or "").strip()
    if name and name not in names:
        names.append(name)


def _extract_required_function_names_from_analysis(analysis_text: str) -> List[str]:
    """从「需要修改的函数」或非正式修复段落解析目标函数名。"""
    names: List[str] = []
    in_section = False
    for raw_line in (analysis_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "需要修改的函数" in line:
            in_section = True
            continue
        if in_section and line.startswith("#### "):
            break
        if not in_section:
            continue
        # 支持两种格式：
        # 1. `FuncName()` (backtick)
        m = re.match(r"^(?:[-*]|\d+[.)]\s*)\s*(?:\*{0,2})\s*`([^`]+)`", line)
        if m:
            _append_unique_name(names, m.group(1))
            continue
        # 2. **FuncName()** (bold markdown)
        m2 = re.match(r"^(?:[-*]|\d+[.)]\s*)\s*\*\*([^*]+)\*\*", line)
        if m2:
            # 提取函数名部分（去掉描述性文字如"CVTask 类声明（成员变量）"）
            name_candidate = m2.group(1).strip()
            # 必须包含 '(' 才是函数（排除描述性条目如"CVTask 类声明"）
            if "(" in name_candidate:
                # 取到 ')' 为止的部分作为函数名
                paren_end = name_candidate.find(")")
                if paren_end >= 0:
                    name_candidate = name_candidate[: paren_end + 1]
                _append_unique_name(names, name_candidate)
    if names:
        return names

    # 非正式输出（如「### 6.1 直接修复」）：从叙述与独立函数围栏补名单
    try:
        from services.toctou_write_completeness import _extract_fix_oriented_text

        fix_text = _extract_fix_oriented_text(analysis_text) or str(analysis_text or "")
    except Exception:
        fix_text = str(analysis_text or "")

    informal_pat = re.compile(
        r"修改\s*`?([A-Za-z_]\w*(?:::[~A-Za-z_]\w*)?)\s*\([^)`\n]*\)`?(?:\s*函数)?"
    )
    for m in informal_pat.finditer(fix_text):
        _append_unique_name(names, m.group(1) + "()")

    # 「修改析构函数」后紧跟的 ```cpp 围栏签名
    dtor_cue = re.compile(
        r"修改\s*(?:其)?析构函数[^\n]*\n+```(?:cpp|c\+\+|c|cc|cxx)?\s*\n"
        r"([^\n]*~[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)?\s*\([^)]*\))",
        re.IGNORECASE,
    )
    for m in dtor_cue.finditer(fix_text):
        _append_unique_name(names, m.group(1).strip())

    # 独立 Class::method / Class::~Class 完整函数围栏（跳过 class/struct 骨架）
    for block in _extract_code_blocks(fix_text):
        if _is_type_skeleton_block(block):
            continue
        for m in re.finditer(
            r"(?:^|\n)\s*(?:[\w:*&<>,\s]+?\s+)?"
            r"((?:~?\w+::)?[~]?\w+)\s*\([^;{]*\)\s*(?:const\s*)?\{",
            block,
        ):
            leaf = m.group(1)
            if leaf in ("if", "else", "for", "while", "switch", "do", "catch", "try"):
                continue
            if "::" not in leaf and not leaf.startswith("~"):
                continue
            _append_unique_name(names, leaf + "()")
        _append_c_function_names(names, block)
    return names


def _find_candidate_node_for_edit(
    candidate_nodes: List[Dict[str, Any]],
    file_path: Path,
    signature: str,
) -> Optional[Dict[str, Any]]:
    try:
        fp = str(file_path.resolve())
    except Exception:
        fp = str(file_path)
    for node in candidate_nodes:
        try:
            nf = str(Path(str(node.get("file", ""))).resolve())
        except Exception:
            nf = str(node.get("file", ""))
        if nf != fp:
            continue
        if signatures_match(str(node.get("signature", "")), signature):
            return node
    return None


def _build_candidate_node_from_current_source(
    file_path: Path,
    signature: str,
    *,
    tool_executor: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """当 graph.nodes 未包含目标函数时，直接从当前源码重提取一个临时候选节点。"""
    if not file_path.is_file() or not signature:
        return None
    try:
        from services.tool_invoke import invoke_tool

        out = invoke_tool(
            "snippet_extractor",
            {
                "file_path": str(file_path),
                "line_number": 1,
                "function_name": _extract_simple_function_name(signature),
                "max_code_length": 0,
            },
            tool_executor=tool_executor,
        )
        snippet = out.get("snippet") if isinstance(out, dict) else None
        if isinstance(snippet, list) and snippet:
            return {
                "type": "function",
                "file": str(file_path),
                "signature": signature,
                "snippet": [str(x) for x in snippet],
                "snippet_start_line": out.get("snippet_start_line"),
                "snippet_end_line": out.get("snippet_end_line"),
            }
    except Exception:
        pass
    # 无 registry / snippet 工具时，直接从源码定位函数块（单测与精简部署常用）。
    try:
        original, _enc = _try_read_source_raw(file_path)
    except Exception:
        return None
    _, block = CodeFixer.replace_function_block(original, signature, "")
    if not block or not _extracted_block_matches_signature(block, signature):
        return None
    lines = str(block).splitlines()
    idx = original.find(block)
    start_line = original.count("\n", 0, idx) + 1 if idx >= 0 else 1
    return {
        "type": "function",
        "file": str(file_path),
        "signature": signature,
        "snippet": lines,
        "snippet_start_line": start_line,
        "snippet_end_line": start_line + max(0, len(lines) - 1),
    }


def _select_required_targets(
    candidate_nodes: List[Dict[str, Any]],
    required_names: List[str],
) -> List[Dict[str, str]]:
    targets: List[Dict[str, str]] = []
    if not required_names:
        return targets
    for name in required_names:
        simple = _extract_simple_function_name(name)
        name_text = str(name or "").strip()
        scope_prefix = ""
        if "::" in name_text:
            scope_prefix = name_text.rsplit("::", 1)[0].strip()

        selected: Optional[Dict[str, Any]] = None
        # 第一优先级：限定名直接匹配（避免 CVTask::Cancel 误命中其它类的 Cancel）
        if scope_prefix:
            for node in candidate_nodes:
                sig = str(node.get("signature", ""))
                if not sig:
                    continue
                if name_text in sig or sig in name_text:
                    selected = node
                    break
            # 第二优先级：无作用域签名但同名（inline/声明场景通常无 Class:: 前缀）
            if selected is None and simple:
                for node in candidate_nodes:
                    sig = str(node.get("signature", ""))
                    sig_simple = _extract_simple_function_name(sig)
                    if "::" in sig:
                        continue
                    if sig_simple == simple:
                        selected = node
                        break
        # 最后兜底：原有简单名匹配
        if selected is None:
            for node in candidate_nodes:
                sig = str(node.get("signature", ""))
                sig_simple = _extract_simple_function_name(sig)
                if name_text in sig or sig in name_text or (simple and simple == sig_simple):
                    selected = node
                    break

        if selected is not None:
            targets.append(
                {
                    "file": str(selected.get("file", "")),
                    "function_signature": str(selected.get("signature", "")),
                }
            )
    dedup: List[Dict[str, str]] = []
    seen = set()
    for t in targets:
        key = (t["file"], t["function_signature"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(t)
    return dedup


def _ensure_owner_class_methods_in_targets(
    code_context: Dict[str, Any],
    required_names: List[str],
    required_targets: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """基于 owner 类上下文（graph class_skeleton 节点）补齐同类 inline 方法目标。"""
    if not required_names:
        return list(required_targets or [])
    from tools.owner_class_context import resolve_owner_class_from_code_context

    owner = resolve_owner_class_from_code_context(code_context) or {}
    if not isinstance(owner, dict):
        return list(required_targets or [])
    def_file = str(owner.get("definition_file") or "").strip()
    class_name = str(owner.get("class_name") or "").strip()
    excerpt = owner.get("class_body_excerpt")
    if not def_file or not isinstance(excerpt, list):
        return list(required_targets or [])

    owner_methods: Set[str] = set()
    for name in required_names:
        nm = str(name or "").strip()
        if "::" not in nm:
            continue
        owner_scope, _, method_tail = nm.rpartition("::")
        owner_simple = owner_scope.split("::")[-1].strip()
        if class_name and owner_simple and owner_simple == class_name:
            method_name = _extract_simple_function_name(method_tail)
            if method_name:
                owner_methods.add(method_name)

    out: List[Dict[str, str]] = []
    for target in required_targets or []:
        if not isinstance(target, dict):
            continue
        sig = str(target.get("function_signature", ""))
        simple = _extract_simple_function_name(sig)
        # owner class 的 inline 方法统一落到 definition_file，避免符号路径变体导致无法定位。
        if simple in owner_methods:
            # 如果此前按简单名误匹配到了其他类的同名函数（如 CVTask::Cancel
            # 命中 CBVMDOfflinePCDN::Cancel），丢弃该错误目标，后续从 owner
            # class excerpt 补回正确 inline 签名。
            if "::" in sig and class_name and f"{class_name}::" not in sig:
                continue
            out.append({"file": def_file, "function_signature": sig})
        else:
            out.append(dict(target))

    existing = {(str(x.get("file", "")), str(x.get("function_signature", ""))) for x in out if isinstance(x, dict)}
    existing_simple = {
        _extract_simple_function_name(str(x.get("function_signature", "")))
        for x in out
        if isinstance(x, dict)
    }
    meth_re = re.compile(
        r"^\s*(?:virtual\s+)?(?:[\w:*&<>,\s]+?\s+)?(~?\w+)\s*\(([^)]*)\)\s*(const)?"
    )
    for name in required_names:
        nm = str(name or "").strip()
        if not nm or "::" not in nm:
            continue
        owner_scope, _, method_tail = nm.rpartition("::")
        owner_simple = owner_scope.split("::")[-1].strip()
        if class_name and owner_simple and owner_simple != class_name:
            continue
        method_name = _extract_simple_function_name(method_tail)
        if not method_name or method_name in existing_simple:
            continue
        inferred_sig = f"{method_name}()"
        for ln in excerpt:
            line = str(ln or "").strip()
            if not line or method_name not in line or "(" not in line:
                continue
            m = meth_re.match(line)
            if not m:
                continue
            if m.group(1) != method_name:
                continue
            params = str(m.group(2) or "").strip()
            suffix = "const" if m.group(3) else ""
            inferred_sig = f"{method_name}({params}){suffix}"
            break
        key = (def_file, inferred_sig)
        if key in existing:
            continue
        out.append({"file": def_file, "function_signature": inferred_sig})
        existing.add(key)
        existing_simple.add(method_name)
    return out


def _ensure_crash_frame_in_targets(
    code_context: Dict[str, Any],
    candidate_nodes: List[Dict[str, Any]],
    required_targets: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """确保崩溃栈前 3 帧函数（如果有 candidate）强制进入 required_targets。"""
    graph = code_context.get("graph", {})
    symbols = graph.get("stack_function_symbols", [])
    # 取前 3 帧（按 render_order_pos 排序）
    top_frames = sorted(symbols, key=lambda x: x.get("render_order_pos", 999))[:3]
    existing_keys = {(t["file"], t["function_signature"]) for t in required_targets}
    result = list(required_targets)
    for frame in top_frames:
        func_name = frame.get("function", "")
        if not func_name:
            continue
        # 提取最短有效匹配键：去掉最外层命名空间，保留 Class::Method
        parts = func_name.split("::")
        # 尝试从最长后缀开始匹配（NS::Class::Method, Class::Method, Method）
        match_keys = []
        for i in range(len(parts)):
            match_keys.append("::".join(parts[i:]))
        for node in candidate_nodes:
            sig = str(node.get("signature", ""))
            matched = False
            for key in match_keys:
                if "::" in key:
                    # 限定名：直接子串匹配
                    if key in sig:
                        matched = True
                        break
                else:
                    # 纯方法名（如 "Update"）：只有在没有类名上下文时才用
                    # 如果有类名（parts >= 2），只用 Class::Method 匹配（已在上面处理）
                    if len(parts) >= 2:
                        continue
                    # 无类名时，要求精确的函数名匹配
                    sig_simple = _extract_simple_function_name(sig)
                    if key == sig_simple:
                        matched = True
                        break
            if matched:
                k = (str(node["file"]), sig)
                if k not in existing_keys:
                    result.append({"file": str(node["file"]), "function_signature": sig})
                    existing_keys.add(k)
                break
    return result


def _applied_replacement_text(item: Dict[str, Any]) -> str:
    """以实际落盘内容判定根因（优先 replaced_preview 中的新代码，避免 plan 与 apply 不一致）。"""
    preview = str(item.get("replaced_preview") or "").strip()
    if preview:
        return preview
    return str(item.get("replacement_code") or "")


def _check_replacement_scope_explosion(old_block: str, replacement: str) -> Optional[str]:
    """防止将单函数替换误写成整文件/超大块写入。"""
    old_lines = len(str(old_block or "").splitlines())
    new_lines = len(str(replacement or "").splitlines())
    if new_lines < 120:
        return None
    if old_lines > 0 and new_lines <= max(80, old_lines * 6):
        return None
    if old_lines == 0 and new_lines < 300:
        return None
    return (
        f"replacement 规模异常（原约 {old_lines} 行 → 新约 {new_lines} 行），"
        "疑似整文件替换，已跳过写入"
    )


def rollback_applied_edits(applied: List[Dict[str, Any]]) -> List[str]:
    """从 backup_path 恢复已 applied 的文件（用于根因门禁未通过时回滚）。"""
    restored: List[str] = []
    for item in applied:
        if not isinstance(item, dict) or item.get("status") != "applied":
            continue
        backup_path = str(item.get("backup_path") or "").strip()
        file_path = str(item.get("file") or "").strip()
        if not backup_path or not file_path:
            continue
        bp = Path(backup_path)
        fp = Path(file_path)
        if not bp.is_file() or not fp.is_file():
            continue
        for enc in ("utf-8", "gbk"):
            try:
                content = bp.read_text(encoding=enc)
                fp.write_text(content, encoding=enc)
                item["status"] = "rolled_back"
                item["rollback_from"] = str(bp.resolve())
                restored.append(str(fp.resolve()))
                break
            except (UnicodeDecodeError, LookupError, OSError):
                continue
    return restored


def evaluate_fix_apply_success(
    apply_result: FixResult,
    code_context: Dict[str, Any],
    fix_plan: Optional[Dict[str, Any]] = None,
) -> FixResult:
    """保留 API；改码成败仅由 apply_fix_plan 的 applied/skipped 决定（不再做根因 regex 门禁）。"""
    del code_context, fix_plan
    return apply_result


# =============================================================================
# Fix Plan 操控（模块级）
# =============================================================================


def _merge_fix_plan_edits(base_plan: Dict[str, Any], new_plan: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base_plan or {})
    merged["summary"] = (new_plan or {}).get("summary") or merged.get("summary") or ""
    merged_edits: List[Dict[str, Any]] = []
    seen = set()
    for plan in (base_plan or {}, new_plan or {}):
        for edit in plan.get("edits", []) if isinstance(plan, dict) else []:
            if not isinstance(edit, dict):
                continue
            key = (str(edit.get("file", "")), str(edit.get("function_signature", "")))
            if key in seen:
                continue
            seen.add(key)
            merged_edits.append(edit)
    merged["edits"] = merged_edits
    return merged


def _upsert_fix_plan_edit(fix_plan: Dict[str, Any], edit: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(fix_plan, dict):
        fix_plan = {"summary": "", "edits": []}
    edits = fix_plan.get("edits", [])
    if not isinstance(edits, list):
        edits = []
    key = (str(edit.get("file", "")), str(edit.get("function_signature", "")))
    out: List[Dict[str, Any]] = []
    replaced = False
    for e in edits:
        if not isinstance(e, dict):
            continue
        k = (str(e.get("file", "")), str(e.get("function_signature", "")))
        if k == key:
            out.append(edit)
            replaced = True
        else:
            out.append(e)
    if not replaced:
        out.append(edit)
    return {"summary": str(fix_plan.get("summary", "")), "edits": out}


_CODE_FENCE_RE = re.compile(
    r"^```(?:cpp|c\+\+|cc|cxx|objc|objective-c|objectivec|mm|m|c|h)?\s*$",
    re.I,
)
_C_FUNCTION_DEF_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:__attribute__\s*\(\(.*?\)\)\s*)*"
    r"(?:(?:static|inline|extern)[ \t]+)*"
    r"(?:[A-Za-z_][\w]*[ \t*]+)+"
    r"([A-Za-z_]\w*)[ \t]*\([^;{]*\)[ \t]*\{"
)
_SKIP_C_FUNCTION_NAMES = frozenset({
    "if", "else", "for", "while", "switch", "do", "catch", "try",
    "sizeof", "return", "defined",
})


def _is_type_skeleton_block(block: str) -> bool:
    """首条有效代码是 class/struct 骨架时，整块不当作函数定义。"""
    for raw in str(block or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("//", "/*", "*", "#")):
            continue
        return bool(re.match(r"(?:typedef\s+)?(?:class|struct)\s+\w+", line))
    return False


def _append_c_function_names(names: List[str], block: str) -> None:
    """从修复围栏里的 C/ObjC 函数定义补进目标函数名。"""
    for match in _C_FUNCTION_DEF_RE.finditer(str(block or "")):
        leaf = match.group(1)
        if leaf in _SKIP_C_FUNCTION_NAMES or len(leaf) < 3:
            continue
        _append_unique_name(names, leaf + "()")


def _strip_leading_attributes(text: str) -> str:
    """去掉开头的 __attribute__((...))，避免把属性宏当成函数名。"""
    stripped = str(text or "").lstrip()
    while True:
        match = re.match(r"__attribute__\s*\(", stripped)
        if not match:
            return stripped
        start = match.end() - 1
        depth = 0
        end = -1
        for index in range(start, len(stripped)):
            char = stripped[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end < 0:
            return stripped
        stripped = stripped[end + 1:].lstrip()


def _declaration_cut_index(source: str, name_index: int) -> int:
    """从函数名退到声明起点，带上同一行的返回类型和紧邻的属性行。"""
    if name_index < 0 or name_index > len(source):
        return name_index
    line_start = source.rfind("\n", 0, name_index) + 1
    if ";" in source[line_start:name_index]:
        return name_index
    cut = line_start
    while cut > 0:
        prev_end = cut - 1
        prev_start = source.rfind("\n", 0, prev_end) + 1
        prev = source[prev_start:cut].strip()
        if not prev or prev.startswith(("//", "/*", "*", "#")):
            break
        if prev.endswith((";", "}", "{")):
            break
        if "(" in prev and not prev.startswith("__attribute__") and not prev.startswith("template"):
            break
        cut = prev_start
    return cut


def _expand_block_to_declaration(source: str, old_block: str, func_name: str) -> str:
    """把已定位的函数块向前扩到声明起点，吃掉残留的属性宏和返回类型。"""
    if not old_block or not func_name:
        return old_block
    index = source.find(old_block)
    if index < 0:
        return old_block
    name_at = source.find(func_name, index)
    if name_at < 0 or name_at > index + max(400, len(func_name) + 80):
        name_at = index
    cut = _declaration_cut_index(source, name_at)
    if cut > index:
        return old_block
    return source[cut:index + len(old_block)]


def _static_decl_name(line: str) -> str:
    """取文件级静态声明里的变量名。"""
    head = re.sub(r"=.*$", "", str(line or "").strip()).rstrip()
    head = re.sub(r";\s*$", "", head).rstrip()
    match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*$", head)
    return match.group(1) if match else ""


_TOP_LEVEL_DECL_START_RE = re.compile(r"^(?:static|extern|typedef|struct|enum|union)\b")


def _strip_trailing_comment(line: str) -> str:
    """去掉行尾 // 或 /* */ 注释，用于判断声明是否以分号结束。"""
    text = re.sub(r"/\*.*?\*/\s*$", "", str(line or "")).rstrip()
    in_string = False
    for index, char in enumerate(text):
        if char == '"' and (index == 0 or text[index - 1] != "\\"):
            in_string = not in_string
        elif not in_string and text.startswith("//", index):
            return text[:index].rstrip()
    return text


def _top_level_decl_name(statement: str) -> str:
    """取文件级声明的符号名：typedef/struct 取末尾别名或标签，变量取等号前的名字。"""
    flat = " ".join(_strip_trailing_comment(x) for x in statement.splitlines()).strip()
    if flat.startswith(("typedef", "struct", "enum", "union")) and "}" in flat:
        tail = flat.rsplit("}", 1)[1]
        match = re.search(r"([A-Za-z_]\w*)\s*;\s*$", tail)
        if match:
            return match.group(1)
        tag = re.match(r"^(?:typedef\s+)?(?:struct|enum|union)\s+([A-Za-z_]\w*)", flat)
        return tag.group(1) if tag else ""
    if flat.startswith("typedef"):
        match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*;\s*$", flat)
        return match.group(1) if match else ""
    head = re.split(r"=|\{", flat, 1)[0]
    if "(" in head:
        return ""
    return _static_decl_name(head)


def _preamble_items_outside_functions(
    block: str,
    function_spans: List[Tuple[int, int, str, str]],
    source: str,
) -> List[Tuple[int, str, str]]:
    """收集围栏里函数之外、源码还没有的 include、宏、类型和文件级变量。"""
    items: List[Tuple[int, str, str]] = []
    lines = block.splitlines(keepends=True)
    offsets: List[int] = []
    total = 0
    for raw in lines:
        offsets.append(total)
        total += len(raw)
    index = 0
    while index < len(lines):
        line_at = offsets[index]
        raw = lines[index]
        stripped = raw.strip()
        if any(start <= line_at < end for start, end, _name, _func in function_spans):
            index += 1
            continue
        if re.match(r"^#\s*(?:include|import)\b", stripped):
            normalized = re.sub(r"\s+", " ", stripped)
            if normalized not in re.sub(r"[ \t]+", " ", source):
                items.append((line_at, normalized, stripped))
            index += 1
            continue
        define = re.match(r"^#\s*define\s+([A-Za-z_]\w*)", stripped)
        if define:
            parts = [raw.rstrip("\n")]
            while parts[-1].rstrip().endswith("\\") and index + 1 < len(lines):
                index += 1
                parts.append(lines[index].rstrip("\n"))
            if not re.search(rf"#\s*define\s+{re.escape(define.group(1))}\b", source):
                items.append((line_at, "#define " + define.group(1), "\n".join(parts).strip()))
            index += 1
            continue
        if not _TOP_LEVEL_DECL_START_RE.match(stripped):
            index += 1
            continue
        parts = [raw.rstrip("\n")]
        depth = stripped.count("{") - stripped.count("}")
        while index + 1 < len(lines) and len(parts) < 200:
            if depth <= 0 and _strip_trailing_comment(parts[-1]).endswith(";"):
                break
            index += 1
            parts.append(lines[index].rstrip("\n"))
            depth += lines[index].count("{") - lines[index].count("}")
        index += 1
        statement = "\n".join(parts).strip()
        if not _strip_trailing_comment(statement.splitlines()[-1]).endswith(";"):
            continue
        name = _top_level_decl_name(statement)
        if not name or re.search(rf"\b{re.escape(name)}\b", source):
            continue
        items.append((line_at, name, statement))
    return items


def _join_symbol_texts(texts: List[str]) -> str:
    """单行符号紧挨着排，多行符号之间空一行。"""
    joined = ""
    for index, text in enumerate(texts):
        if index:
            compact = "\n" not in text and "\n" not in texts[index - 1]
            joined += "\n" if compact else "\n\n"
        joined += text
    return joined


def _is_import_area_text(text: str) -> bool:
    """#include/#import 和只含常量的 #define 进文件头部；引用了其它符号的宏跟变量走。"""
    stripped = str(text or "").lstrip()
    if re.match(r"#\s*(?:include|import)\b", stripped):
        return True
    define = re.match(r"#\s*define\s+([A-Za-z_]\w*)\b(.*)$", stripped, re.S)
    if not define:
        return False
    idents = set(re.findall(r"[A-Za-z_]\w*", define.group(2)))
    idents.discard(define.group(1))
    idents -= {"sizeof", "defined", "alignof", "typeof", "__typeof__"}
    return not idents


def _missing_symbol_groups(
    analysis_text: str,
    source: str,
    replaced_names: Set[str],
    *,
    file_name: str = "",
    single_file: bool = True,
) -> Tuple[str, str]:
    """把缺失符号分成 (头部段, 符号段)：include/常量宏进头部，其余按围栏顺序进符号段。"""
    head: List[str] = []
    body: List[str] = []
    for text in _missing_symbol_texts(
        analysis_text, source, replaced_names, file_name=file_name, single_file=single_file,
    ):
        (head if _is_import_area_text(text) else body).append(text)
    return _join_symbol_texts(head), _join_symbol_texts(body)


def _missing_symbol_texts(
    analysis_text: str,
    source: str,
    replaced_names: Set[str],
    *,
    file_name: str = "",
    single_file: bool = True,
) -> List[str]:
    """收集分析围栏里源码还没有的 include、宏、类型、文件级变量和新函数。

    按围栏前的文件名标题只取本文件的围栏；没有文件名时，只取含本文件被替换函数的围栏，
    单文件修复时全部采用。
    """
    ordered: List[str] = []
    seen: Set[str] = set()
    hinted = _extract_code_blocks_with_file_hint(analysis_text)
    if not hinted:
        hinted = [("", block) for block in _extract_code_blocks(analysis_text)]
    target_name = Path(file_name).name.lower() if file_name else ""
    for hint, block in hinted:
        if hint and target_name and hint.lower() != target_name:
            continue
        if not hint and not single_file and not any(
            _source_defines_function(block, name) for name in replaced_names
        ):
            continue
        function_spans: List[Tuple[int, int, str, str]] = []
        for match in _C_FUNCTION_DEF_RE.finditer(block):
            func = _extract_function_block_from_code(block, match.start(1))
            if not func:
                continue
            at = block.find(func, match.start())
            if at < 0:
                at = match.start()
            function_spans.append((at, at + len(func), match.group(1), func.strip()))
        pieces: List[Tuple[int, str, str]] = []
        for start, _end, name, func in function_spans:
            if name in replaced_names or name in _SKIP_C_FUNCTION_NAMES:
                continue
            if _source_defines_function(source, name):
                continue
            pieces.append((start, name, func))
        pieces.extend(_preamble_items_outside_functions(block, function_spans, source))
        for _start, key, text in sorted(pieces, key=lambda item: item[0]):
            if key in seen:
                continue
            seen.add(key)
            ordered.append(text)
    return ordered


def _is_function_definition_at(source: str, index: int) -> bool:
    """index 起第一个参数表闭合后先遇到 ``{``，且中间没有多出的 ``)``，才是函数定义而非调用。"""
    open_at = source.find("(", max(0, index))
    if open_at < 0:
        return False
    depth = 0
    close_at = -1
    for pos in range(open_at, min(len(source), open_at + 4000)):
        char = source[pos]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                close_at = pos
                break
    if close_at < 0:
        return False
    depth = 0
    for pos in range(close_at + 1, min(len(source), close_at + 800)):
        char = source[pos]
        if char == "{":
            return depth == 0
        if char in ";,":
            return False
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return False


def _first_function_definition_index(source: str, func_name: str) -> int:
    """函数名在源码中第一次以定义形式出现的位置；没有时返回 -1。"""
    if not func_name:
        return -1
    for match in re.finditer(rf"(?<![\w.>]){re.escape(func_name)}\s*\(", source):
        if _is_function_definition_at(source, match.start()):
            return match.start()
    return -1


def _definition_index(source: str, func_name: str) -> int:
    """返回函数定义在源码中的位置；找不到时给一个靠后的序号。"""
    for match in re.finditer(rf"\b{re.escape(func_name)}\s*\(", source):
        line_start = source.rfind("\n", 0, match.start()) + 1
        prefix = source[line_start:match.start()].rstrip()
        if not prefix or prefix.endswith(("=", ",", "(", ">", ".", ":")) or ";" in prefix:
            continue
        rest = source[match.end():match.end() + 800]
        brace = rest.find("{")
        semi = rest.find(";")
        if brace < 0 or (semi >= 0 and semi < brace):
            continue
        return match.start()
    return 10 ** 9


def _symbol_insertion_key(edit: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """去重键：同一文件、同一落点、同一段代码只插一次。"""
    return (
        str(edit.get("file") or ""),
        str(edit.get("placement") or ""),
        str(edit.get("anchor_function") or ""),
        str(edit.get("code") or "").strip(),
    )


def _filter_header_block(source: str, block: str, planned_includes: Set[str]) -> str:
    """去掉源码或已有 include 编辑里已经出现的头部行。"""
    normalized = re.sub(r"[ \t]+", " ", source)
    kept: List[str] = []
    for raw in str(block or "").splitlines():
        stripped = raw.strip()
        if not stripped or stripped in planned_includes:
            continue
        if re.sub(r"\s+", " ", stripped) in normalized:
            continue
        kept.append(stripped)
    return _join_symbol_texts(kept)


def _filter_body_block(source: str, block: str) -> str:
    """整段符号已经在源码里时不再插入。"""
    text = str(block or "").strip()
    if not text:
        return ""
    if re.sub(r"\s+", " ", text) in re.sub(r"\s+", " ", source):
        return ""
    return text


def _attach_missing_symbols(fix_plan: Dict[str, Any], analysis_text: str) -> None:
    """把围栏里缺失的 include/宏和文件级符号做成独立插入，不挂到函数替换上。"""
    edits = fix_plan.get("edits") if isinstance(fix_plan, dict) else None
    if not isinstance(edits, list):
        return
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        if str(edit.get("edit_type") or "function_replacement") != "function_replacement":
            continue
        path = str(edit.get("file") or "").strip()
        if path:
            by_file.setdefault(path, []).append(edit)
    existing = {
        _symbol_insertion_key(item)
        for item in edits
        if isinstance(item, dict) and item.get("edit_type") == "symbol_insertion"
    }
    planned_includes = {
        str(item.get("include") or "").strip()
        for item in edits
        if isinstance(item, dict) and item.get("edit_type") == "include_directive"
    }
    extra: List[Dict[str, Any]] = []
    for path, file_edits in by_file.items():
        file_path = Path(path)
        if not file_path.is_file():
            continue
        try:
            source, _encoding = _try_read_source_raw(file_path)
        except Exception:
            continue
        replaced: Set[str] = set()
        ranked: List[Tuple[int, Dict[str, Any]]] = []
        for edit in file_edits:
            simple = _extract_simple_function_name(str(edit.get("function_signature") or ""))
            if simple:
                replaced.add(simple)
            ranked.append((_definition_index(source, simple) if simple else 10 ** 9, edit))
        head, body = _missing_symbol_groups(
            analysis_text,
            source,
            replaced,
            file_name=path,
            single_file=len(by_file) == 1,
        )
        head = _filter_header_block(source, head, planned_includes)
        body = _filter_body_block(source, body)
        ranked.sort(key=lambda item: item[0])
        anchor = ""
        if ranked:
            anchor = _extract_simple_function_name(str(ranked[0][1].get("function_signature") or ""))
        if head:
            item = {
                "file": path,
                "edit_type": "symbol_insertion",
                "placement": "head",
                "code": head,
                "reason": "independent_file_header",
            }
            key = _symbol_insertion_key(item)
            if key not in existing:
                extra.append(item)
                existing.add(key)
        if body and anchor:
            item = {
                "file": path,
                "edit_type": "symbol_insertion",
                "placement": "before_function",
                "anchor_function": anchor,
                "code": body,
                "reason": "independent_file_symbols",
            }
            key = _symbol_insertion_key(item)
            if key not in existing:
                extra.append(item)
                existing.add(key)
    if extra:
        edits.extend(extra)


def _import_area_insert_offset(source: str) -> int:
    """插到最后一个 #include/#import 之后；没有时插到文件头注释/include guard 之后。"""
    last_end = -1
    for match in re.finditer(r"^[ \t]*#[ \t]*(?:include|import)\b[^\n]*\n?", source, re.M):
        last_end = match.end()
    if last_end >= 0:
        return last_end
    offset = 0
    while offset < len(source):
        if source.startswith(("\n", "\r"), offset):
            offset += 1
            continue
        line_end = source.find("\n", offset)
        line_end = len(source) if line_end < 0 else line_end + 1
        stripped = source[offset:line_end].strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "*/")):
            offset = line_end
            continue
        if re.match(r"#\s*(?:ifndef|if\s+!defined|pragma\s+once)\b", stripped):
            offset = line_end
            continue
        if re.match(r"#\s*define\s+\w+_H(?:PP)?\b", stripped, re.I):
            offset = line_end
            continue
        return offset
    return offset


def _doc_comment_start(source: str, pos: int) -> int:
    """从函数声明起点回退到紧贴着的文档注释第一行。"""
    while pos > 0:
        prev_start = source.rfind("\n", 0, pos - 1) + 1
        line = source[prev_start:pos].strip()
        if not line:
            break
        if line.startswith(("///", "//", "/*", "*", "*/")):
            pos = prev_start
            continue
        break
    return pos


def _before_function_insert_offset(source: str, func_name: str) -> int:
    """插到锚点函数原文档注释之前；找不到函数时返回 -1。"""
    name_at = _first_function_definition_index(source, func_name)
    if name_at < 0:
        name_at = _definition_index(source, func_name)
        if name_at >= 10 ** 9:
            return -1
    return _doc_comment_start(source, _declaration_cut_index(source, name_at))


def _insert_source_block(source: str, offset: int, block: str) -> str:
    """在 ``offset`` 插入一段文件级代码，并保证前后各有换行。"""
    text = str(block or "").strip("\n")
    if not text or offset < 0 or offset > len(source):
        return source
    prefix = "" if offset == 0 or source[:offset].endswith("\n") else "\n"
    suffix = "\n" if offset < len(source) and source[offset] == "\n" else "\n\n"
    return source[:offset] + prefix + text + suffix + source[offset:]


def _apply_symbol_insertion_edit(
    edit: Dict[str, Any],
    code_roots: List[str],
    report_dir: Optional[Path],
    backup_original_sources: bool,
    edit_id: str,
) -> Dict[str, Any]:
    """把独立的头部或函数前符号写进文件。"""
    file_path = Path(str(edit.get("file") or "")).resolve()
    placement = str(edit.get("placement") or "").strip()
    code = str(edit.get("code") or "").strip()
    record: Dict[str, Any] = {
        "edit_id": edit_id,
        "file": str(file_path),
        "edit_type": "symbol_insertion",
        "placement": placement,
        "function_signature": str(edit.get("anchor_function") or ""),
        "reason": str(edit.get("reason") or ""),
        "status": "skipped",
    }
    if not _is_within_code_roots(file_path, code_roots):
        record["error"] = "目标文件不在 code_root 范围内"
        return record
    if not file_path.is_file():
        record["error"] = "目标文件不存在"
        return record
    if not code:
        record["error"] = "symbol_insertion 代码为空"
        return record
    try:
        original, file_encoding = _try_read_source_raw(file_path)
    except (UnicodeDecodeError, LookupError, OSError):
        record["error"] = "目标文件编码无法识别（非 UTF-8/GBK）"
        return record
    if placement == "head":
        block = _filter_header_block(original, code, set())
        offset = _import_area_insert_offset(original)
    elif placement == "before_function":
        block = _filter_body_block(original, code)
        offset = _before_function_insert_offset(original, str(edit.get("anchor_function") or ""))
    else:
        record["error"] = f"不支持的 symbol_insertion placement: {placement}"
        return record
    if not block:
        record["error"] = "符号已存在，无需修改"
        return record
    if offset < 0:
        record["error"] = "找不到 symbol_insertion 的锚点函数"
        return record
    eol = _source_newline_style(original)
    new_source = _convert_newlines(_insert_source_block(original, offset, block), eol)
    try:
        _write_text_raw(file_path, new_source, encoding=file_encoding)
    except (UnicodeEncodeError, UnicodeDecodeError):
        _write_text_raw(file_path, new_source, encoding="utf-8")
    if report_dir is not None and backup_original_sources:
        backup_root = report_dir / "original_sources"
        containing_root = _find_containing_code_root(file_path, code_roots)
        if containing_root:
            backup_path = backup_root / file_path.relative_to(containing_root)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if not backup_path.exists():
                _write_text_raw(backup_path, original, encoding="utf-8")
            record["backup_path"] = str(backup_path)
    record["status"] = "applied"
    record["replaced_preview"] = block
    return record


def _source_defines_function(source: str, func_name: str) -> bool:
    """源码里是否有该函数的定义，而不是调用或赋值。"""
    name = str(func_name or "").strip()
    if not name:
        return False
    for match in re.finditer(rf"\b{re.escape(name)}\s*\(", source):
        line_start = source.rfind("\n", 0, match.start()) + 1
        prefix = source[line_start:match.start()].rstrip()
        if not prefix:
            prev_end = line_start - 1
            prev_start = source.rfind("\n", 0, max(0, prev_end - 1)) + 1
            prefix = source[prev_start:prev_end].strip()
        if not prefix or prefix.endswith(("=", ",", "(", ">", ".", ":")):
            continue
        if ";" in prefix:
            continue
        rest = source[match.end():match.end() + 800]
        brace = rest.find("{")
        semi = rest.find(";")
        if brace < 0 or (semi >= 0 and semi < brace):
            continue
        return True
    return False


def _resolve_session_read_path(raw: str, code_roots: List[str]) -> Optional[Path]:
    """把账本里的绝对路径或相对 code root 的路径解析成真实文件。"""
    text = str(raw or "").strip()
    if not text or not code_roots:
        return None
    path = Path(text)
    candidates = [path] if path.is_absolute() else [Path(root) / path for root in code_roots]
    for candidate in candidates:
        try:
            if not candidate.is_file() or not _is_within_code_roots(candidate, code_roots):
                continue
            return candidate.resolve()
        except OSError:
            continue
    return None


_SOURCE_SESSION_TYPES = frozenset({"read_file", "function"})


def _session_source_file_hints(context_session: Dict[str, Any]) -> List[str]:
    """账本和已解析上下文里成功拿到的源码路径。"""
    hints: List[str] = []
    for item in context_session.get("request_ledger") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("kind") or "")
        if kind not in _SOURCE_SESSION_TYPES:
            continue
        if str(item.get("status") or "") not in ("success", "ok"):
            continue
        text = str(item.get("file") or "").strip()
        if text:
            hints.append(text)
    for rnd in context_session.get("rounds") or []:
        if not isinstance(rnd, dict):
            continue
        for item in rnd.get("resolved_context") or []:
            if not isinstance(item, dict) or not item.get("success"):
                continue
            request = item.get("request") if isinstance(item.get("request"), dict) else {}
            kind = str(request.get("type") or item.get("type") or "")
            if kind not in _SOURCE_SESSION_TYPES:
                continue
            text = str(item.get("file") or request.get("file") or "").strip()
            if text:
                hints.append(text)
    return hints


def _read_files_from_session(
    context_session: Optional[Dict[str, Any]],
    code_roots: List[str],
) -> List[Path]:
    """取出本轮成功读过或按函数定位到的、且落在 code root 内的源码路径。"""
    if not isinstance(context_session, dict) or not code_roots:
        return []
    found: List[Path] = []
    seen = set()
    for raw in _session_source_file_hints(context_session):
        resolved = _resolve_session_read_path(raw, code_roots)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
    return found


def _graph_has_read_file_candidates(code_context: Dict[str, Any]) -> bool:
    """图节点是否来自已读源码补齐，而不是崩溃行定位。"""
    graph = code_context.get("graph") if isinstance(code_context, dict) else {}
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(nodes, list):
        return False
    for node in nodes:
        if not isinstance(node, dict) or node.get("origin") != "read_file":
            continue
        snippet = node.get("snippet")
        if node.get("file") and node.get("signature") and isinstance(snippet, list) and snippet:
            return True
    return False


def _candidate_node_from_defined_source(
    path: Path,
    source: str,
    signature: str,
) -> Optional[Dict[str, Any]]:
    """从已读源码切出与签名对应的完整函数，不使用行窗兜底。"""
    _updated, block = CodeFixer.replace_function_block(source, signature, "")
    if not block or not _extracted_block_matches_signature(block, signature):
        return None
    lines = str(block).splitlines()
    if len(lines) < 2:
        return None
    index = source.find(block)
    start_line = source.count("\n", 0, index) + 1 if index >= 0 else 1
    return {
        "type": "function",
        "file": str(path),
        "signature": sanitize_function_signature(signature),
        "snippet": lines,
        "snippet_start_line": start_line,
        "snippet_end_line": start_line + len(lines) - 1,
        "origin": "read_file",
    }


_REPAIR_COMPLETION_SOURCE_BUDGET = 24000


def _analysis_mentions_path(analysis_text: str, path: Path) -> bool:
    """分析正文或 target_files 是否点到了这个源文件。"""
    text = str(analysis_text or "")
    if path.name in text or str(path) in text:
        return True
    return any(part and part in text for part in path.parts[-3:])


def _select_repair_source_files(analysis_text: str, files: List[Path]) -> List[Path]:
    """优先送分析点名的已读源码，控制补全请求的体积。"""
    ranked = sorted(
        files,
        key=lambda path: (
            0 if _analysis_mentions_path(analysis_text, path) else 1,
            str(path),
        ),
    )
    chosen: List[Path] = []
    used = 0
    for path in ranked:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        mentioned = _analysis_mentions_path(analysis_text, path)
        if size > _REPAIR_COMPLETION_SOURCE_BUDGET and not mentioned:
            continue
        if used >= _REPAIR_COMPLETION_SOURCE_BUDGET and not mentioned:
            break
        chosen.append(path)
        used += min(size, _REPAIR_COMPLETION_SOURCE_BUDGET)
        if len(chosen) >= 4:
            break
    return chosen


def _complete_missing_repair_code(
    llm: Any,
    analysis_text: str,
    *,
    context_session: Optional[Dict[str, Any]],
    code_roots: List[str],
) -> str:
    """分析只有步骤说明时，补一次只产出完整函数的改码请求。"""
    if llm is None or not hasattr(llm, "chat"):
        return ""
    files = _select_repair_source_files(
        analysis_text,
        _read_files_from_session(context_session, code_roots),
    )
    if not files:
        return ""
    source_parts: List[str] = []
    for path in files:
        try:
            source, _encoding = _try_read_source_raw(path)
        except Exception:
            continue
        clipped = source if len(source) <= 12000 else source[:12000]
        source_parts.append(f"### {path.name}\n```\n{clipped}\n```")
    if not source_parts:
        return ""
    advice = str(analysis_text or "")[-6000:]
    prompt = (
        "下面是崩溃分析，以及已经读过的源码。请只输出「修复代码」。\n"
        "每个要修改的、源码中已经存在的函数，各给一个完整定义，放在独立围栏里。\n"
        "`.m` / `.mm` 用 objc，`.c` 用 c，`.cpp` 用 cpp。\n"
        "新增的文件级 static 和源码中没有的 helper，放在第一个被替换函数的同一围栏之前。\n"
        "函数名必须是源码里已有的名字。可以把 `__attribute__((visibility(\"hidden\")))` "
        "换成 `static void`。\n"
        "若根因是信号上下文死锁，替换函数不得再调用挂起线程、ObjC 消息、malloc、"
        "NSThread、callStackSymbols、callStackReturnAddresses。\n"
        "崩溃 PC 从 ucontext 读取；地址范围在注册时算好；崩溃记录用预先打开的 fd 加 write。\n"
        "只删掉挂起调用、把这些调用留在 handler 里，不能当作最终修复。\n"
        "禁止省略号和步骤说明。不要输出解释。\n\n"
        "## 分析\n"
        f"{advice}\n\n"
        "## 已读源码\n"
        + "\n\n".join(source_parts)
    )
    call_kwargs: Dict[str, Any] = {"temperature": 0, "max_tokens": 16384}
    if getattr(llm, "supports_effort", False):
        # 只要求输出函数，思考型模型默认思考量会耗尽输出预算导致正文为空。
        call_kwargs["effort"] = "low"
    try:
        response = llm.chat([{"role": "user", "content": prompt}], **call_kwargs)
    except Exception as exc:
        logger.warning("[CodeFixer] 补全修复代码失败: %s", exc)
        return ""
    content = str(getattr(response, "content", "") or "").strip()
    if not content:
        return ""
    if not _extract_required_function_names_from_analysis(content):
        return ""
    return "## 修复代码\n\n" + content


def promote_read_file_function_candidates(
    code_context: Dict[str, Any],
    *,
    analysis_text: str,
    context_session: Optional[Dict[str, Any]],
    code_roots: List[str],
) -> List[Dict[str, Any]]:
    """崩溃行没有符号时，用分析点名且已经读过的函数补候选节点。

    watchdog/ANR 流程不跑 code_content_provider，code_context 会是空的。
    这里收 request_ledger 里读成功或按 function 定位到的文件，并且源码里真有该函数定义。
    已有证据摘要明确禁止自动改码时，不改写该结论。
    """
    if not isinstance(code_context, dict):
        return []
    graph = code_context.get("graph")
    if not isinstance(graph, dict):
        graph = {}
        code_context["graph"] = graph
    existing = graph.get("nodes")
    if isinstance(existing, list) and any(
        isinstance(node, dict) and isinstance(node.get("snippet"), list) and node.get("snippet")
        for node in existing
    ):
        return []
    names = _extract_required_function_names_from_analysis(analysis_text)
    files = _read_files_from_session(context_session, code_roots)
    if not names or not files:
        return []
    built: List[Dict[str, Any]] = []
    seen = set()
    for path in files:
        try:
            source, _encoding = _try_read_source_raw(path)
        except Exception:
            continue
        for name in names:
            simple = _extract_simple_function_name(name)
            if not simple or not _source_defines_function(source, simple):
                continue
            key = (str(path), simple)
            if key in seen:
                continue
            signature = name if "(" in str(name) else f"{simple}()"
            node = _candidate_node_from_defined_source(path, source, signature)
            if not node:
                continue
            seen.add(key)
            built.append(node)
    if not built:
        return []
    graph["nodes"] = built
    summary = graph.get("evidence_summary")
    if not isinstance(summary, dict):
        graph["evidence_summary"] = {
            "auto_fix_allowed": True,
            "auto_fix_block_reason": None,
            "allow_reason": "read_file_cited_function",
        }
    return built


def _extract_code_blocks(analysis_text: str) -> List[str]:
    """从 analysis_text 中提取 C/C++/ObjC 代码围栏；若无围栏则尝试后备提取。"""
    blocks: List[str] = []
    lines = str(analysis_text or "").splitlines()
    in_block = False
    cur: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_block:
            if _CODE_FENCE_RE.match(stripped):
                in_block = True
                cur = []
            continue
        if stripped.startswith("```"):
            block = "\n".join(cur).strip("\n")
            if block:
                blocks.append(block)
            in_block = False
            cur = []
            continue
        cur.append(line)
    if in_block and cur:
        block = "\n".join(cur).strip("\n")
        if block:
            blocks.append(block)
    # 后备：无围栏时，尝试按函数定义模式提取裸代码块
    if not blocks:
        blocks = _extract_unfenced_code_blocks(analysis_text)
    return blocks


_FILE_HINT_RE = re.compile(r"([A-Za-z_][\w+\-]*\.(?:mm|m|cc|cpp|cxx|c|hpp|hh|h|java|kt|swift|ets|ts))\b")


def _extract_code_blocks_with_file_hint(analysis_text: str) -> List[Tuple[str, str]]:
    """提取代码围栏及其所属文件名；文件名取围栏前最近一个带文件名的 Markdown 标题。"""
    result: List[Tuple[str, str]] = []
    hint = ""
    in_block = False
    cur: List[str] = []
    for line in str(analysis_text or "").splitlines():
        stripped = line.strip()
        if not in_block:
            if stripped.startswith("#"):
                match = _FILE_HINT_RE.search(stripped)
                hint = match.group(1) if match else ""
                continue
            match = _FILE_HINT_RE.search(stripped)
            if match:
                rest = stripped.replace(match.group(1), "")
                rest = re.sub(r"[`*_:：\s]|文件|修改|路径", "", rest)
                if not rest:
                    hint = match.group(1)
                    continue
            if _CODE_FENCE_RE.match(stripped):
                in_block = True
                cur = []
            continue
        if stripped.startswith("```"):
            block = "\n".join(cur).strip("\n")
            if block:
                result.append((hint, block))
            in_block = False
            cur = []
            continue
        cur.append(line)
    if in_block and cur:
        block = "\n".join(cur).strip("\n")
        if block:
            result.append((hint, block))
    return result


def _extract_unfenced_code_blocks(analysis_text: str) -> List[str]:
    """后备提取：从无围栏文本中按函数签名+大括号配对提取代码块。

    识别策略：找到「返回类型 + 函数名(」或「类名::函数名(」模式后跟 {，
    按大括号配对提取完整函数体。
    """
    text = str(analysis_text or "")
    blocks: List[str] = []
    # 匹配常见 C++ 函数定义行的起始（含 class 前缀、返回值等）
    # 例如: "void crash_nullptr() {", "int Foo::bar(int x) {"
    func_pattern = re.compile(
        r"^[ \t]*"
        r"(?:(?:virtual|static|inline|explicit|constexpr|friend)\s+)*"
        r"(?:[\w:*&<>,\s]+?\s+)?"
        r"(?:~?\w+(?:::\w+)*)\s*\([^)]*\)"
        r"(?:\s*(?:const|override|noexcept|final))*"
        r"\s*\{?",
        re.MULTILINE,
    )
    # 控制流关键字不是函数定义
    _CONTROL_KW = {"if", "else", "for", "while", "switch", "do", "catch", "try"}
    for m in func_pattern.finditer(text):
        # 从匹配位置向后找 {
        start = m.start()
        # 过滤控制语句
        first_word = m.group().strip().split("(")[0].split()[-1] if "(" in m.group() else ""
        if first_word in _CONTROL_KW:
            continue
        brace_pos = text.find("{", m.start())
        if brace_pos < 0 or brace_pos > m.end() + 5:
            continue
        # 确保不在 markdown 标题行或列表项中
        line_start = text.rfind("\n", 0, start)
        line_prefix = text[line_start + 1 : start] if line_start >= 0 else text[:start]
        if line_prefix.strip().startswith(("#", "-", "*", ">")):
            continue
        # 大括号配对
        depth = 0
        end_pos = -1
        for i in range(brace_pos, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end_pos = i
                    break
        if end_pos < 0:
            continue
        block = text[start : end_pos + 1].strip()
        # 至少 2 行才算有效函数体
        if block.count("\n") >= 1:
            blocks.append(block)
    return blocks


def _extract_function_block_from_code(code_text: str, start_index: int) -> Optional[str]:
    brace_index = code_text.find("{", start_index)
    if brace_index < 0:
        return None
    head_text = code_text[start_index:brace_index]
    # 函数名后到函数体左括号前不应出现分号；出现分号通常是调用语句或声明，
    # 例如 ReleaseAllLayers(); 后面跟另一个 if {...}，不能误当成函数定义。
    if ";" in head_text:
        return None
    line_start = code_text.rfind("\n", 0, start_index)
    if line_start < 0:
        line_start = 0
    else:
        line_start += 1
    # 匹配起点可能落在调用语句的分号之后，块又从行首截取，会把 foo(x); 加后面的 if {...} 当成 foo 的定义。
    if ";" in code_text[line_start:brace_index]:
        return None
    prefix_start = _find_template_prefix_start(code_text, line_start)
    if prefix_start is not None:
        line_start = prefix_start
    depth = 0
    end_index = -1
    for i in range(brace_index, len(code_text)):
        ch = code_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_index = i
                break
    if end_index < 0:
        return None
    block = code_text[line_start : end_index + 1].strip()
    if not block:
        return None
    return block


def _find_template_prefix_start(code_text: str, line_start: int) -> Optional[int]:
    """若函数定义前紧邻 template<...> 声明，则返回 template 行起点。"""
    if line_start <= 0:
        return None
    lines_before = code_text[:line_start].splitlines(keepends=True)
    if not lines_before:
        return None
    pos = line_start
    i = len(lines_before) - 1
    # 跳过函数签名前最多一个空行不安全，模板声明必须紧邻，因此遇空行即停止。
    template_start_pos: Optional[int] = None
    while i >= 0:
        line = lines_before[i]
        stripped = line.strip()
        pos -= len(line)
        if not stripped:
            break
        if stripped.startswith("template"):
            template_start_pos = pos
            break
        if template_start_pos is None and (
            stripped == ">"
            or stripped.endswith(",")
            or stripped.startswith("class ")
            or stripped.startswith("typename ")
        ):
            i -= 1
            continue
        break
    return template_start_pos


def _extract_block_signature(block: str) -> str:
    """从函数代码块提取函数签名文本（到形参列表结束及尾部限定符，空白已规范化）。"""
    text = _strip_leading_attributes(str(block or "").strip())
    if not text:
        return ""
    open_idx = _parameter_list_open_index(text)
    if open_idx >= 0:
        depth = 0
        close_idx = -1
        angle = 0
        for i in range(open_idx, len(text)):
            ch = text[i]
            if ch == "<":
                angle += 1
                continue
            if ch == ">" and angle > 0:
                angle -= 1
                continue
            if angle > 0:
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close_idx = i
                    break
        if close_idx >= 0:
            after = text[close_idx + 1 :]
            # 只保留 const/noexcept 等尾缀；不把 ctor-initializer / 函数体算进签名。
            qual_match = re.match(
                r"^((?:\s*(?:const|volatile|noexcept\s*(?:\([^)]*\))?|override|final|&&|&))*)",
                after,
            )
            qual = qual_match.group(1) if qual_match else ""
            return _normalize_signature_whitespace(text[: close_idx + 1] + qual)
    brace = text.find("{")
    if brace < 0:
        return ""
    return _normalize_signature_whitespace(text[:brace])


def _extract_signature_function_name(signature: str) -> str:
    """提取声明中的函数名（忽略参数类型里的嵌套 ``name(``）。"""
    _, name = _extract_owner_and_name(signature)
    return name


def _target_is_destructor(signature: str) -> bool:
    return bool(re.search(r"(?:^|::)\s*~[A-Za-z_]\w*\s*\(", str(signature or "")))


def _replacement_signature_compatible(target_signature: str, replacement_code: str) -> bool:
    """确认 replacement 是目标函数的完整定义，类名/函数名一致。

    只约束身份（类 + 函数名），不要求签名字符串逐字相同：多行参数列表、Tab/空格、
    ``T*`` vs ``T *``、换行折行等格式差异一律视为兼容。参数类型被改写时不在此拦截
    （避免模型排版导致误拒）；跨类同名仍拒绝。
    """
    # 新符号可以排在目标函数前面；只要替换文本里有目标函数的定义即可。
    target_simple = _extract_simple_function_name(target_signature)
    target_owner, _target_func = _extract_owner_and_name(target_signature)
    if target_simple and not target_owner and _source_defines_function(replacement_code, target_simple):
        return True
    block_sig = _extract_block_signature(replacement_code)
    if not block_sig:
        return False
    stripped = block_sig.strip()
    if ";" in stripped or stripped.startswith("//") or stripped.startswith("/*"):
        return False
    target_norm = _normalize_signature_whitespace(target_signature)
    target_name = _extract_signature_function_name(target_norm)
    block_name = _extract_signature_function_name(block_sig)
    if target_name and block_name and target_name != block_name:
        return False
    target_owner, _ = _extract_owner_and_name(target_norm)
    block_owner, _ = _extract_owner_and_name(block_sig)
    if target_owner and block_owner:
        t_tail = target_owner.split("::")[-1]
        b_tail = block_owner.split("::")[-1]
        if target_owner != block_owner and t_tail != b_tail:
            return False
    if _target_is_destructor(target_norm):
        # 析构函数没有返回类型；`bool Class::~Class()` 或 `bool ~Class()` 都不可直接替换。
        if re.match(r"^\s*(?:[\w:<>,*&]+\s+)+[A-Za-z_]\w*::~[A-Za-z_]\w*\s*\(", block_sig):
            return False
        if re.match(r"^\s*(?:[\w:<>,*&]+\s+)+~[A-Za-z_]\w*\s*\(", block_sig):
            return False
    return True


def _normalize_params_for_match(params: str) -> str:
    return re.sub(r"\s+", "", str(params or ""))


def _extract_inline_method_from_class_block(code_text: str, target_signature: str) -> Optional[str]:
    """从 class/struct 代码块中按目标签名提取 inline 方法定义。"""
    sig_text = str(target_signature or "").strip()
    if not sig_text:
        return None
    m = re.search(r"(~?\w+)\s*\(([^)]*)\)\s*(const)?", sig_text)
    if not m:
        return None
    method_name = m.group(1)
    target_params = _normalize_params_for_match(m.group(2))
    target_const = bool(m.group(3))
    method_pattern = re.compile(
        rf"^[ \t]*(?:[\w:*&<>,\s]+?\s+)?{re.escape(method_name)}\s*\(([^)]*)\)"
        rf"(?:\s*(const))?(?:\s*(?:override|noexcept|final))*\s*\{{",
        re.MULTILINE,
    )
    for mm in method_pattern.finditer(code_text):
        params = _normalize_params_for_match(mm.group(1))
        is_const = bool(mm.group(2))
        # 参数不一致则跳过，避免同名重载误命中
        if target_params and params != target_params:
            continue
        # 目标声明有 const 时，要求命中方法也带 const
        if target_const and not is_const:
            continue
        block = _extract_function_block_from_code(code_text, mm.start())
        if block:
            return block
    return None


def _extract_replacement_from_analysis(
    analysis_text: str,
    target_signature: str,
) -> Optional[str]:
    blocks = _extract_code_blocks(analysis_text)
    if not blocks:
        return None
    target_signature = sanitize_function_signature(str(target_signature or "").strip())
    simple_name = _extract_simple_function_name(target_signature)
    # Structured reports usually quote the original function before the
    # proposed repair, so the last matching definition is the useful one.
    for code in reversed(blocks):
        idx = code.find(target_signature) if target_signature else -1
        if idx >= 0:
            block = _extract_function_block_from_code(code, idx)
            if block:
                return sanitize_replacement_code(block, target_signature)
        # 代码块是 class/struct 定义时，优先从类体内按方法名提取 inline 方法。
        # 这样可以避免普通函数扫描在一行多个 inline 方法/注释场景下粘连相邻方法。
        if re.search(r"^\s*(?:class|struct)\s+\w+", code, re.MULTILINE):
            inline_block = _extract_inline_method_from_class_block(code, target_signature)
            if inline_block:
                return sanitize_replacement_code(inline_block, target_signature)
        # 先按块内每个函数定义扫描，通过 signatures_match 做容错匹配
        func_pattern = re.compile(
            r"(?:[\w:*&<>,\s]+?\s+)?(~?\w+(?:::\w+)*)\s*\([^)]*\)"
            r"(?:\s*(?:const|override|noexcept|final))*"
            r"\s*\{",
            re.MULTILINE,
        )
        for m in func_pattern.finditer(code):
            block = _extract_function_block_from_code(code, m.start())
            if not block:
                continue
            blk_sig = _extract_block_signature(block)
            if (
                blk_sig
                and target_signature
                and signatures_match(blk_sig, target_signature)
            ):
                return sanitize_replacement_code(block, target_signature)
        if simple_name:
            pattern = re.compile(rf"\b{re.escape(simple_name)}\s*\(", re.M)
            for m in pattern.finditer(code):
                block = _extract_function_block_from_code(code, m.start())
                if block and (
                    _extract_simple_function_name(block) == simple_name
                    or _source_defines_function(block, simple_name)
                ):
                    return sanitize_replacement_code(block, target_signature)
    return None


def _fill_empty_replacements(
    fix_plan: Dict[str, Any],
    analysis_text: str,
) -> Dict[str, Any]:
    if not isinstance(fix_plan, dict):
        return {"summary": "", "edits": []}
    edits = fix_plan.get("edits", [])
    if not isinstance(edits, list):
        return {"summary": str(fix_plan.get("summary", "")), "edits": []}
    new_edits: List[Dict[str, Any]] = []
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        merged = dict(edit)
        replacement = str(merged.get("replacement_code", "")).strip()
        if not replacement:
            sig = str(merged.get("function_signature", ""))
            fallback = _extract_replacement_from_analysis(analysis_text, sig)
            if fallback:
                merged["replacement_code"] = fallback
                merged["reason"] = str(merged.get("reason") or "") + "；fallback=analysis_text"
        new_edits.append(merged)
    return {"summary": str(fix_plan.get("summary", "")), "edits": new_edits}


def _append_missing_edits(
    fix_plan: Dict[str, Any],
    analysis_text: str,
    required_targets: List[Dict[str, str]],
) -> Dict[str, Any]:
    if not isinstance(fix_plan, dict):
        fix_plan = {"summary": "", "edits": []}
    edits = fix_plan.get("edits", [])
    if not isinstance(edits, list):
        edits = []
    existing_keys = {
        (str(e.get("file", "")), str(e.get("function_signature", "")))
        for e in edits
        if isinstance(e, dict)
    }
    appended = list(edits)
    for t in required_targets or []:
        file_path = str(t.get("file", ""))
        signature = str(t.get("function_signature", ""))
        key = (file_path, signature)
        if not file_path or not signature or key in existing_keys:
            continue
        replacement = _extract_replacement_from_analysis(analysis_text, signature)
        if not replacement:
            continue
        appended.append(
            {
                "file": file_path,
                "function_signature": signature,
                "replacement_code": replacement,
                "reason": "fallback=analysis_text_missing_edit",
            }
        )
        existing_keys.add(key)
    return {"summary": str(fix_plan.get("summary", "")), "edits": appended}


def _header_member_names(header: str) -> Set[str]:
    """从头文件声明行收集已有 m_* 成员名。"""
    names: Set[str] = set()
    for line in str(header or "").splitlines():
        if ";" not in line:
            continue
        names.update(re.findall(r"\bm_\w+\b", line))
    return names


def _format_member_like(sibling_line: str, new_name: str) -> str:
    """按同类成员声明的缩进和空格，生成新成员那一行。"""
    matched = re.match(r"^(\s*\S+\s+)(m_\w+)(\s*;.*)$", sibling_line.rstrip())
    if not matched:
        return ""
    return f"{matched.group(1)}{new_name}{matched.group(3)}"


def _extract_added_sync_member_edits(
    analysis_text: str,
    header: str,
    file_path: Path,
) -> List[Dict[str, Any]]:
    """从两行声明块提取新增的 m_*Mutex / m_*Lock，插到已有成员声明之后。"""
    known = _header_member_names(header)
    edits: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    decl_re = re.compile(r"^(\s*)([A-Za-z_][\w:]*)\s+(m_\w*(?:Mutex|Lock))\s*;\s*$")
    blocks = re.findall(
        r"```(?:cpp|c\+\+|cc|cxx)?\s*\n(.*?)```",
        analysis_text or "",
        re.DOTALL,
    )
    for block in blocks:
        lines = block.splitlines()
        for index, line in enumerate(lines):
            matched = decl_re.match(line.rstrip())
            if not matched:
                continue
            type_name = matched.group(2)
            new_name = matched.group(3)
            if new_name in known or new_name in seen:
                continue
            type_exists = re.search(
                rf"^\s*{re.escape(type_name)}\s+m_\w+\s*;",
                header,
                re.MULTILINE,
            )
            if not type_exists:
                continue
            anchor_name = ""
            for prev in reversed(lines[:index]):
                prev_names = [
                    name for name in re.findall(r"\bm_\w+\b", prev) if name in known
                ]
                if prev_names and ";" in prev:
                    anchor_name = prev_names[-1]
                    break
            if not anchor_name:
                continue
            anchor_line = ""
            for header_line in header.splitlines():
                if (
                    re.search(rf"\b{re.escape(anchor_name)}\b", header_line)
                    and header_line.rstrip().endswith(";")
                ):
                    anchor_line = header_line.rstrip()
                    break
            sibling_line = type_exists.group(0).rstrip()
            new_line = _format_member_like(sibling_line, new_name)
            if not anchor_line or not new_line or new_line in header:
                continue
            seen.add(new_name)
            edits.append(
                {
                    "file": str(file_path),
                    "edit_type": "member_declaration",
                    "old_text": anchor_line,
                    "new_text": anchor_line + "\n" + new_line,
                    "reason": f"新增同步成员 {new_name}，插在 {anchor_name} 之后",
                    "sync_init_name": new_name,
                    "sync_init_type": type_name,
                }
            )
    return edits


def _members_introduced_by_declaration_edits(edits: List[Dict[str, Any]]) -> Set[str]:
    """同一修复计划里即将写入头文件的新成员，允许出现在函数替换中。"""
    names: Set[str] = set()
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        if str(edit.get("edit_type") or "") != "member_declaration":
            continue
        old_names = set(re.findall(r"\bm_\w+\b", str(edit.get("old_text") or "")))
        new_names = set(re.findall(r"\bm_\w+\b", str(edit.get("new_text") or "")))
        names.update(new_names - old_names)
    return names


def _preprocessor_depths(lines: List[str]) -> List[int]:
    """每行所在的 #if/#ifdef/#ifndef 嵌套深度，指令行本身记进入该分支之前的深度。"""
    depths: List[int] = []
    depth = 0
    opened = re.compile(r"^\s*#\s*(?:if|ifdef|ifndef)\b")
    closed = re.compile(r"^\s*#\s*endif\b")
    for line in lines:
        depths.append(depth)
        if opened.match(line):
            depth += 1
        elif closed.match(line):
            depth = max(0, depth - 1)
    return depths


def _insert_mutex_create_beside_sibling(
    header_path: Path,
    new_name: str,
    type_name: str,
    code_roots: List[str],
) -> Optional[str]:
    """在同目录实现文件里，于同类成员的 Create 调用后插入同样参数的初始化。"""
    if not new_name or not type_name or not header_path.is_file():
        return None
    try:
        header_text, _encoding = _try_read_source_raw(header_path)
    except (UnicodeDecodeError, LookupError, OSError):
        return "头文件无法读取，未补 Create 初始化"
    siblings = re.findall(
        rf"^\s*{re.escape(type_name)}\s+(m_\w+)\s*;",
        header_text,
        re.MULTILINE,
    )
    siblings = [name for name in siblings if name != new_name]
    if not siblings:
        return "未找到同类锁，未补 Create 初始化"
    create_line_re = re.compile(
        r"^(\s*)("
        + "|".join(re.escape(name) for name in siblings)
        + r")\.Create\((.*)\)\s*;\s*$"
    )
    own_create_re = re.compile(
        rf"^(\s*){re.escape(new_name)}\.Create\((.*)\)\s*;\s*$"
    )
    for cpp in sorted(header_path.parent.glob("*.cpp")):
        cpp_path = cpp.resolve()
        if not _is_within_code_roots(cpp_path, code_roots):
            continue
        try:
            original, encoding = _try_read_source_raw(cpp)
        except (UnicodeDecodeError, LookupError, OSError):
            continue
        lines = original.splitlines()
        depths = _preprocessor_depths(lines)
        if any(
            own_create_re.match(line) and depths[index] == 0
            for index, line in enumerate(lines)
        ):
            return None
        unconditional = [
            (index, create_line_re.match(line))
            for index, line in enumerate(lines)
            if depths[index] == 0 and create_line_re.match(line)
        ]
        if not unconditional:
            if any(create_line_re.match(line) for line in lines):
                return "同类 Create 都在预处理条件里，未插入初始化"
            continue
        line_no, found = unconditional[0]
        if found is None:
            continue
        indent = found.group(1)
        args = found.group(3)
        insert_line = f"{indent}{new_name}.Create({args});"
        eol = _source_newline_style(original)
        lines.insert(line_no + 1, insert_line)
        trailing = eol if original.endswith(("\n", "\r\n")) else ""
        new_source = _convert_newlines("\n".join(lines) + trailing, eol)
        try:
            _write_text_raw(cpp, new_source, encoding=encoding)
        except (UnicodeEncodeError, UnicodeDecodeError):
            _write_text_raw(cpp, new_source, encoding="utf-8")
        return None
    return "未找到同类 Create 初始化，需人工补构造"


def _candidate_member_header_files(code_context: Dict[str, Any]) -> List[Path]:
    """成员声明可写入的头文件：owner 类，以及图中的 class_skeleton。"""
    from tools.owner_class_context import resolve_owner_class_from_code_context

    raw_paths: List[str] = []
    owner = resolve_owner_class_from_code_context(code_context) or {}
    if isinstance(owner, dict):
        raw_paths.append(str(owner.get("definition_file") or ""))
    graph = code_context.get("graph") if isinstance(code_context, dict) else None
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict) or node.get("type") != "class_skeleton":
                continue
            raw_paths.append(str(node.get("file") or node.get("definition_file") or ""))
    found: List[Path] = []
    seen: Set[str] = set()
    for raw in raw_paths:
        text = raw.strip()
        if not text:
            continue
        path = Path(text)
        if not path.is_file():
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        found.append(path)
    return found


def _extract_member_declaration_edits(
    analysis_text: str,
    code_context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """从AI分析文本的代码块中提取成员变量声明变更，生成 member_declaration 类型的 edit。"""
    edits: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()
    for file_path in _candidate_member_header_files(code_context):
        for edit in _member_declaration_edits_in_header(analysis_text, file_path):
            key = str(edit.get("sync_init_name") or edit.get("old_text") or "")
            if key and key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
            edits.append(edit)
    return edits


def _member_declaration_edits_in_header(
    analysis_text: str,
    file_path: Path,
) -> List[Dict[str, Any]]:
    """在一个头文件上提取 volatile 替换和新增同步成员。"""
    edits: List[Dict[str, Any]] = []
    code_blocks = re.findall(r"```(?:cpp|c\+\+)?\s*\n(.*?)```", analysis_text or "", re.DOTALL)
    if not code_blocks:
        return edits
    try:
        original = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return edits
    atomic_decl_re = re.compile(r"^\s*(std::atomic<[^>]+>)\s+(\w+)\s*;", re.MULTILINE)
    original_lines = original.splitlines()
    for block in code_blocks:
        for m in atomic_decl_re.finditer(block):
            atomic_type = m.group(1)
            var_name = m.group(2)
            volatile_line_re = re.compile(
                r"^(\s*)volatile\s+\w+\s+" + re.escape(var_name) + r"\s*;$"
            )
            for line_idx, line in enumerate(original_lines):
                vm = volatile_line_re.match(line)
                if vm:
                    old_text = line.rstrip()
                    indent = vm.group(1)
                    new_text = f"{indent}{atomic_type} {var_name};"
                    if any(
                        e.get("old_text", "").strip() == old_text.strip()
                        for e in edits
                        if isinstance(e, dict)
                    ):
                        break
                    edits.append({
                        "file": str(file_path),
                        "edit_type": "member_declaration",
                        "old_text": old_text,
                        "new_text": new_text,
                        "reason": f"将 {var_name} 从 volatile 改为 {atomic_type}，确保多线程原子性",
                    })
                    break
    edits.extend(_extract_added_sync_member_edits(analysis_text, original, file_path))
    return edits


def _extract_include_directive_edits(
    analysis_text: str,
    code_context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """从 AI 分析文本提取缺失的 #include，并生成 include_directive edit。"""
    edits: List[Dict[str, Any]] = []
    from tools.owner_class_context import resolve_owner_class_from_code_context

    owner_ctx = resolve_owner_class_from_code_context(code_context) or {}
    if not isinstance(owner_ctx, dict):
        return edits
    def_file = str(owner_ctx.get("definition_file", "")).strip()
    if not def_file:
        return edits
    file_path = Path(def_file)
    if not file_path.is_file():
        return edits
    code_blocks = re.findall(r"```(?:cpp|c\+\+|cc|cxx)?\s*\n(.*?)```", analysis_text or "", re.DOTALL)
    if not code_blocks:
        return edits
    seen: Set[str] = set()
    for block in code_blocks:
        for m in re.finditer(r"^\s*#\s*include\s*(<[^>\n]+>)\s*$", block, re.MULTILINE):
            header = m.group(1).strip()
            if not header or header in seen:
                continue
            seen.add(header)
            edits.append(
                {
                    "file": str(file_path),
                    "edit_type": "include_directive",
                    "include": f"#include {header}",
                    "reason": f"补充 {header} 头文件依赖",
                }
            )
    return edits


# =============================================================================
# JSON 解析（模块级公共函数）
# =============================================================================


def _extract_json_payload(raw_text: str) -> Dict[str, Any]:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("AI 未返回结构化修改计划")
    # 剥离 <thinking>...</thinking> 标签（某些模型会输出 extended thinking）
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.S).strip()
    if not text:
        raise ValueError("AI 仅返回了 thinking 内容，无结构化输出")
    # 尝试匹配 ```json ... ``` 围栏内的 JSON（对象或数组）
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if not fence_match:
        fence_match = re.search(r"```(?:json)?\s*(\[.*\])\s*```", text, re.S)
    if fence_match:
        text = fence_match.group(1)
    else:
        # 检测文本是否以数组开头（去除空白和可能的前缀文本）
        stripped = text.lstrip()
        arr_start = text.find("[")
        start = text.find("{")
        # 如果数组出现在对象之前（或文本以 [ 开头），优先提取数组
        if arr_start >= 0 and (start < 0 or arr_start < start):
            arr_end = text.rfind("]")
            if arr_end > arr_start:
                text = text[arr_start : arr_end + 1]
        elif start >= 0:
            end = text.rfind("}")
            if end > start:
                text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        fixed = re.sub(
            r'("(?:[^"\\]|\\.)*")',
            lambda m: m.group(0).replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"),
            text,
            flags=re.S,
        )
        payload = json.loads(fixed)
    # 如果 LLM 返回的是数组（edits 列表），包装为标准 fix_plan 格式
    if isinstance(payload, list):
        return {"summary": "", "edits": payload}
    if not isinstance(payload, dict):
        raise ValueError("结构化修改计划必须是 JSON 对象或数组")
    return payload


def parse_json_payload(raw_text: str) -> Dict[str, Any]:
    """安全解析 LLM 返回的 JSON fix plan 文本，失败时返回空 edits。"""
    try:
        payload = _extract_json_payload(raw_text)
        normalized_edits: List[Dict[str, Any]] = []
        for edit in payload.get("edits", []) if isinstance(payload.get("edits"), list) else []:
            if not isinstance(edit, dict):
                continue
            normalized_edits.append(
                {
                    "file": str(edit.get("file", "")),
                    "function_signature": str(edit.get("function_signature", "")),
                    "replacement_code": str(
                        edit.get("replacement_code")
                        or edit.get("replacement")
                        or edit.get("code")
                        or ""
                    ).strip(),
                    "reason": str(edit.get("reason", "")),
                }
            )
        payload = {"summary": str(payload.get("summary", "")), "edits": normalized_edits}
        if isinstance(payload.get("edits"), list):
            return payload
        return {"summary": str(payload.get("summary") or ""), "edits": []}
    except Exception as exc:
        preview = str(raw_text or "")[:200].replace("\n", " ")
        print(f"[AI Fix] JSON 解析失败: {exc}; 原始响应前200字符: {preview}", file=sys.stderr)
        return {"summary": "", "edits": []}


# =============================================================================
# CodeFixer（主 facade）
# =============================================================================


class CodeFixer:
    """AI Fix 主入口：从分析结果生成修复计划并应用到源码。"""

    def __init__(self, llm_adapter: Any = None, uaf_nullptr_guard_policy: str = "strict", file_context_tracker: Any = None):
        self._llm = llm_adapter
        self._uaf_policy = uaf_nullptr_guard_policy
        self.file_context_tracker = file_context_tracker

    def _try_extract_fix_plan_from_analysis(
        self,
        analysis_text: str,
        candidate_nodes: List[Dict[str, Any]],
        required_targets: List[Dict[str, str]],
    ) -> Optional[Dict[str, Any]]:
        """尝试直接从 Phase 4 的分析文本中提取修复代码构造 fix_plan。

        如果能为所有 required_targets（或至少有 candidate 匹配的目标）提取到
        有效的 replacement_code，则返回 fix_plan dict；否则返回 None 表示需要走 LLM。
        """
        if not analysis_text or not candidate_nodes:
            return None
        # 确定需要覆盖的目标
        if required_targets:
            targets = required_targets
        else:
            # required_targets 为空但存在 candidate_nodes：
            # 直接从 analysis_text 中所有代码块提取可用的修复函数
            blocks = _extract_code_blocks(analysis_text)
            if not blocks:
                return None
            # 尝试匹配 analysis 中提到的函数名（required_names 可能已提取出来）
            # 用第一个 candidate 的 file 作为默认文件路径
            default_file = candidate_nodes[0].get("file", "") if candidate_nodes else ""
            # 扫描所有代码块中的函数
            targets = []
            for code in blocks:
                # class/struct 骨架（常含 // ... 占位）不能整块替换；跳过该围栏，
                # 继续扫描后续独立的 Class::method 完整函数围栏。
                # 函数体内的 struct sigaction 不算骨架。
                if _is_type_skeleton_block(code):
                    continue
                # 尝试找到完整的函数定义
                func_pattern = re.compile(
                    r"(?:[\w:*&<>,\s]+?\s+)?(~?\w+(?:::\w+)*)\s*\([^)]*\)"
                    r"(?:\s*(?:const|override|noexcept|final))*"
                    r"\s*\{",
                    re.MULTILINE,
                )
                for m in func_pattern.finditer(code):
                    func_name = m.group(1)
                    # 排除控制语句
                    if func_name in ("if", "else", "for", "while", "switch", "do", "catch", "try"):
                        continue
                    # 排除初始化列表中的成员（如 m_name(x) 后跟 {）：
                    # 检查匹配位置前面同一行是否包含 ':' 或 ',' 表明是初始化列表
                    line_start_pos = code.rfind("\n", 0, m.start())
                    line_start_pos = line_start_pos + 1 if line_start_pos >= 0 else 0
                    prefix_on_line = code[line_start_pos:m.start()].strip()
                    if prefix_on_line.endswith(":") or prefix_on_line.endswith(","):
                        continue
                    # 排除成员变量名模式 (m_ 前缀在初始化列表中常见)
                    if func_name.startswith("m_"):
                        continue
                    matched_node = next(
                        (
                            node
                            for node in candidate_nodes
                            if signatures_match(
                                str(node.get("signature") or ""),
                                func_name if "(" in func_name else f"{func_name}()",
                            )
                        ),
                        None,
                    )
                    sig = str((matched_node or {}).get("signature") or "").strip() or (func_name + "(")
                    target_file = str((matched_node or {}).get("file") or default_file)
                    # 确认能从此块提取出完整函数
                    block = _extract_function_block_from_code(code, m.start())
                    if block and block.strip():
                        # 去重
                        if not any(t.get("function_signature") == sig for t in targets):
                            targets.append({"file": target_file, "function_signature": sig})
            if not targets:
                return None
        edits: List[Dict[str, Any]] = []
        missing_targets: List[str] = []
        for t in targets:
            sig = str(t.get("function_signature", ""))
            file_path = str(t.get("file", ""))
            replacement = _extract_replacement_from_analysis(analysis_text, sig)
            if not replacement:
                missing_targets.append(sig or file_path or "<unknown>")
                continue
            # 提取阶段只过滤占位/空代码，不做业务或语义门禁。
            if not replacement.strip():
                missing_targets.append(sig or file_path or "<unknown>")
                continue
            edits.append({
                "file": file_path,
                "function_signature": sanitize_function_signature(sig),
                "replacement_code": replacement,
                "reason": "extracted_from_analysis_text",
            })
        if not edits:
            return None
        if missing_targets:
            logger.warning(
                "[CodeFixer] analysis fast-path only partially matched targets: "
                f"{len(edits)} extracted, {len(missing_targets)} missing/invalid. "
                f"missing={missing_targets[:8]}"
            )
        return {"summary": "fast_path_from_analysis", "edits": edits}

    def generate_and_apply(
        self,
        result: Dict[str, Any],
        code_roots: List[str],
        report_dir: Optional[Path] = None,
        backup_original_sources: bool = True,
        tool_executor: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        tool_authorization: Any = None,
    ) -> FixResult:
        """完整流程：提取候选 → evidence gate → LLM 生成 plan → 应用。"""

        def _invoke_tool(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            data = dict(payload)
            if tool_authorization is not None:
                data["_runtime_authorization"] = tool_authorization
            if tool_executor is not None:
                return tool_executor(name, data)
            from tool_system.registry import ToolAndWorkflowRegistry
            from tool_system.tool_gateway import ToolExecutionGateway
            from services.policy import PolicyEngine

            tool = ToolAndWorkflowRegistry().get_tool(name)
            if tool is None:
                raise ValueError(f"tool not found: {name}")
            if isinstance(tool, type):
                tool = tool()
            gateway = ToolExecutionGateway(PolicyEngine())
            return gateway.execute(name, tool, data)
        if self._llm is None or not code_roots:
            return FixResult(
                success=False,
                error="缺少 LLM 或 code_root，无法应用 AI 修复",
            )
        analysis_text = str(result.get("analysis") or "").strip()
        code_context = result.get("code_context", {}) or {}
        parse_result = result.get("parse_result", {}) or {}
        if not analysis_text or not isinstance(code_context, dict):
            return FixResult(
                success=False,
                error="缺少 analysis/code_context，无法应用 AI 修复",
            )
        candidate_nodes = extract_candidate_nodes(code_context)
        session = result.get("context_session")
        session_dict = session if isinstance(session, dict) else None
        if not candidate_nodes:
            promoted = promote_read_file_function_candidates(
                code_context,
                analysis_text=analysis_text,
                context_session=session_dict,
                code_roots=list(code_roots or []),
            )
            if promoted:
                logger.info("[CodeFixer] 从已读源码补齐候选函数 %s 个", len(promoted))
            candidate_nodes = extract_candidate_nodes(code_context)
        if not candidate_nodes:
            completed = _complete_missing_repair_code(
                self._llm,
                analysis_text,
                context_session=session_dict,
                code_roots=list(code_roots or []),
            )
            if completed:
                analysis_text = analysis_text.rstrip() + "\n\n" + completed
                result["analysis"] = analysis_text
                logger.info("[CodeFixer] 分析未给出完整函数，已补一次修复代码")
                promote_read_file_function_candidates(
                    code_context,
                    analysis_text=analysis_text,
                    context_session=session_dict,
                    code_roots=list(code_roots or []),
                )
                candidate_nodes = extract_candidate_nodes(code_context)
        if not candidate_nodes:
            return FixResult(
                success=False,
                error="代码上下文未提供可替换的函数候选",
            )
        allowed, block_reason = graph_auto_fix_allowed(code_context)
        if not allowed:
            return FixResult(
                success=False,
                error=block_reason,
                skipped_reason="evidence_gate",
            )
        required_names = _extract_required_function_names_from_analysis(analysis_text)
        required_targets = _select_required_targets(candidate_nodes, required_names)
        required_targets = _ensure_owner_class_methods_in_targets(
            code_context, required_names, required_targets
        )

        try:
            # 统一走工具化链路：提取工具 -> 应用工具
            from tools.fix_code_extractor_tool import FixCodeExtractorTool
            from tools.fix_code_applier_tool import FixCodeApplierTool

            extract_out = _invoke_tool(
                "fix_code_extractor",
                {
                    "analysis_text": analysis_text,
                    "code_context": code_context,
                    "required_targets": required_targets,
                    "strict_required": False,
                },
            )
            if not bool(extract_out.get("success")):
                missing_required = (
                    extract_out.get("missing_required", [])
                    if isinstance(extract_out.get("missing_required"), list)
                    else []
                )
                return FixResult(
                    success=False,
                    error=(
                        "未能从分析输出文本中提取可执行修复代码（通常对应 06_ai_gen_res.md），已跳过自动改码。"
                        "请确保分析输出包含完整函数代码块（建议 ```cpp 围栏 + 准确函数签名）。"
                    ),
                    missing_required=[x for x in missing_required if isinstance(x, dict)],
                )
            from services.agent_schema import RepairPlan

            raw_fix_plan = extract_out.get("fix_plan")
            repair_plan, schema_violations = RepairPlan.from_mapping(raw_fix_plan)
            if repair_plan is None:
                logger.warning("[CodeFixer] rejected malformed repair plan: %s", schema_violations)
                return FixResult(
                    success=False,
                    error="修复计划未通过严格 schema 校验，已禁止自动改码",
                    skipped_reason="schema_violation",
                    schema_violations=schema_violations,
                )
            fix_plan = repair_plan.to_dict()
            _attach_missing_symbols(fix_plan, analysis_text)
            missing_required = (
                extract_out.get("missing_required", [])
                if isinstance(extract_out.get("missing_required"), list)
                else []
            )
            logger.info(
                "[CodeFixer] 工具链提取完成: extracted=%s required=%s",
                extract_out.get("extracted_count", 0),
                extract_out.get("required_target_count", 0),
            )
            # 调试：保存提取阶段完整输出（含 fix_plan 与覆盖率统计）
            if report_dir is not None:
                try:
                    report_dir.mkdir(parents=True, exist_ok=True)
                    extract_debug_path = report_dir / "07b_fix_extract_debug.json.json"
                    extract_debug_path.write_text(
                        json.dumps(extract_out, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
            apply_out = _invoke_tool(
                "fix_code_applier",
                {
                    "fix_plan": fix_plan,
                    "code_context": code_context,
                    "code_roots": code_roots,
                    "required_targets": required_targets,
                    "report_dir": str(report_dir) if report_dir is not None else "",
                    "backup_original_sources": backup_original_sources,
                },
            )
            apply_result = FixResult(
                success=bool(apply_out.get("success")),
                applied=apply_out.get("applied", []) if isinstance(apply_out.get("applied"), list) else [],
                error=str(apply_out.get("error", "") or "") or None,
                summary=str(apply_out.get("summary", "") or ""),
                edit_feedback=[
                    item.get("match_feedback") for item in (apply_out.get("applied") or [])
                    if isinstance(item, dict) and isinstance(item.get("match_feedback"), dict)
                ],
            )
            apply_result.missing_required = [
                x for x in missing_required if isinstance(x, dict)
            ]
            apply_result = evaluate_fix_apply_success(apply_result, code_context, fix_plan)
            if not apply_result.success:
                if not apply_result.applied:
                    edits = fix_plan.get("edits", []) if isinstance(fix_plan.get("edits"), list) else []
                    if not edits:
                        apply_result.error = (
                            "AI 未返回可执行的结构化 edits（空 edits）。"
                            "请在模型输出中确保返回严格 JSON，且 edits 至少包含 1 个包含 replacement_code 的条目。"
                        )
                    else:
                        apply_result.error = (
                            "AI 返回了 edits，但没有可应用项。"
                            "请检查 file/function_signature 是否与 candidate_nodes 精确匹配，"
                            "以及 replacement_code 是否为完整函数代码。"
                        )
                elif not apply_result.error:
                    first_err = next(
                        (
                            str(item.get("error"))
                            for item in apply_result.applied
                            if isinstance(item, dict) and item.get("error")
                        ),
                        "",
                    ).strip()
                    if first_err:
                        apply_result.error = f"未应用修改: {first_err}"
            apply_result.fix_plan = fix_plan
            apply_result.summary = str(fix_plan.get("summary", ""))
            return apply_result
        except Exception as exc:
            return FixResult(
                success=False,
                error=f"生成或应用 AI 修复计划失败: {exc}",
            )

    def apply_fix_plan(
        self,
        fix_plan: Dict[str, Any],
        candidate_nodes: List[Dict[str, Any]],
        code_roots: List[str],
        report_dir: Optional[Path] = None,
        backup_original_sources: bool = True,
        required_targets: Optional[List[Dict[str, str]]] = None,
        code_context: Optional[Dict[str, Any]] = None,
        tool_executor: Optional[Any] = None,
        file_context_tracker: Any = None,
    ) -> FixResult:
        """仅应用已有 plan（不调 LLM）。"""
        from services.agent_schema import RepairPlan

        repair_plan, schema_violations = RepairPlan.from_mapping(fix_plan)
        if repair_plan is None:
            return FixResult(
                success=False,
                error="修复计划未通过严格 schema 校验，已禁止自动改码",
                skipped_reason="schema_violation",
                schema_violations=schema_violations,
            )
        fix_plan = repair_plan.to_dict()
        edits = fix_plan["edits"]
        planned_members = _members_introduced_by_declaration_edits(edits)
        applied: List[Dict[str, Any]] = []
        tracker = file_context_tracker or self.file_context_tracker
        for edit_index, edit in enumerate(edits):
            if not isinstance(edit, dict):
                continue
            edit_type = str(edit.get("edit_type", "")).strip()
            edit_id = str(edit.get("edit_id") or f"edit_{edit_index + 1}")
            target = str(edit.get("file") or "")
            if target and tracker is not None:
                stale = tracker.check_stale(target)
                if stale.get("stale"):
                    applied.append({"edit_id": edit_id, "file": str(Path(target).resolve()),
                                    "status": "stale", "failure_class": "stale_file",
                                    "error": "文件在读取后发生变化，已禁止覆盖写入",
                                    "match_feedback": stale})
                    continue

            # ========== include 指令插入（include_directive）==========
            if edit_type == "include_directive":
                file_path = Path(str(edit.get("file", ""))).resolve()
                include_line = str(edit.get("include", "")).strip()
                reason = str(edit.get("reason", ""))
                record: Dict[str, Any] = {
                    "file": str(file_path),
                    "edit_type": "include_directive",
                    "include": include_line,
                    "reason": reason,
                    "status": "skipped",
                }
                if not _is_within_code_roots(file_path, code_roots):
                    record["error"] = "目标文件不在 code_root 范围内"
                    applied.append(record)
                    continue
                if not file_path.exists():
                    record["error"] = "目标文件不存在"
                    applied.append(record)
                    continue
                if not include_line.startswith("#include "):
                    record["error"] = "include 指令格式无效"
                    applied.append(record)
                    continue
                file_encoding = "utf-8"
                try:
                    original, file_encoding = _try_read_source_raw(file_path)
                except (UnicodeDecodeError, LookupError, OSError):
                    record["error"] = "目标文件编码无法识别（非 UTF-8/GBK）"
                    applied.append(record)
                    continue
                include_pat = re.compile(r"^\s*" + re.escape(include_line) + r"\s*$", re.MULTILINE)
                if include_pat.search(original):
                    record["error"] = "include 已存在，无需修改"
                    applied.append(record)
                    continue
                eol = _source_newline_style(original)
                lines = original.splitlines()
                insert_at = 0
                for idx, line in enumerate(lines):
                    if re.match(r"^\s*#\s*include\b", line):
                        insert_at = idx + 1
                lines.insert(insert_at, include_line)
                new_source = _convert_newlines(
                    "\n".join(lines) + (eol if original.endswith(("\n", "\r\n")) else ""),
                    eol,
                )
                try:
                    _write_text_raw(file_path, new_source, encoding=file_encoding)
                except (UnicodeEncodeError, UnicodeDecodeError):
                    _write_text_raw(file_path, new_source, encoding="utf-8")
                if report_dir is not None and backup_original_sources:
                    backup_root = report_dir / "original_sources"
                    containing_root = _find_containing_code_root(file_path, code_roots)
                    if containing_root:
                        backup_path = backup_root / file_path.relative_to(containing_root)
                        backup_path.parent.mkdir(parents=True, exist_ok=True)
                        if not backup_path.exists():
                            _write_text_raw(backup_path, original, encoding="utf-8")
                        record["backup_path"] = str(backup_path)
                record["status"] = "applied"
                record["replaced_preview"] = include_line
                applied.append(record)
                continue

            # ========== 成员声明替换（member_declaration）==========
            if edit_type == "member_declaration":
                file_path = Path(str(edit.get("file", ""))).resolve()
                old_text = str(edit.get("old_text", "")).strip()
                new_text = str(edit.get("new_text", "")).strip()
                reason = str(edit.get("reason", ""))
                record: Dict[str, Any] = {
                    "file": str(file_path),
                    "edit_type": "member_declaration",
                    "old_text": old_text,
                    "new_text": new_text,
                    "reason": reason,
                    "status": "skipped",
                }
                if not _is_within_code_roots(file_path, code_roots):
                    record["error"] = "目标文件不在 code_root 范围内"
                    applied.append(record)
                    continue
                if not file_path.exists():
                    record["error"] = "目标文件不存在"
                    applied.append(record)
                    continue
                if not old_text or not new_text:
                    record["error"] = "old_text 或 new_text 为空"
                    applied.append(record)
                    continue
                if old_text == new_text:
                    record["error"] = "old_text 与 new_text 相同，无需修改"
                    applied.append(record)
                    continue
                file_encoding = "utf-8"
                try:
                    original, file_encoding = _try_read_source_raw(file_path)
                except (UnicodeDecodeError, LookupError, OSError):
                    record["error"] = "目标文件编码无法识别（非 UTF-8/GBK）"
                    applied.append(record)
                    continue
                if old_text not in original:
                    record["error"] = "old_text 未能在文件中精确匹配"
                    record["match_feedback"] = _match_feedback(original, old_text, file_path=str(file_path))
                    applied.append(record)
                    continue
                eol = _source_newline_style(original)
                new_source = _convert_newlines(
                    original.replace(old_text, _convert_newlines(new_text, eol), 1),
                    eol,
                )
                try:
                    _write_text_raw(file_path, new_source, encoding=file_encoding)
                except (UnicodeEncodeError, UnicodeDecodeError):
                    _write_text_raw(file_path, new_source, encoding="utf-8")
                if report_dir is not None and backup_original_sources:
                    backup_root = report_dir / "original_sources"
                    containing_root = _find_containing_code_root(file_path, code_roots)
                    if containing_root:
                        backup_path = backup_root / file_path.relative_to(containing_root)
                        backup_path.parent.mkdir(parents=True, exist_ok=True)
                        if not backup_path.exists():
                            _write_text_raw(backup_path, original, encoding="utf-8")
                        record["backup_path"] = str(backup_path)
                record["status"] = "applied"
                record["replaced_preview"] = new_text
                init_name = str(edit.get("sync_init_name") or "").strip()
                init_type = str(edit.get("sync_init_type") or "").strip()
                if init_name and init_type:
                    init_note = _insert_mutex_create_beside_sibling(
                        file_path,
                        init_name,
                        init_type,
                        code_roots,
                    )
                    if init_note:
                        record["init_note"] = init_note
                applied.append(record)
                continue

            if edit_type == "symbol_insertion":
                applied.append(_apply_symbol_insertion_edit(
                    edit, code_roots, report_dir, backup_original_sources, edit_id,
                ))
                continue

            # ========== 函数替换（默认类型）==========
            file_path = Path(str(edit.get("file", ""))).resolve()
            signature = sanitize_function_signature(str(edit.get("function_signature", "")))
            replacement_code = sanitize_replacement_code(
                str(edit.get("replacement_code", "")).strip("\n"),
                signature,
            )
            reason = str(edit.get("reason", ""))
            record = {
                "file": str(file_path),
                "function_signature": signature,
                "reason": reason,
                "status": "skipped",
            }
            if not _signature_is_plausible_for_edit(signature):
                record["error"] = "function_signature 无效（含宏污染或不完整）"
                applied.append(record)
                continue
            if not replacement_code:
                record["error"] = "replacement_code 为空"
                applied.append(record)
                continue
            if not _is_valid_replacement_code(replacement_code):
                record["error"] = "replacement_code 无效（含孤立括号或占位符）"
                applied.append(record)
                continue
            if not _replacement_signature_compatible(signature, replacement_code):
                record["error"] = "replacement_code 的类名/函数名与目标签名不一致，已拒绝写入"
                applied.append(record)
                continue
            prompt_block = edit_allowed_by_prompt_complete_body(signature, code_context)
            if prompt_block:
                record["error"] = prompt_block
                applied.append(record)
                continue
            node = _find_candidate_node_for_edit(candidate_nodes, file_path, signature)
            if node is None:
                node = _build_candidate_node_from_current_source(
                    file_path, signature, tool_executor=tool_executor,
                )
                if node is None:
                    record["error"] = "目标函数不在本次代码上下文候选列表中"
                    applied.append(record)
                    continue
            if not _is_within_code_roots(file_path, code_roots):
                record["error"] = "目标文件不在 code_root 范围内"
                applied.append(record)
                continue
            if not file_path.exists():
                record["error"] = "目标文件不存在"
                applied.append(record)
                continue
            file_encoding = "utf-8"
            try:
                original, file_encoding = _try_read_source_raw(file_path)
            except (UnicodeDecodeError, LookupError, OSError):
                record["error"] = "目标文件编码无法识别（非 UTF-8/GBK）"
                applied.append(record)
                continue
            snippet_text = "\n".join(node["snippet"])
            # 与原文换行对齐，避免 CRLF 文件中 snippet 用 LF 拼出后匹配失败
            eol_probe = _source_newline_style(original)
            if eol_probe == "\r\n" and snippet_text:
                snippet_text = _convert_newlines(snippet_text, "\r\n")
            new_text_content = original
            old_block: Optional[str] = _extract_current_function_block(
                file_path, signature, node, original, tool_executor=tool_executor,
            )
            if old_block:
                new_text_content = original.replace(old_block, replacement_code, 1)
            elif snippet_text in original:
                replace_pos = original.find(snippet_text)
                line_start = original.rfind("\n", 0, replace_pos)
                line_start = line_start + 1 if line_start >= 0 else 0
                prefix = original[line_start:replace_pos]
                if prefix.strip() and ";" not in prefix and replacement_code.lstrip().startswith(prefix.strip()):
                    first_non_ws = 0
                    while first_non_ws < len(prefix) and prefix[first_non_ws] in (" ", "\t"):
                        first_non_ws += 1
                    extended_snippet = prefix[first_non_ws:] + snippet_text
                    if extended_snippet in original:
                        old_block = extended_snippet
                        new_text_content = original.replace(extended_snippet, replacement_code, 1)
                    else:
                        old_block = snippet_text
                        new_text_content = original.replace(snippet_text, replacement_code, 1)
                else:
                    old_block = snippet_text
                    new_text_content = original.replace(snippet_text, replacement_code, 1)
            else:
                new_text_content, old_block = CodeFixer.replace_function_block(original, signature, replacement_code)
            if old_block is None or new_text_content == original:
                record["error"] = "未能在源码中定位待替换函数"
                record["match_feedback"] = _match_feedback(
                    original,
                    snippet_text or signature,
                    file_path=str(file_path),
                )
                applied.append(record)
                continue
            if not _extracted_block_matches_signature(old_block, signature):
                record["error"] = "定位到的函数块与目标签名不匹配（可能跨函数误截断）"
                record["match_feedback"] = _match_feedback(
                    original,
                    old_block or signature,
                    file_path=str(file_path),
                )
                applied.append(record)
                continue
            if _normalize_code_for_equivalence(old_block) == _normalize_code_for_equivalence(replacement_code):
                record["error"] = "replacement_code 与原函数等价（仅空白或格式差异），已跳过写入"
                applied.append(record)
                continue
            guard_err = _check_null_guard_only_patch(
                old_block, replacement_code, code_context=code_context
            )
            if guard_err:
                record["error"] = guard_err
                applied.append(record)
                continue
            allowed_members = _collect_allowed_member_names(original, code_context)
            allowed_members.update(planned_members)
            replacement_code, member_err = _validate_and_fix_member_references(
                original,
                replacement_code,
                allowed_members=allowed_members,
                allow_fuzzy_rewrite=False,
            )
            if member_err:
                record["error"] = member_err
                applied.append(record)
                continue
            sync_err = _validate_sync_api_consistency(original, replacement_code)
            if sync_err:
                record["error"] = sync_err
                applied.append(record)
                continue
            forbidden_err = is_forbidden_patch(
                signature,
                replacement_code,
                uaf_nullptr_guard_policy=str(self._uaf_policy or "strict"),
            )
            if forbidden_err:
                record["error"] = forbidden_err
                applied.append(record)
                continue
            destructive_err = _check_destructive_body_shrink(old_block, replacement_code)
            if destructive_err:
                record["error"] = destructive_err
                applied.append(record)
                continue
            method_err = _validate_method_calls_in_replacement(
                original,
                replacement_code,
                old_block=old_block,
            )
            if method_err:
                record["error"] = method_err
                applied.append(record)
                continue
            # 仅拦截「同名转发 + 整函数入口判空」；其余质量由提示词与人工 review 控制。
            eol = _source_newline_style(original)
            replacement_code = _adapt_replacement_indent(replacement_code, old_block or "")
            replacement_code = _align_leading_newline(old_block or "", replacement_code)
            simple_name = _extract_simple_function_name(signature)
            if simple_name and old_block and _source_defines_function(replacement_code, simple_name):
                old_block = _expand_block_to_declaration(original, old_block, simple_name)
            replace_at = original.find(old_block or "")
            if replace_at >= 0:
                replacement_code = _ensure_newline_before_replacement(
                    original[:replace_at],
                    replacement_code,
                )
            replacement_code = _convert_newlines(replacement_code, eol)
            new_text_content = _convert_newlines(
                original.replace(old_block, replacement_code, 1),
                eol,
            )
            logger.info("[CodeFixer] 写入文件: %s (diff=%+d bytes, encoding=%s)",
                        file_path, len(new_text_content) - len(original), file_encoding)
            try:
                _write_text_raw(file_path, new_text_content, encoding=file_encoding)
            except (UnicodeEncodeError, UnicodeDecodeError):
                # replacement_code 含原编码无法表示的字符，升级为 UTF-8 写入
                _write_text_raw(file_path, new_text_content, encoding="utf-8")
                file_encoding = "utf-8"
            if report_dir is not None and backup_original_sources:
                backup_root = report_dir / "original_sources"
                containing_root = _find_containing_code_root(file_path, code_roots)
                if containing_root is None:
                    record["error"] = "目标文件未命中任何 code_root，无法回写备份"
                    applied.append(record)
                    continue
                backup_path = backup_root / file_path.relative_to(containing_root)
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                if not backup_path.exists():
                    _write_text_raw(backup_path, original, encoding=file_encoding)
                record["backup_path"] = str(backup_path)
            record["status"] = "applied"
            record["replaced_preview"] = replacement_code
            # Keep the actual on-disk change as a reviewable unified diff. The
            # preview remains for backward compatibility with older reports.
            record["diff"] = "".join(difflib.unified_diff(
                original.splitlines(keepends=True),
                new_text_content.splitlines(keepends=True),
                fromfile=str(file_path), tofile=str(file_path),
            ))
            record["actual_fingerprint"] = hashlib.sha256(
                new_text_content.encode("utf-8", errors="replace")
            ).hexdigest()[:20]
            applied.append(record)

        for index, item in enumerate(applied):
            if isinstance(item, dict):
                item.setdefault("edit_id", f"edit_{index + 1}")
                item.setdefault("status", "failed")
        success = any(item.get("status") == "applied" for item in applied)
        return FixResult(
            success=success,
            applied=applied,
            summary=str(fix_plan.get("summary", "")) if isinstance(fix_plan, dict) else "",
            edit_feedback=[
                item.get("match_feedback") for item in applied
                if isinstance(item, dict) and isinstance(item.get("match_feedback"), dict)
            ],
            edits=applied,
        )

    @staticmethod
    def replace_function_block(source: str, signature: str, replacement_code: str) -> Tuple[str, Optional[str]]:
        """核心替换算法：在源码中定位函数并替换为新代码。返回 (new_source, old_block)。"""
        signature = sanitize_function_signature(signature)
        replacement_code = sanitize_replacement_code(replacement_code, signature)
        sig_index = source.find(signature)
        if sig_index > 0 and (source[sig_index - 1].isalnum() or source[sig_index - 1] == '_'):
            sig_index = -1
        # 验证：确保匹配位置是函数定义，而非初始化列表中的成员初始化
        if sig_index >= 0:
            line_start_pos = source.rfind("\n", 0, sig_index)
            line_start_pos = line_start_pos + 1 if line_start_pos >= 0 else 0
            prefix_text = source[line_start_pos:sig_index].strip()
            # 初始化列表特征：同行前面含 ':' 或 ',' 后跟若干成员初始化
            # 如 ": m_state(None), m_cancel(false), m_name(name)"
            if prefix_text and (prefix_text[-1] in ":," or
                                re.search(r"[:,]\s*\w+\s*\([^)]*\)\s*,?\s*$", prefix_text)):
                sig_index = -1
        if sig_index < 0:
            norm_sig = _normalize_signature_key(signature)
            if norm_sig:
                for m in re.finditer(
                    r"(?:(?:template\s*<[^>]+>\s*)|[\w:\<\>\s\*&~]+)?"
                    r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)::\s*([~]?[A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(",
                    source,
                ):
                    owner = m.group(1)
                    func_name = m.group(2)
                    probe = f"{owner}::{func_name}"
                    if probe in norm_sig or norm_sig in probe:
                        sig_index = m.start()
                        break
        if sig_index < 0:
            owner = ""
            m_owner = re.search(r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)::\s*([~]?[A-Za-z_]\w*)\s*[\(<]", signature)
            if m_owner:
                owner = m_owner.group(1)
            func_name = _extract_simple_function_name(signature)
            idx_candidates: List[int] = []
            if owner and func_name:
                pattern = re.compile(
                    rf"{re.escape(owner)}\s*::\s*{re.escape(func_name)}\s*(?:<[^>]*>)?\s*\(",
                    re.M,
                )
                idx_candidates = [m.start() for m in pattern.finditer(source)]
            if not idx_candidates and func_name:
                word_boundary = r"\b" if not func_name.startswith("~") else r"(?<!\w)"
                pattern2 = re.compile(
                    rf"{word_boundary}{re.escape(func_name)}(?!\w)\s*(?:<[^>]*>)?\s*\(",
                    re.M,
                )
                idx_candidates = [m.start() for m in pattern2.finditer(source)]
            if idx_candidates:
                sig_index = idx_candidates[0]
        if sig_index < 0:
            return source, None
        # 签名写成 foo() 而源码定义是 foo(void) 时，精确匹配会落到调用语句上，改用真正的定义位置。
        simple_name = _extract_simple_function_name(signature)
        if simple_name and not _is_function_definition_at(source, sig_index):
            definition_at = _first_function_definition_index(source, simple_name)
            if definition_at >= 0:
                sig_index = definition_at
        # 最终验证：确保匹配位置是函数定义，而非初始化列表中的成员初始化
        # 初始化列表特征：同行前面含 ':' 或 ',' 后跟若干 ident(expr) 模式
        _init_line_start = source.rfind("\n", 0, sig_index)
        _init_line_start = _init_line_start + 1 if _init_line_start >= 0 else 0
        _init_prefix = source[_init_line_start:sig_index].strip()
        if _init_prefix and (
            _init_prefix[-1] in ":,"
            or re.search(r"[:,]\s*\w+\s*\([^)]*\)\s*,?\s*$", _init_prefix)
        ):
            return source, None
        # 新代码自带完整定义时，切点退到声明起点，避免留下原来的属性宏和返回类型。
        func_name_for_cut = _extract_simple_function_name(signature)
        replacement_defines = bool(
            func_name_for_cut and _source_defines_function(replacement_code, func_name_for_cut)
        )
        if replacement_defines:
            sig_index = _declaration_cut_index(source, sig_index)
        else:
            line_start = source.rfind("\n", 0, sig_index)
            line_start = line_start + 1 if line_start >= 0 else 0
            prefix_before_sig = source[line_start:sig_index]
            if prefix_before_sig.strip() and ";" not in prefix_before_sig:
                prefix_text = prefix_before_sig.strip()
                replacement_stripped = replacement_code.lstrip()
                if replacement_stripped.startswith(prefix_text):
                    first_non_ws = line_start
                    while first_non_ws < sig_index and source[first_non_ws] in (" ", "\t"):
                        first_non_ws += 1
                    sig_index = first_non_ws
        brace_start = source.find("{", sig_index)
        if brace_start < 0:
            return source, None
        depth = 0
        end_index = -1
        for idx in range(brace_start, len(source)):
            ch = source[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_index = idx
                    break
        if end_index < 0:
            return source, None
        # 安全检查：防止 replacement_code 开头与 source[:sig_index] 尾部产生拼凑/重叠
        line_start_for_check = source.rfind("\n", 0, sig_index)
        line_start_for_check = line_start_for_check + 1 if line_start_for_check >= 0 else 0
        prefix_on_line = source[line_start_for_check:sig_index]
        if prefix_on_line.strip():
            repl_first_line = replacement_code.split("\n", 1)[0].lstrip()
            prefix_stripped = prefix_on_line.strip()
            overlap_detected = False
            for overlap_len in range(min(len(prefix_stripped), len(repl_first_line)), 0, -1):
                if prefix_stripped.endswith(repl_first_line[:overlap_len]):
                    overlap_detected = True
                    break
            if overlap_detected:
                if repl_first_line.startswith(prefix_stripped):
                    first_non_ws = line_start_for_check
                    while first_non_ws < sig_index and source[first_non_ws] in (" ", "\t"):
                        first_non_ws += 1
                    sig_index = first_non_ws
                else:
                    return source, None
        old_block = source[sig_index : end_index + 1]
        patched = _ensure_newline_before_replacement(source[:sig_index], replacement_code)
        return source[:sig_index] + patched + source[end_index + 1 :], old_block
