"""Source-code resolvers for model-requested analyze context."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from services.code_locator import CodeLocatorService, LocatorConfig, SymbolLocator


class CodeContextRequestResolver:

    @staticmethod
    def _read_source_lines(file_path: str) -> List[str]:
        try:
            return Path(file_path).read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            return []

    @staticmethod
    def _is_destructor_symbol(symbol: str) -> bool:
        raw = str(symbol or "").strip()
        if not raw:
            return False
        head = raw.split("(", 1)[0]
        return bool(re.search(r"::~\w+\s*$", head) or re.search(r"::~\w+\s*$", raw))

    @classmethod
    def _normalize_context_request_symbol(cls, symbol: str, req_type: str = "") -> str:
        """规范化 context request 符号，减少等价写法导致的定位/去重失败。"""
        raw = str(symbol or "").strip()
        if not raw:
            return raw
        req_type = str(req_type or "").strip().lower()
        head = raw.split("(", 1)[0].strip()
        if req_type == "function" and re.search(r"::\s*~\w+\s*$", head):
            return head + "()"
        if req_type == "references":
            return raw.rstrip().rstrip(";")
        return raw

    @classmethod
    def _canonical_context_request_symbol(cls, symbol: str, req_type: str = "") -> str:
        """将等价函数符号规范为同一去重键（如 CVList::RemoveAll 与 CVList<T>::RemoveAll）。"""
        norm = cls._normalize_context_request_symbol(symbol, req_type)
        if str(req_type or "").strip().lower() != "function":
            return norm
        head = norm.split("(", 1)[0].strip()
        from services.code_locator import SymbolLocator

        tpl = SymbolLocator.parse_template_qualified_symbol(head)
        if tpl:
            return f"{tpl['template_class']}<>::{tpl['member']}"
        parsed = SymbolLocator.parse_qualified_member_symbol(head)
        if parsed:
            scope = str(parsed.get("short_scope") or "")
            method = str(parsed.get("method") or "")
            if (
                scope
                and method
                and scope[0].isupper()
                and not scope.startswith("m_")
                and "<" not in head
            ):
                return f"{scope}<>::{method}"
        return norm

    @classmethod
    def _context_request_outcome_key(
        cls, req_type: str, symbol: str, file_path: str, line_number: int
    ) -> str:
        norm = cls._normalize_context_request_symbol(symbol, req_type)
        req_type = str(req_type or "").strip().lower()
        if req_type == "function" and not file_path and line_number <= 0:
            return f"{req_type}::{norm}"
        return f"{req_type}:{file_path}:{line_number}:{norm}".strip(":")

    @classmethod
    def _context_request_success_dedupe_keys(
        cls, req_type: str, symbol: str, file_path: str, line_number: int
    ) -> List[str]:
        """成功去重键：等价模板函数写法共享；失败/拒绝仍用精确 outcome key。"""
        exact = cls._context_request_outcome_key(req_type, symbol, file_path, line_number)
        keys = [exact]
        if str(req_type or "").strip().lower() == "function":
            canon = cls._canonical_context_request_symbol(symbol, req_type)
            canon_key = cls._context_request_outcome_key(req_type, canon, file_path, line_number)
            if canon_key not in keys:
                keys.append(canon_key)
        return keys

    @classmethod
    def _record_context_request_outcome(
        cls,
        outcomes: Dict[str, str],
        req_type: str,
        symbol: str,
        file_path: str,
        line_number: int,
        status: str,
    ) -> None:
        key = cls._context_request_outcome_key(req_type, symbol, file_path, line_number)
        if not key:
            return
        outcomes[key] = status
        if status == "success":
            for alias in cls._context_request_success_dedupe_keys(
                req_type, symbol, file_path, line_number
            ):
                outcomes[alias] = "success"

    @staticmethod
    def _looks_like_package_or_thread_label(symbol: str) -> bool:
        """判断是否为 Android 包名/线程标签（而非 C++ 成员访问）。"""
        symbol = str(symbol or "").strip()
        if not symbol or "::" in symbol:
            return False
        if re.match(r"^m_[A-Za-z_]\w*\.[A-Za-z_]\w+", symbol):
            return False
        if not re.match(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", symbol):
            return False
        return True

    @staticmethod
    def _parse_member_method_reference(symbol: str) -> Optional[Dict[str, str]]:
        """解析 obj.method / obj.method() 形式的引用符号。"""
        raw = str(symbol or "").strip().rstrip(";")
        if raw.endswith("()"):
            raw = raw[:-2]
        raw = raw.rstrip(")").strip()
        match = re.match(r"^(?:([\w:<>*,&\s]+)::)?(m_\w+)\.(\w+)$", raw)
        if match:
            return {
                "owner_prefix": str(match.group(1) or "").strip(),
                "member": match.group(2),
                "method": match.group(3),
            }
        match = re.match(r"^(\w+)\.(\w+)$", raw)
        if match and match.group(1).startswith("m_"):
            return {
                "owner_prefix": "",
                "member": match.group(1),
                "method": match.group(2),
            }
        return None

    @classmethod
    def _extract_brace_block_lines(
        cls, lines: List[str], start_index: int, *, max_lines: int = 120
    ) -> List[str]:
        if start_index < 0 or start_index >= len(lines):
            return []
        collected: List[str] = []
        depth = 0
        started = False
        for idx in range(start_index, min(len(lines), start_index + max_lines)):
            line = lines[idx]
            collected.append(line)
            depth += line.count("{") - line.count("}")
            if "{" in line:
                started = True
            if started and depth <= 0:
                break
        if not collected:
            collected = lines[start_index : min(len(lines), start_index + 20)]
        return collected

    @classmethod
    def _repair_template_function_snippet(
        cls,
        *,
        symbol: str,
        target_file: str,
        target_line: int,
        out: Dict[str, Any],
    ) -> Dict[str, Any]:
        from services.code_locator import SymbolLocator

        lines = cls._read_source_lines(target_file)
        if not lines or not (0 < target_line <= len(lines)):
            return out
        def_idx = target_line - 1
        def_line = lines[def_idx]
        is_template_member = bool(
            re.search(r"\b\w+\s*<[^>]*>\s*::", def_line)
            or (
                def_idx > 0
                and re.match(r"\s*template\s*<", lines[def_idx - 1])
                and "::" in def_line
            )
        )
        if not is_template_member:
            return out
        head = str(symbol or "").split("(", 1)[0].strip()
        if not (
            SymbolLocator.parse_template_qualified_symbol(symbol)
            or SymbolLocator.parse_qualified_member_symbol(head)
        ):
            return out
        start_idx = def_idx
        if def_idx > 0 and re.match(r"\s*template\s*<", lines[def_idx - 1]):
            start_idx = def_idx - 1
        body_lines = cls._extract_brace_block_lines(lines, def_idx)
        if not body_lines:
            return out
        snippet_lines: List[str] = []
        if start_idx < def_idx:
            snippet_lines.append(lines[start_idx])
        snippet_lines.extend(body_lines)
        out = dict(out)
        out["snippet"] = snippet_lines
        out["function_name"] = def_line.strip()
        out["snippet_start_line"] = start_idx + 1
        out["snippet_end_line"] = start_idx + len(snippet_lines)
        return out

    @classmethod
    def _template_symbol_matches_text(cls, symbol: str, *texts: str) -> bool:
        from services.code_locator import SymbolLocator

        parsed = SymbolLocator.parse_template_qualified_symbol(symbol)
        if not parsed:
            return False
        template_class = str(parsed.get("template_class") or "")
        member = str(parsed.get("member") or "")
        if not template_class or not member:
            return False
        pattern = rf"\b{re.escape(template_class)}\s*<[^>]*>\s*::\s*{re.escape(member)}\s*\("
        for candidate in texts:
            text = str(candidate or "").strip()
            if text and re.search(pattern, text):
                return True
        return False

    @classmethod
    def _bare_template_class_method_matches_text(cls, symbol: str, *texts: str) -> bool:
        """CVList::RemoveAll 等未写模板参数的限定符号，匹配 CVList<T>::RemoveAll 实现。"""
        from services.code_locator import SymbolLocator

        head = str(symbol or "").split("(", 1)[0].strip()
        if "<" in head:
            return False
        parsed = SymbolLocator.parse_qualified_member_symbol(head)
        if not parsed:
            return False
        scope = str(parsed.get("short_scope") or "")
        method = str(parsed.get("method") or "")
        if not scope or not method or not scope[0].isupper() or scope.startswith("m_"):
            return False
        pattern = rf"\b{re.escape(scope)}\s*<[^>]*>\s*::\s*{re.escape(method)}\s*\("
        for candidate in texts:
            text = str(candidate or "").strip()
            if text and re.search(pattern, text):
                return True
        return False

    @classmethod
    def _resolved_function_matches_symbol(
        cls,
        symbol: str,
        file_path: str,
        line_number: int,
        function_signature: str = "",
        snippet_text: str = "",
    ) -> bool:
        """校验定位结果是否与请求的符号类型一致（限定名、ctor/dtor）。"""
        from services.code_locator import SymbolLocator

        lines = cls._read_source_lines(file_path)
        line = ""
        if 0 < line_number <= len(lines):
            line = lines[line_number - 1]
        sig = str(function_signature or line)
        norm_symbol = cls._normalize_context_request_symbol(symbol, "function")
        if SymbolLocator.parse_template_qualified_symbol(norm_symbol):
            return cls._template_symbol_matches_text(norm_symbol, line, snippet_text)
        if cls._template_symbol_matches_text(norm_symbol, sig, line):
            return True
        if cls._bare_template_class_method_matches_text(
            norm_symbol, sig, line, snippet_text
        ):
            return True
        head = str(norm_symbol or "").split("(", 1)[0]
        if "::" in head:
            if not SymbolLocator.qualified_symbol_matches_line(norm_symbol, line, sig):
                if cls._is_destructor_symbol(norm_symbol):
                    m = re.search(r"::~(\w+)", norm_symbol)
                    if m:
                        class_name = m.group(1)
                        return bool(re.search(rf"::~\s*{re.escape(class_name)}\s*\(", sig))
                return False
        if cls._is_destructor_symbol(norm_symbol):
            m = re.search(r"::~(\w+)", norm_symbol)
            if m:
                class_name = m.group(1)
                return bool(re.search(rf"::~\s*{re.escape(class_name)}\s*\(", sig))
        return True

    @classmethod
    def _resolved_snippet_matches_symbol(
        cls, symbol: str, snippet_text: str, function_signature: str = ""
    ) -> bool:
        """限定成员符号时，要求片段或签名中出现 Class::method。"""
        from services.code_locator import SymbolLocator

        if cls._template_symbol_matches_text(symbol, snippet_text, function_signature):
            return True
        if cls._bare_template_class_method_matches_text(
            symbol, function_signature, snippet_text
        ):
            return True
        head = str(symbol or "").split("(", 1)[0]
        if "::" not in head:
            return True
        for candidate in (function_signature, snippet_text):
            text = str(candidate or "").strip()
            if text and SymbolLocator.qualified_symbol_matches_line(symbol, text):
                return True
        return False

    @staticmethod
    def _context_request_symbol_leaf(symbol: str) -> str:
        raw = str(symbol or "").strip()
        if "::" in raw:
            raw = raw.rsplit("::", 1)[-1]
        return raw.strip()

    @classmethod
    def _looks_like_member_field_request(cls, symbol: str) -> bool:
        leaf = cls._context_request_symbol_leaf(symbol)
        # 当前工程中成员变量/函数指针大多以 m_ 命名；作为 function 请求时应要求改用 field/references。
        return bool(re.match(r"^m_[A-Za-z_]\w*$", leaf))

    @staticmethod
    def _reject_unavailable_context_request(
        req: Dict[str, Any],
        *,
        symbol: str,
        file_path: str,
        line_number: int,
    ) -> Optional[str]:
        """拒绝明显不可机器定位的 context_requests，避免误命中无关源码。"""
        if file_path and line_number > 0:
            return None
        symbol = str(symbol or "").strip()
        if not symbol:
            return None
        # 典型误用：把线程名/进程名/包名（如 com.anjuke.home）臆造成 C++ 符号。
        if CodeContextRequestResolver._looks_like_package_or_thread_label(symbol):
            return (
                f"拒绝请求: `{symbol}` 看起来是线程名/进程名/包名标签，"
                "不是可解析的源码符号；请作为缺失证据/人工补充项处理。"
            )
        if re.match(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+::", symbol):
            prefix = symbol.split("::", 1)[0]
            return (
                f"拒绝请求: `{symbol}` 看起来由线程名/进程名/包名 `{prefix}` "
                "臆造成函数符号；当前无法机器定位该上下文，请作为缺失证据/人工补充项处理。"
            )
        # 裸 main 在大型仓库中高度歧义；没有 file+line 时容易命中无关工具程序入口。
        if symbol in {"main", "::main"}:
            return (
                "拒绝请求: `main` 未提供文件或行号，仓库内可能存在多个无关入口函数；"
                "请作为缺失证据/人工补充项处理，或提供明确 file+line。"
            )
        if symbol in {"Activity::onCreate", "Application::onCreate"}:
            return (
                f"拒绝请求: `{symbol}` 是无日志/栈帧证据支持的入口函数猜测；"
                "当前无法机器定位该上下文，请作为缺失证据/人工补充项处理。"
            )
        return None

    @staticmethod
    def _iter_context_search_files(
        code_roots: List[str],
        *,
        max_files: int = 5000,
    ) -> List[str]:
        config = LocatorConfig(max_code_length=0)
        files: List[str] = []
        seen: Set[str] = set()
        for root in code_roots:
            root_path = Path(str(root or "")).expanduser()
            if not root_path.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root_path):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in config.exclude_dirs and not d.startswith(".")
                ]
                for name in filenames:
                    path = Path(dirpath) / name
                    if path.suffix not in config.supported_extensions:
                        continue
                    try:
                        if path.stat().st_size > config.max_file_size:
                            continue
                        resolved = str(path.resolve())
                    except Exception:
                        continue
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    files.append(resolved)
                    if len(files) >= max_files:
                        return files
        return files

    @staticmethod
    def _parse_qualified_field_symbol(symbol: str) -> Optional[Dict[str, str]]:
        """解析 Class::field / Ns::Class::field 形式的字段符号。"""
        head = str(symbol or "").strip()
        if not head:
            return None
        paren_idx = head.find("(")
        if paren_idx > 0:
            head = head[:paren_idx].strip()
        if "::" not in head:
            return None
        parts = [p.strip() for p in head.split("::") if p.strip()]
        if len(parts) < 2:
            return None
        return {
            "class_name": parts[-2],
            "field": parts[-1],
            "scope": "::".join(parts[:-1]),
        }

    @staticmethod
    def _field_context_path_score(
        file_path: str,
        class_name: str = "",
        *,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> int:
        path = str(file_path or "").replace("\\", "/").lower()
        score = 0
        if any(
            token in path
            for token in ("/demo/", "/test/", "/apptest/", "/unittest/", "/systemtest/", "/enginetest/")
        ):
            score -= 120
        if any(token in path for token in ("/support/", "/huiwei")):
            score -= 150
        if any(token in path for token in ("/engine-dev/", "/src/app/", "/src/", "/inc/")):
            score += 80
        if "/basemap/vmap/" in path:
            score += 50
        elif "/basemap/gmap/" in path:
            score -= 30
        if path.endswith(".h") or path.endswith(".hpp") or path.endswith(".hh"):
            score += 60
        elif path.endswith(".cpp") or path.endswith(".cc") or path.endswith(".cxx"):
            score += 20
        if path.endswith("vtempl.h"):
            score += 40
        if class_name:
            stem = class_name.lower().lstrip("cv").lstrip("c")
            base = class_name.lower()
            file_name = path.rsplit("/", 1)[-1]
            if base in file_name or (stem and stem in file_name):
                score += 100
        for cls_name in stack_priority_classes or []:
            token = str(cls_name or "").strip().lower()
            if token and token in path:
                score += 70
        return score

    @classmethod
    def _classify_field_match_kind(cls, line: str, field: str) -> Optional[str]:
        stripped = str(line or "").strip()
        if not stripped or not re.search(rf"\b{re.escape(field)}\b", stripped):
            return None
        if re.search(rf"\b{re.escape(field)}\s*;", stripped) and not re.search(
            rf"\b{re.escape(field)}\s*=", stripped
        ):
            return "declaration"
        if re.search(rf"\b{re.escape(field)}\s*\(", stripped) and (
            ":" in stripped or stripped.endswith(")") or stripped.endswith("),")
        ):
            return "initialization"
        if re.search(rf"\b{re.escape(field)}\s*=", stripped) or re.search(
            rf"\b{re.escape(field)}\s*[+\-]{2}", stripped
        ):
            return "usage"
        if re.search(rf"\bif\s*\([^)]*\b{re.escape(field)}\b", stripped):
            return "usage"
        if re.search(rf"\b{re.escape(field)}\b\s*[,;=)]", stripped):
            return "usage"
        return "usage"

    @staticmethod
    def _field_match_kind_label(kind: str) -> str:
        return {
            "declaration": "成员声明",
            "initialization": "初始化",
            "usage": "读写/使用",
            "class_declaration": "类声明",
        }.get(str(kind or ""), str(kind or ""))

    @classmethod
    def _looks_like_type_name_field_request(cls, symbol: str) -> bool:
        raw = str(symbol or "").strip()
        if not raw or "::" in raw or raw.startswith("m_"):
            return False
        return bool(re.match(r"^[A-Z][A-Za-z0-9_]*$", raw))

    @classmethod
    def _extract_class_declaration_span(
        cls, lines: List[str], line_number: int
    ) -> Tuple[int, int]:
        idx = max(0, line_number - 1)
        start_idx = idx
        if idx > 0 and re.match(r"\s*template\s*<", lines[idx - 1]):
            start_idx = idx - 1
        depth = 0
        started = False
        end_idx = idx
        for i in range(start_idx, min(len(lines), start_idx + 100)):
            line = lines[i]
            end_idx = i
            if "{" in line:
                started = True
            depth += line.count("{") - line.count("}")
            if started and depth <= 0:
                break
            if not started and line.strip().endswith(";") and i > idx:
                break
        return start_idx + 1, end_idx + 1

    @classmethod
    def _find_class_declaration_context_matches(
        cls,
        symbol: str,
        code_roots: List[str],
        *,
        max_matches: int = 2,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        class_name = str(symbol or "").strip()
        if not class_name:
            return []
        class_re = re.compile(rf"\b(?:class|struct)\s+{re.escape(class_name)}\b")
        ctor_re = re.compile(
            rf"\b{re.escape(class_name)}\s*::\s*{re.escape(class_name)}\s*\("
        )
        ranked: List[Tuple[int, Dict[str, Any]]] = []
        files = cls._iter_context_search_files(code_roots)
        files.sort(
            key=lambda fp: -cls._field_context_path_score(
                fp, class_name, stack_priority_classes=stack_priority_classes
            )
        )
        for file_path in files:
            lines = cls._read_source_lines(file_path)
            if not lines:
                continue
            path_score = cls._field_context_path_score(
                file_path, class_name, stack_priority_classes=stack_priority_classes
            )
            for idx, line in enumerate(lines, start=1):
                if ctor_re.search(line) or not class_re.search(line):
                    continue
                start, end = cls._extract_class_declaration_span(lines, idx)
                ranked.append(
                    (
                        path_score,
                        {
                            "file": file_path,
                            "line_number": idx,
                            "line_text": lines[idx - 1].strip(),
                            "match_kind": "class_declaration",
                            "match_kind_label": cls._field_match_kind_label(
                                "class_declaration"
                            ),
                            "context_start_line": start,
                            "context_end_line": end,
                            "context": lines[start - 1 : end],
                        },
                    )
                )
        ranked.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line_number"]))
        return [entry for _, entry in ranked[:max_matches]]

    @classmethod
    def _file_declares_class(cls, lines: List[str], class_name: str) -> bool:
        if not class_name or not lines:
            return False
        pattern = re.compile(rf"\b(?:class|struct)\s+{re.escape(class_name)}\b")
        return any(pattern.search(line) for line in lines)

    @classmethod
    def _build_field_match_entry(
        cls,
        *,
        file_path: str,
        lines: List[str],
        line_number: int,
        match_kind: str,
        context_radius: int = 2,
    ) -> Dict[str, Any]:
        stripped = lines[line_number - 1].strip()
        if match_kind == "class_declaration":
            start, end = cls._extract_class_declaration_span(lines, line_number)
            return {
                "file": file_path,
                "line_number": line_number,
                "line_text": stripped,
                "match_kind": match_kind,
                "match_kind_label": cls._field_match_kind_label(match_kind),
                "context_start_line": start,
                "context_end_line": end,
                "context": lines[start - 1 : end],
            }
        if match_kind == "declaration":
            context_radius = 4
        start = max(1, line_number - context_radius)
        end = min(len(lines), line_number + context_radius)
        return {
            "file": file_path,
            "line_number": line_number,
            "line_text": stripped,
            "match_kind": match_kind,
            "match_kind_label": cls._field_match_kind_label(match_kind),
            "context_start_line": start,
            "context_end_line": end,
            "context": lines[start - 1:end],
        }

    @classmethod
    def _find_template_field_context_matches(
        cls,
        symbol: str,
        code_roots: List[str],
        *,
        max_matches: int = 2,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        from services.code_locator import SymbolLocator

        parsed = SymbolLocator.parse_template_qualified_symbol(symbol)
        if not parsed:
            return []
        template_class = str(parsed.get("template_class") or "")
        field = str(parsed.get("member") or "")
        if not template_class or not field:
            return []
        class_re = re.compile(rf"\b(?:class|struct)\s+{re.escape(template_class)}\b")
        token_re = re.compile(rf"\b{re.escape(field)}\b")
        ranked: List[Tuple[int, Dict[str, Any]]] = []
        files = cls._iter_context_search_files(code_roots)
        files.sort(
            key=lambda fp: -cls._field_context_path_score(
                fp, template_class, stack_priority_classes=stack_priority_classes
            )
        )
        for file_path in files:
            lines = cls._read_source_lines(file_path)
            if not lines or not class_re.search("\n".join(lines)):
                continue
            path_score = cls._field_context_path_score(
                file_path, template_class, stack_priority_classes=stack_priority_classes
            )
            for idx, line in enumerate(lines, start=1):
                if not token_re.search(line):
                    continue
                match_kind = cls._classify_field_match_kind(line, field)
                if match_kind != "declaration":
                    continue
                entry = cls._build_field_match_entry(
                    file_path=file_path,
                    lines=lines,
                    line_number=idx,
                    match_kind="declaration",
                )
                ranked.append((path_score, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line_number"]))
        return [entry for _, entry in ranked[:max_matches]]

    @classmethod
    def _collect_field_match_candidates(
        cls,
        *,
        field: str,
        class_name: str,
        code_roots: List[str],
        allowed_kinds: Optional[Set[str]] = None,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Tuple[int, Dict[str, Any]]]:
        if not field:
            return []
        token_re = re.compile(rf"\b{re.escape(field)}\b")
        ranked: List[Tuple[int, Dict[str, Any]]] = []
        kind_weight = {"declaration": 200, "initialization": 80, "usage": 20}
        files = cls._iter_context_search_files(code_roots)
        files.sort(
            key=lambda fp: -cls._field_context_path_score(
                fp, class_name, stack_priority_classes=stack_priority_classes
            )
        )
        for file_path in files:
            lines = cls._read_source_lines(file_path)
            if not lines:
                continue
            if class_name and not cls._file_declares_class(lines, class_name):
                continue
            path_score = cls._field_context_path_score(
                file_path, class_name, stack_priority_classes=stack_priority_classes
            )
            for idx, line in enumerate(lines, start=1):
                if not token_re.search(line):
                    continue
                match_kind = cls._classify_field_match_kind(line, field)
                if not match_kind:
                    continue
                if allowed_kinds is not None and match_kind not in allowed_kinds:
                    continue
                entry = cls._build_field_match_entry(
                    file_path=file_path,
                    lines=lines,
                    line_number=idx,
                    match_kind=match_kind,
                )
                score = path_score + kind_weight.get(match_kind, 0) - idx // 10000
                ranked.append((score, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line_number"]))
        return ranked

    @classmethod
    def _find_field_context_matches(
        cls,
        symbol: str,
        code_roots: List[str],
        *,
        max_matches: int = 4,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        from services.code_locator import SymbolLocator

        if cls._looks_like_type_name_field_request(symbol):
            class_matches = cls._find_class_declaration_context_matches(
                symbol,
                code_roots,
                max_matches=max_matches,
                stack_priority_classes=stack_priority_classes,
            )
            if class_matches:
                return class_matches
        template_matches = cls._find_template_field_context_matches(
            symbol,
            code_roots,
            max_matches=max_matches,
            stack_priority_classes=stack_priority_classes,
        )
        if template_matches:
            return template_matches
        parsed = cls._parse_qualified_field_symbol(symbol)
        field = parsed["field"] if parsed else cls._context_request_symbol_leaf(symbol)
        class_name = str(parsed.get("class_name") or "") if parsed else ""
        if not field:
            return []
        if not class_name:
            max_matches = min(max_matches, 1)

        if class_name:
            ranked = cls._collect_field_match_candidates(
                field=field,
                class_name=class_name,
                code_roots=code_roots,
                allowed_kinds={"declaration"},
                stack_priority_classes=stack_priority_classes,
            )
            if not ranked:
                ranked = cls._collect_field_match_candidates(
                    field=field,
                    class_name=class_name,
                    code_roots=code_roots,
                    allowed_kinds={"initialization"},
                    stack_priority_classes=stack_priority_classes,
                )
        else:
            ranked = cls._collect_field_match_candidates(
                field=field,
                class_name="",
                code_roots=code_roots,
                allowed_kinds={"declaration", "initialization"},
                stack_priority_classes=stack_priority_classes,
            )

        matches: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, int, str]] = set()
        for _, entry in ranked:
            key = (entry["file"], int(entry["line_number"]), str(entry.get("match_kind")))
            if key in seen:
                continue
            seen.add(key)
            matches.append(entry)
            if len(matches) >= max_matches:
                break
        return matches

    @classmethod
    def _find_reference_context_matches(
        cls,
        symbol: str,
        code_roots: List[str],
        *,
        max_matches: int = 12,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        member_method = cls._parse_member_method_reference(symbol)
        if member_method:
            member = member_method["member"]
            method = member_method["method"]
            literal_re = re.compile(
                rf"\b{re.escape(member)}\s*\.\s*{re.escape(method)}\s*\("
            )
            method_re = re.compile(
                rf"\.{re.escape(method)}\s*\(|\b{re.escape(method)}\s*\("
            )
            ranked: List[Tuple[int, Dict[str, Any]]] = []
            files = cls._iter_context_search_files(code_roots)
            files.sort(
                key=lambda fp: -cls._field_context_path_score(
                    fp, "", stack_priority_classes=stack_priority_classes
                )
            )
            for file_path in files:
                lines = cls._read_source_lines(file_path)
                if not lines:
                    continue
                path_score = cls._field_context_path_score(
                    file_path, "", stack_priority_classes=stack_priority_classes
                )
                for idx, line in enumerate(lines, start=1):
                    stripped = line.strip()
                    if literal_re.search(stripped):
                        boost = 120
                    elif method_re.search(stripped) and member in stripped:
                        boost = 80
                    elif method_re.search(stripped):
                        boost = 20
                    else:
                        continue
                    entry = cls._build_field_match_entry(
                        file_path=file_path,
                        lines=lines,
                        line_number=idx,
                        match_kind="usage",
                    )
                    ranked.append((path_score + boost - idx // 10000, entry))
            ranked.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line_number"]))
            matches: List[Dict[str, Any]] = []
            seen: Set[Tuple[str, int]] = set()
            for _, entry in ranked:
                key = (entry["file"], int(entry["line_number"]))
                if key in seen:
                    continue
                seen.add(key)
                matches.append(entry)
                if len(matches) >= max_matches:
                    break
            if matches:
                return matches

        return cls._find_text_context_matches(
            symbol,
            code_roots,
            mode="references",
            max_matches=max_matches,
            stack_priority_classes=stack_priority_classes,
            parse_member_method=False,
        )

    @classmethod
    def _find_text_context_matches(
        cls,
        symbol: str,
        code_roots: List[str],
        *,
        mode: str,
        max_matches: int = 8,
        stack_priority_classes: Optional[List[str]] = None,
        parse_member_method: bool = True,
    ) -> List[Dict[str, Any]]:
        if mode == "field":
            return cls._find_field_context_matches(
                symbol,
                code_roots,
                max_matches=max_matches,
                stack_priority_classes=stack_priority_classes,
            )
        leaf = cls._context_request_symbol_leaf(symbol)
        if not leaf:
            return []
        parsed = cls._parse_qualified_field_symbol(symbol)
        class_name = str(parsed.get("class_name") or "") if parsed else ""
        token_re = re.compile(rf"\b{re.escape(leaf)}\b")
        ranked: List[Tuple[int, Dict[str, Any]]] = []
        files = cls._iter_context_search_files(code_roots)
        files.sort(
            key=lambda fp: -cls._field_context_path_score(
                fp, class_name, stack_priority_classes=stack_priority_classes
            )
        )
        for file_path in files:
            lines = cls._read_source_lines(file_path)
            if not lines:
                continue
            if class_name and not cls._file_declares_class(lines, class_name):
                continue
            path_score = cls._field_context_path_score(
                file_path, class_name, stack_priority_classes=stack_priority_classes
            )
            for idx, line in enumerate(lines, start=1):
                if not token_re.search(line):
                    continue
                match_kind = cls._classify_field_match_kind(line, leaf) or "usage"
                entry = cls._build_field_match_entry(
                    file_path=file_path,
                    lines=lines,
                    line_number=idx,
                    match_kind=match_kind,
                )
                ranked.append((path_score - idx // 10000, entry))
                if len(ranked) >= max_matches * 4:
                    break
        ranked.sort(key=lambda item: (-item[0], item[1]["file"], item[1]["line_number"]))
        matches: List[Dict[str, Any]] = []
        seen: Set[Tuple[str, int]] = set()
        for _, entry in ranked:
            key = (entry["file"], int(entry["line_number"]))
            if key in seen:
                continue
            seen.add(key)
            matches.append(entry)
            if len(matches) >= max_matches:
                break
        return matches

    @classmethod
    def _resolve_non_function_context_request(
        cls,
        req: Dict[str, Any],
        req_type: str,
        symbol: str,
        code_roots: List[str],
        locator: CodeLocatorService,
        *,
        context: Optional["WorkflowContext"] = None,
        tool_executor: Optional[Any] = None,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        if req_type == "field":
            matches = cls._find_text_context_matches(
                symbol,
                code_roots,
                mode="field",
                max_matches=4,
                stack_priority_classes=stack_priority_classes,
            )
            if matches:
                return {
                    "request": req,
                    "success": True,
                    "context_type": "field",
                    "symbol": symbol,
                    "matches": matches,
                }
            return {
                "request": req,
                "success": False,
                "context_type": "field",
                "error": f"未定位到字段声明/初始化: {symbol}",
            }
        if req_type == "references":
            matches = cls._find_reference_context_matches(
                symbol,
                code_roots,
                max_matches=12,
                stack_priority_classes=stack_priority_classes,
            )
            if matches:
                return {
                    "request": req,
                    "success": True,
                    "context_type": "references",
                    "symbol": symbol,
                    "matches": matches,
                }
            return {
                "request": req,
                "success": False,
                "context_type": "references",
                "error": f"未定位到引用: {symbol}",
            }
        if req_type == "callers":
            if cls._is_destructor_symbol(symbol):
                norm = cls._normalize_context_request_symbol(symbol, "function")
                found = locator.find_function_definition_for_symbol(norm, code_roots)
                if found:
                    payload = {
                        "file_path": found[0],
                        "line_number": int(found[1]),
                        "function_name": norm,
                        "max_code_length": 0,
                    }
                    from services.tool_invoke import snippet_extractor_executor

                    exec_fn = snippet_extractor_executor(
                        context=context, tool_executor=tool_executor,
                    )
                    out = exec_fn("snippet_extractor", payload)
                    if not out.get("error"):
                        return {
                            "request": req,
                            "success": True,
                            "context_type": "callers",
                            "symbol": symbol,
                            "matches": [
                                {
                                    "name": out.get("function_name") or norm,
                                    "file": out.get("file_path") or found[0],
                                    "parent_fun": "",
                                    "snippet": out.get("snippet")
                                    if isinstance(out.get("snippet"), list)
                                    else [],
                                    "note": "callers 请求命中析构函数，已回填析构实现源码。",
                                }
                            ],
                        }
            simple = locator.symbol_locator.extract_simple_function_name(symbol) or symbol
            callers = locator.find_callers(simple, code_roots, max_search_files=600)[:8]
            if callers:
                return {
                    "request": req,
                    "success": True,
                    "context_type": "callers",
                    "symbol": symbol,
                    "matches": [
                        {
                            "name": c.name,
                            "file": c.file,
                            "parent_fun": c.parent_fun,
                            "snippet": c.snippet,
                        }
                        for c in callers
                    ],
                }
            return {
                "request": req,
                "success": False,
                "context_type": "callers",
                "error": f"未定位到调用方: {symbol}",
            }
        return {
            "request": req,
            "success": False,
            "error": (
                f"不支持的 context request type: {req_type}; "
                "支持 function/field/references/callers"
            ),
        }

    @classmethod
    def resolve_requests(
        cls,
        requests: List[Dict[str, Any]],
        code_roots: List[str],
        *,
        context: Optional["WorkflowContext"] = None,
        tool_executor: Optional[Any] = None,
        max_requests: int = 5,
        seen_keys: Optional[Set[str]] = None,
        request_outcomes: Optional[Dict[str, str]] = None,
        stack_priority_classes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """按模型请求补充函数源码。当前仅支持 file+line 或 symbol/function 定位。"""
        from tool_system.workflow import WorkflowContext

        from services.tool_invoke import snippet_extractor_executor

        if context is None and tool_executor is None:
            from tools.snippet_extractor_tool import SnippetExtractorTool

            direct_snippet_tool = SnippetExtractorTool()
            exec_fn = lambda _name, payload: direct_snippet_tool.execute(payload)
        else:
            exec_fn = snippet_extractor_executor(context=context, tool_executor=tool_executor)

        def _extract_snippet(payload: Dict[str, Any]) -> Dict[str, Any]:
            return exec_fn("snippet_extractor", payload)

        # seen_keys 保留兼容；新逻辑使用 request_outcomes 记录 success/failed/rejected。
        outcomes = request_outcomes if isinstance(request_outcomes, dict) else {}
        if isinstance(seen_keys, set) and seen_keys and not outcomes:
            for legacy_key in seen_keys:
                outcomes.setdefault(str(legacy_key), "success")
        locator = CodeLocatorService(LocatorConfig(max_code_length=0))
        resolved: List[Dict[str, Any]] = []
        for req in requests[: max(0, max_requests)]:
            if not isinstance(req, dict):
                continue
            req_type = str(req.get("type") or "function").strip().lower()
            if req_type not in {"function", "field", "references", "callers"}:
                resolved.append({
                    "request": req,
                    "success": False,
                    "rejected": True,
                    "reject_reason": "unsupported_type",
                    "error": f"不支持的 context request type: {req_type}",
                })
                continue
            symbol = cls._normalize_context_request_symbol(
                str(req.get("symbol") or "").strip(), req_type
            )
            file_path = str(req.get("file") or "").strip()
            line_number = int(req.get("line_number") or 0)
            key = cls._context_request_outcome_key(req_type, symbol, file_path, line_number)
            success_keys = cls._context_request_success_dedupe_keys(
                req_type, symbol, file_path, line_number
            )
            if any(outcomes.get(alias) == "success" for alias in success_keys):
                resolved.append(
                    {
                        "request": req,
                        "success": False,
                        "skipped": True,
                        "skip_reason": "duplicate_request",
                        "error": f"重复请求，已在前序轮次成功补充: {symbol or file_path}",
                    }
                )
                continue
            if key and key in outcomes:
                prior = outcomes[key]
                if prior == "success":
                    resolved.append(
                        {
                            "request": req,
                            "success": False,
                            "skipped": True,
                            "skip_reason": "duplicate_request",
                            "error": f"重复请求，已在前序轮次成功补充: {symbol or file_path}",
                        }
                    )
                elif prior == "rejected":
                    resolved.append(
                        {
                            "request": req,
                            "success": False,
                            "rejected": True,
                            "reject_reason": "duplicate_rejected",
                            "error": f"重复请求，此前已判定不可用: {symbol or file_path}",
                        }
                    )
                else:
                    resolved.append(
                        {
                            "request": req,
                            "success": False,
                            "lookup_exhausted": True,
                            "error": (
                                f"此前已尝试但未定位，Agent 无法自动补充: "
                                f"{symbol or file_path}"
                            ),
                        }
                    )
                continue

            target_file = ""
            target_line = 0
            function_name = symbol
            error = ""
            reject_reason = cls._reject_unavailable_context_request(
                req,
                symbol=symbol,
                file_path=file_path,
                line_number=line_number,
            )
            if reject_reason:
                resolved.append(
                    {
                        "request": req,
                        "success": False,
                        "error": reject_reason,
                        "rejected": True,
                        "reject_reason": "unavailable_context",
                    }
                )
                cls._record_context_request_outcome(
                    outcomes, req_type, symbol, file_path, line_number, "rejected"
                )
                continue
            if req_type != "function":
                item = cls._resolve_non_function_context_request(
                    req,
                    req_type,
                    symbol,
                    code_roots,
                    locator,
                    context=context,
                    tool_executor=tool_executor,
                    stack_priority_classes=stack_priority_classes,
                )
                resolved.append(item)
                if item.get("success"):
                    cls._record_context_request_outcome(
                        outcomes, req_type, symbol, file_path, line_number, "success"
                    )
                elif item.get("rejected"):
                    cls._record_context_request_outcome(
                        outcomes, req_type, symbol, file_path, line_number, "rejected"
                    )
                else:
                    cls._record_context_request_outcome(
                        outcomes, req_type, symbol, file_path, line_number, "failed"
                    )
                continue
            if file_path and line_number > 0 and Path(file_path).is_file():
                target_file = str(Path(file_path).expanduser().resolve())
                target_line = line_number
            elif symbol:
                if cls._looks_like_member_field_request(symbol):
                    resolved.append(
                        {
                            "request": req,
                            "success": False,
                            "rejected": True,
                            "reject_reason": "type_mismatch",
                            "error": (
                                f"拒绝请求: `{symbol}` 看起来是成员变量/字段，不是函数；"
                                "请改用 type=field 获取声明/初始化，或 type=references 获取读写引用。"
                            ),
                        }
                    )
                    cls._record_context_request_outcome(
                        outcomes, req_type, symbol, file_path, line_number, "rejected"
                    )
                    continue
                found = locator.find_function_definition_for_symbol(symbol, code_roots)
                if found:
                    target_file, target_line = found[0], int(found[1])
                else:
                    error = f"未定位到函数定义: {symbol}"
            else:
                error = "缺少 symbol/function 或 file+line"

            if not target_file or target_line <= 0:
                item = {"request": req, "success": False, "error": error or "定位失败"}
                resolved.append(item)
                cls._record_context_request_outcome(
                    outcomes, req_type, symbol, file_path, line_number, "failed"
                )
                continue

            out = _extract_snippet(
                {
                    "file_path": target_file,
                    "line_number": target_line,
                    "function_name": function_name,
                    "max_code_length": 0,
                }
            )
            if out.get("error"):
                item = {"request": req, "success": False, "error": str(out.get("error"))}
                resolved.append(item)
                cls._record_context_request_outcome(
                    outcomes, req_type, symbol, file_path, line_number, "failed"
                )
                continue
            out = cls._repair_template_function_snippet(
                symbol=symbol,
                target_file=target_file,
                target_line=target_line,
                out=out,
            )
            resolved_sig = str(out.get("function_name") or function_name or "")
            snippet_lines = out.get("snippet") if isinstance(out.get("snippet"), list) else []
            snippet_text = "\n".join(str(line) for line in snippet_lines)
            if not cls._resolved_function_matches_symbol(
                symbol, target_file, target_line, resolved_sig, snippet_text
            ) or not cls._resolved_snippet_matches_symbol(symbol, snippet_text, resolved_sig):
                item = {
                    "request": req,
                    "success": False,
                    "error": (
                        f"定位结果与请求符号不一致（疑似误命中构造函数/其它同名函数）: "
                        f"{symbol}"
                    ),
                }
                resolved.append(item)
                cls._record_context_request_outcome(
                    outcomes, req_type, symbol, file_path, line_number, "failed"
                )
                continue
            resolved.append(
                {
                    "request": req,
                    "success": True,
                    "file": out.get("file_path") or target_file,
                    "function_signature": resolved_sig,
                    "snippet_start_line": out.get("snippet_start_line"),
                    "snippet_end_line": out.get("snippet_end_line"),
                    "snippet": out.get("snippet") if isinstance(out.get("snippet"), list) else [],
                    "strategy": out.get("strategy"),
                    "is_complete_function": out.get("is_complete_function"),
                }
            )
            cls._record_context_request_outcome(
                outcomes, req_type, symbol, file_path, line_number, "success"
            )
        return resolved
