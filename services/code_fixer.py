"""AI Fix 服务：从 AI 分析结果生成修复计划并应用到源码。

主要组件：
- CodeFixer: 主 facade，提供 generate_and_apply / apply_fix_plan
- FixResult: 结果数据类
- 模块级公共函数：extract_candidate_nodes, graph_auto_fix_allowed, signatures_match 等
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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
        return d


# =============================================================================
# 纯工具函数（模块级）
# =============================================================================


def _extract_simple_function_name(signature: str) -> str:
    s = str(signature or "").strip()
    if not s:
        return ""
    paren_idx = s.find("(")
    head = s[:paren_idx].strip() if paren_idx > 0 else s
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


def _normalize_signature_key(signature: str) -> str:
    s = re.sub(r"\s+", " ", str(signature or "").strip())
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
    """校验从源码提取的函数块：必须以目标签名开头，且不能夹带其它顶层成员定义。"""
    block = str(block or "").strip()
    signature = str(signature or "").strip()
    if not block or not signature:
        return False
    func_name = _extract_simple_function_name(signature)
    owner = ""
    m_owner = re.search(
        r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)::\s*([~]?[A-Za-z_]\w*)\s*[\(<]",
        signature,
    )
    if m_owner:
        owner = m_owner.group(1)
        func_name = m_owner.group(2) or func_name
    if not func_name:
        return False
    non_empty = [
        ln
        for ln in block.splitlines()
        if ln.strip() and not ln.strip().startswith("//")
    ]
    if not non_empty:
        return False
    first = non_empty[0].strip()
    target_head = f"{owner}::{func_name}" if owner else func_name
    if owner:
        if target_head not in first:
            return False
    elif not re.search(
        rf"(?:^|\s){re.escape(func_name)}\s*(?:<[^>]*>)?\s*\(",
        first,
    ):
        return False
    if owner:
        extra_defs = re.findall(
            rf"(?:^|\n)\s*(?:[\w:\<\>\*&~\s]+\s+)?{re.escape(owner)}\s*::\s*"
            rf"(?!{re.escape(func_name)}\b)([~]?[A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(",
            block,
            flags=re.M,
        )
        if extra_defs:
            return False
    return True


def _extract_owner_and_name(signature: str) -> Tuple[str, str]:
    """从签名提取 (类名, 函数名)；无类作用域时类名为空。"""
    text = str(signature or "")
    m_owner = re.search(
        r"([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)::\s*([~]?[A-Za-z_]\w*)\s*[\(<]",
        text,
    )
    if m_owner:
        return m_owner.group(1), m_owner.group(2)
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


def is_forbidden_patch(signature: str, replacement_code: str, uaf_nullptr_guard_policy: str = "strict") -> Optional[str]:
    """拒绝无效/症状级补丁（通用模式，非业务硬编码）。"""
    code = str(replacement_code or "")
    if (
        re.search(r"\bthis\s*[!=]=\s*(nullptr|NULL|V_NULL|0)\b", code)
        or re.search(r"\b(nullptr|NULL|V_NULL|0)\s*[!=]=\s*this\b", code)
        or re.search(r"\bif\s*\(\s*!\s*this\s*\)", code)
    ):
        sig = str(signature or "")
        if re.search(r"\)\s*const\b", sig) or (
            "::" in sig and not sig.strip().startswith("static")
        ):
            policy = uaf_nullptr_guard_policy
            if policy in ("strict", "balanced"):
                return "成员函数内 this/nullptr 防护不能作为 UAF 根因修复（policy={})".format(policy)
    return None


def _source_newline_style(source: str) -> str:
    return "\r\n" if "\r\n" in str(source or "") else "\n"


def _convert_newlines(text: str, style: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if style == "\r\n":
        return normalized.replace("\n", "\r\n")
    return normalized


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
        from tools.snippet_extractor_tool import SnippetExtractorTool

        out = SnippetExtractorTool().execute(
            {
                "file_path": str(file_path),
                "line_number": line_no,
                "function_name": _extract_simple_function_name(signature),
                "max_code_length": 0,
            }
        )
        snippet = out.get("snippet") if isinstance(out, dict) else None
        if bool(out.get("is_complete_function")) and isinstance(snippet, list) and snippet:
            block = "\n".join(str(x) for x in snippet).strip("\n")
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
    )
    if any(mark in s for mark in phrase_markers):
        return True
    # "..." 仅在非字符串字面量且非注释上下文中视为占位
    for m in re.finditer(r"\.{3}", s):
        start = m.start()
        line_start = s.rfind("\n", 0, start)
        prefix = s[line_start + 1 : start] if line_start >= 0 else s[:start]
        # 在引号内（如 "触发空指针崩溃..."）→ 跳过
        in_string = prefix.count('"') % 2 == 1
        if in_string:
            continue
        # 在 // 单行注释内 → 跳过（注释中的 ... 不算占位符）
        dslash = prefix.rfind("//")
        if dslash >= 0:
            # 确保 // 不在引号内
            before_slash = prefix[:dslash]
            if before_slash.count('"') % 2 == 0:
                continue
        # 在 /* */ 块注释内 → 跳过
        last_open = prefix.rfind("/*")
        last_close = prefix.rfind("*/")
        if last_open >= 0 and last_open > last_close:
            continue
        return True
    return False


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
    for root in code_roots:
        try:
            path.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def _validate_and_fix_member_references(original_source: str, replacement: str) -> Tuple[str, Optional[str]]:
    """检查并修正 replacement 中的成员变量引用。
    返回 (修正后的 replacement, 错误描述或 None)。
    如果幻觉变量名有唯一近似匹配（子串关系或编辑距离<=3），自动替换为正确名称。
    """
    orig_members = set(re.findall(r'\bm_\w+', original_source))
    repl_members = set(re.findall(r'\bm_\w+', replacement))
    novel = repl_members - orig_members
    if not novel:
        return replacement, None
    fixed = replacement
    truly_novel = set()
    for m in novel:
        candidates = []
        for om in orig_members:
            # 子串关系（短名可能是长成员名的前缀）
            if m in om or om in m:
                candidates.append(om)
                continue
            # 简单编辑距离（Levenshtein 近似）
            if abs(len(m) - len(om)) > 4:
                continue
            # 使用 SequenceMatcher 比率
            from difflib import SequenceMatcher
            ratio = SequenceMatcher(None, m, om).ratio()
            if ratio >= 0.7:
                candidates.append(om)
        if len(candidates) == 1:
            fixed = re.sub(rf'\b{re.escape(m)}\b', candidates[0], fixed)
        elif not candidates:
            truly_novel.add(m)
    if truly_novel:
        return replacement, f"成员变量 {', '.join(sorted(truly_novel))} 在源文件中不存在（可能是 LLM 幻觉），已跳过"
    return fixed, None


# 改码允许调用的通用宏/关键字（非类成员方法；成员方法须出现在原文件）
_ALLOWED_CALLEE_NAMES = frozenset(
    {
        "NAVI_CHECK_RESULT_VAL",
        "NAVI_FUNC_ENTERLEAVE_CHECK",
        "V_ASSERT",
        "V_NULL",
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


def _validate_method_calls_in_replacement(original: str, replacement: str) -> Optional[str]:
    """仅拒绝配置中明确禁止的方法；不以“原文件未出现”作为硬门禁。

    修复代码可能合理引入标准库/已有类型的新方法调用，例如将 volatile 改为
    std::atomic 后新增 load()/store()。这类场景无法靠“原文件是否出现过”判断，
    过度拦截会误伤有效修复。
    """
    forbidden = _forbidden_method_error(replacement)
    if forbidden:
        return forbidden
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
        r'\s*if\s*\(\s*(\w+)\s*==\s*(?:V_NULL|nullptr|NULL|0)\s*\)\s*\{[^}]*return[^}]*\}',
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
        r'(\s*if\s*\(\s*!?\s*\w+\s*(?:==\s*(?:V_NULL|nullptr|NULL|0)\s*)?\)\s*\{[^}]*return[^}]*\}\s*\n?)',
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
    try:
        line_no = int(cs.get("crash_line_number") or 0)
    except (TypeError, ValueError):
        line_no = 0
    path = Path(file_path)
    if not path.is_file() or line_no <= 0:
        return []
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
            "signature": signature.rstrip("{").rstrip(),
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


def _extract_required_function_names_from_analysis(analysis_text: str) -> List[str]:
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
            names.append(m.group(1).strip())
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
                names.append(name_candidate.strip())
    dedup: List[str] = []
    for n in names:
        if n and n not in dedup:
            dedup.append(n)
    return dedup


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
) -> Optional[Dict[str, Any]]:
    """当 graph.nodes 未包含目标函数时，直接从当前源码重提取一个临时候选节点。"""
    if not file_path.is_file() or not signature:
        return None
    try:
        from tools.snippet_extractor_tool import SnippetExtractorTool

        out = SnippetExtractorTool().execute(
            {
                "file_path": str(file_path),
                "line_number": 1,
                "function_name": _extract_simple_function_name(signature),
                "max_code_length": 0,
            }
        )
    except Exception:
        return None
    snippet = out.get("snippet") if isinstance(out, dict) else None
    if not (isinstance(snippet, list) and snippet):
        return None
    return {
        "type": "function",
        "file": str(file_path),
        "signature": signature,
        "snippet": [str(x) for x in snippet],
        "snippet_start_line": out.get("snippet_start_line"),
        "snippet_end_line": out.get("snippet_end_line"),
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


def _extract_code_blocks(analysis_text: str) -> List[str]:
    """从 analysis_text 中提取 C++ 代码围栏块；若无围栏则尝试后备提取。"""
    blocks: List[str] = []
    lines = str(analysis_text or "").splitlines()
    in_block = False
    cur: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_block:
            if re.match(r"^```(?:cpp|c\+\+|cc|cxx)?\s*$", stripped, re.I):
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
    """从函数代码块提取函数签名文本（到 '{' 之前）。"""
    text = str(block or "").strip()
    if not text:
        return ""
    brace = text.find("{")
    if brace < 0:
        return ""
    sig = text[:brace].strip()
    sig = re.sub(r"\s+", " ", sig)
    return sig


def _extract_signature_function_name(signature: str) -> str:
    text = str(signature or "")
    matches = re.findall(r"([~]?[A-Za-z_]\w*)\s*\(", text)
    for name in reversed(matches):
        if name not in {"if", "for", "while", "switch", "catch"}:
            return name
    return ""


def _target_is_destructor(signature: str) -> bool:
    return bool(re.search(r"(?:^|::)\s*~[A-Za-z_]\w*\s*\(", str(signature or "")))


def _replacement_signature_compatible(target_signature: str, replacement_code: str) -> bool:
    """确认 replacement 是目标函数的完整定义，且没有改写函数签名。"""
    block_sig = _extract_block_signature(replacement_code)
    if not block_sig:
        return False
    stripped = block_sig.strip()
    if ";" in stripped or stripped.startswith("//") or stripped.startswith("/*"):
        return False
    target_name = _extract_signature_function_name(target_signature)
    block_name = _extract_signature_function_name(block_sig)
    if target_name and block_name and target_name != block_name:
        return False
    target_owner, _ = _extract_owner_and_name(target_signature)
    block_owner, _ = _extract_owner_and_name(block_sig)
    if target_owner and block_owner:
        t_tail = target_owner.split("::")[-1]
        b_tail = block_owner.split("::")[-1]
        if target_owner != block_owner and t_tail != b_tail:
            return False
    if _target_is_destructor(target_signature):
        # 析构函数没有返回类型；`VBool Class::~Class()` 或 `VBool ~Class()` 都不可直接替换。
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
                if (
                    block
                    and _extract_simple_function_name(block) == simple_name
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


def _extract_member_declaration_edits(
    analysis_text: str,
    code_context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """从AI分析文本的代码块中提取成员变量声明变更，生成 member_declaration 类型的 edit。"""
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

    def __init__(self, llm_adapter: Any = None, uaf_nullptr_guard_policy: str = "strict"):
        self._llm = llm_adapter
        self._uaf_policy = uaf_nullptr_guard_policy

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
                # 如果代码块包含 class/struct 定义，说明 LLM 输出了整个类，
                # fast-path 无法从类中精确提取单个函数替换，放弃快速路径
                if re.search(r"^\s*(?:class|struct)\s+\w+", code, re.MULTILINE):
                    return None
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
    ) -> FixResult:
        """完整流程：提取候选 → evidence gate → LLM 生成 plan → 应用。"""
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

            extractor = FixCodeExtractorTool()
            extract_out = extractor.execute(
                {
                    "analysis_text": analysis_text,
                    "code_context": code_context,
                    "required_targets": required_targets,
                    "strict_required": False,
                }
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
            fix_plan = extract_out.get("fix_plan") if isinstance(extract_out.get("fix_plan"), dict) else {"summary": "", "edits": []}
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
            applier = FixCodeApplierTool()
            apply_out = applier.execute(
                {
                    "fix_plan": fix_plan,
                    "code_context": code_context,
                    "code_roots": code_roots,
                    "required_targets": required_targets,
                    "report_dir": str(report_dir) if report_dir is not None else "",
                    "backup_original_sources": backup_original_sources,
                }
            )
            apply_result = FixResult(
                success=bool(apply_out.get("success")),
                applied=apply_out.get("applied", []) if isinstance(apply_out.get("applied"), list) else [],
                error=str(apply_out.get("error", "") or "") or None,
                summary=str(apply_out.get("summary", "") or ""),
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
    ) -> FixResult:
        """仅应用已有 plan（不调 LLM）。"""
        edits = fix_plan.get("edits", []) if isinstance(fix_plan, dict) else []
        applied: List[Dict[str, Any]] = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            edit_type = str(edit.get("edit_type", "")).strip()

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
                    original = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    try:
                        original = file_path.read_text(encoding="gbk")
                        file_encoding = "gbk"
                    except (UnicodeDecodeError, LookupError):
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
                new_source = _convert_newlines("\n".join(lines) + (eol if original.endswith(("\n", "\r\n")) else ""), eol)
                try:
                    file_path.write_text(new_source, encoding=file_encoding)
                except (UnicodeEncodeError, UnicodeDecodeError):
                    file_path.write_text(new_source, encoding="utf-8")
                if report_dir is not None and backup_original_sources:
                    backup_root = report_dir / "original_sources"
                    containing_root = _find_containing_code_root(file_path, code_roots)
                    if containing_root:
                        backup_path = backup_root / file_path.relative_to(containing_root)
                        backup_path.parent.mkdir(parents=True, exist_ok=True)
                        if not backup_path.exists():
                            backup_path.write_text(original, encoding="utf-8")
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
                    original = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    try:
                        original = file_path.read_text(encoding="gbk")
                        file_encoding = "gbk"
                    except (UnicodeDecodeError, LookupError):
                        record["error"] = "目标文件编码无法识别（非 UTF-8/GBK）"
                        applied.append(record)
                        continue
                if old_text not in original:
                    record["error"] = "old_text 未能在文件中精确匹配"
                    applied.append(record)
                    continue
                eol = _source_newline_style(original)
                new_source = _convert_newlines(
                    original.replace(old_text, _convert_newlines(new_text, eol), 1),
                    eol,
                )
                try:
                    file_path.write_text(new_source, encoding=file_encoding)
                except (UnicodeEncodeError, UnicodeDecodeError):
                    file_path.write_text(new_source, encoding="utf-8")
                if report_dir is not None and backup_original_sources:
                    backup_root = report_dir / "original_sources"
                    containing_root = _find_containing_code_root(file_path, code_roots)
                    if containing_root:
                        backup_path = backup_root / file_path.relative_to(containing_root)
                        backup_path.parent.mkdir(parents=True, exist_ok=True)
                        if not backup_path.exists():
                            backup_path.write_text(original, encoding="utf-8")
                        record["backup_path"] = str(backup_path)
                record["status"] = "applied"
                record["replaced_preview"] = new_text
                applied.append(record)
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
                node = _build_candidate_node_from_current_source(file_path, signature)
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
                original = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # 尝试 GBK 等常见中文编码
                try:
                    original = file_path.read_text(encoding="gbk")
                    file_encoding = "gbk"
                except (UnicodeDecodeError, LookupError):
                    record["error"] = "目标文件编码无法识别（非 UTF-8/GBK）"
                    applied.append(record)
                    continue
            snippet_text = "\n".join(node["snippet"])
            new_text_content = original
            old_block: Optional[str] = _extract_current_function_block(
                file_path, signature, node, original
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
                applied.append(record)
                continue
            if not _extracted_block_matches_signature(old_block, signature):
                record["error"] = "定位到的函数块与目标签名不匹配（可能跨函数误截断）"
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
            # 仅拦截「同名转发 + 整函数入口判空」；其余质量由提示词与人工 review 控制。
            eol = _source_newline_style(original)
            replacement_code = _convert_newlines(replacement_code, eol)
            new_text_content = _convert_newlines(
                original.replace(old_block, replacement_code, 1),
                eol,
            )
            logger.info("[CodeFixer] 写入文件: %s (diff=%+d bytes, encoding=%s)",
                        file_path, len(new_text_content) - len(original), file_encoding)
            try:
                file_path.write_text(new_text_content, encoding=file_encoding)
            except (UnicodeEncodeError, UnicodeDecodeError):
                # replacement_code 含原编码无法表示的字符，升级为 UTF-8 写入
                file_path.write_text(new_text_content, encoding="utf-8")
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
                    backup_path.write_text(original, encoding=file_encoding)
                record["backup_path"] = str(backup_path)
            record["status"] = "applied"
            record["replaced_preview"] = replacement_code
            applied.append(record)

        success = any(item.get("status") == "applied" for item in applied)
        return FixResult(
            success=success,
            applied=applied,
            summary=str(fix_plan.get("summary", "")) if isinstance(fix_plan, dict) else "",
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
        # 向前回退到函数声明的起始位置（包含返回值类型）
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
        return source[:sig_index] + replacement_code + source[end_index + 1 :], old_block
