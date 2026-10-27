"""Deterministic static checks for applied C / Objective-C patches.

The checks run after ``apply_patch`` and before diff inspection.  They never
compile anything; they only catch mistakes a compiler is certain to reject:

* braces that no longer balance, nested or duplicated function definitions;
* project-prefixed identifiers the patch introduced without a declaration;
* class messages to project methods that the class header does not declare;
* functions turned ``static`` while a header or another file still uses them;
* new C ``bool`` / ``true`` / ``false`` in ``.m`` / ``.mm`` files that already use ``BOOL``.

The header-declaration case is repaired in place (the declaration is copied
from the ``@implementation`` signature) and reported as an extra applied edit,
so later diff inspection, rollback and review creation see the header change.
"""
from __future__ import annotations

import os
import re
from itertools import accumulate
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

SOURCE_SUFFIXES = frozenset({".m", ".mm", ".c", ".cc", ".cpp", ".h", ".hpp"})
_SYMBOL_CHECK_SUFFIXES = frozenset({".m", ".mm", ".c", ".h"})
_OBJC_IMPL_SUFFIXES = frozenset({".m", ".mm"})
_HEADER_SUFFIXES = frozenset({".h", ".hpp", ".pch"})
_INDEX_SUFFIXES = SOURCE_SUFFIXES | {".pch"}
_SKIP_DIRS = frozenset({".git", "Pods", "build", "DerivedData", "node_modules", "Carthage", ".build"})
_MAX_INDEX_FILES = 6000
_SEARCH_LEVELS = 3
_IMPORT_DEPTH = 3

_STATEMENT_KEYWORDS = frozenset({
    "return", "else", "case", "goto", "do", "sizeof", "typeof", "__typeof__", "throw", "new", "delete",
})
_C_KEYWORDS = _STATEMENT_KEYWORDS | frozenset({
    "if", "for", "while", "switch", "default", "break", "continue", "static", "extern", "const",
    "volatile", "inline", "register", "auto", "struct", "union", "enum", "typedef", "signed",
    "unsigned", "void", "char", "short", "int", "long", "float", "double", "bool", "_Bool", "BOOL",
    "id", "self", "super", "nil", "Nil", "NULL", "YES", "NO", "true", "false", "defined",
})
_GENERIC_PREFIXES = frozenset({
    "get", "set", "has", "the", "for", "new", "old", "max", "min", "abs", "str", "mem", "sig",
    "signal", "handle", "handler", "mach", "dispatch", "pthread", "objc", "task", "thread", "kern",
    "sys", "dyld", "posix", "uint", "int", "size", "ptr", "std", "app", "log", "init", "default",
    "test", "main", "debug", "file", "path", "data", "info", "type", "time", "char", "bool", "void",
})

_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_ATTRIBUTE = r"__attribute__[ \t]*\(\((?:[^()]|\([^()]*\))*\)\)"
_TYPE_TOKENS = r"((?:" + _ATTRIBUTE + r"[ \t\n\*]*|[A-Za-z_]\w*[ \t\n\*]+)+)"
_PARAMS = r"\(([^;{}()]*(?:\([^;{}()]*\)[^;{}()]*)*)\)"
_FUNC_DEF_RE = re.compile(
    r"^[ \t]*" + _TYPE_TOKENS + r"\**[ \t]*([A-Za-z_]\w*)[ \t]*" + _PARAMS + r"[ \t\n]*\{",
    re.M,
)
_FUNC_DECL_RE = re.compile(
    r"^[ \t]*" + _TYPE_TOKENS + r"\**[ \t]*([A-Za-z_]\w*)[ \t]*" + _PARAMS + r"[^;{}]*[;{]",
    re.M,
)
_VAR_DECL_RE = re.compile(
    r"(?:^|[;{}(,])[ \t\n]*" + _TYPE_TOKENS + r"\**[ \t]*([A-Za-z_]\w*)[ \t]*(?:\[[^\]\n]*\][ \t]*)*(?=[=;,)\[])",
    re.M,
)
_ENUM_BODY_RE = re.compile(r"\benum\b[^{;]*\{([^{}]*)\}")
_ENUM_ITEM_RE = re.compile(r"(?:^|,)[ \t\n]*([A-Za-z_]\w*)[ \t\n]*(?==|,|$)")
_DECLARATOR_RE = re.compile(r"[ \t\n\*]*([A-Za-z_]\w*)")
_DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)", re.M)
_TYPEDEF_CLOSE_RE = re.compile(r"\}[ \t\n]*\**[ \t]*([A-Za-z_]\w*)[ \t]*(?:\[[^\]\n]*\][ \t]*)*[;,]")
_TYPEDEF_RE = re.compile(r"\btypedef\b[^;{}]*?\b([A-Za-z_]\w*)[ \t]*(?:\[[^\]\n]*\][ \t]*)*;")
_FUNC_PTR_RE = re.compile(r"\([ \t]*[\^\*][ \t]*([A-Za-z_]\w*)[ \t]*\)[ \t]*\(")
_TAG_RE = re.compile(r"\b(?:struct|union|enum)[ \t\n]+([A-Za-z_]\w*)")
_OBJC_DECL_RE = re.compile(r"@(?:interface|implementation|protocol|class)[ \t]+([A-Za-z_]\w*)")
_OBJC_PARAM_RE = re.compile(r":[ \t]*\([^()]*(?:\([^()]*\)[^()]*)*\)[ \t]*([A-Za-z_]\w*)")
_IMPORT_RE = re.compile(r"^[ \t]*#[ \t]*(?:import|include)[ \t]*[\"<]([^\">]+)[\">]", re.M)
_PREPROCESSOR_COND_RE = re.compile(r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|undef)\b[^\n]*", re.M)
_DEFINE_LINE_RE = re.compile(r"^[ \t]*#[ \t]*define\b.*(?:\\\n.*)*", re.M)
_OBJC_SIGNATURE_LINE_RE = re.compile(r"^[ \t]*(?:[+-][ \t]*\(|@property\b)[^{;]*", re.M)
_PREFIX_RE = re.compile(r"^_*([A-Z]{2,}(?=[A-Z][a-z]|_|\d|$)|[a-z]{2,}(?=_)|[A-Z][a-z]+(?=_))")
_CLASS_MESSAGE_RE = re.compile(r"\[[ \t\n]*([A-Z][A-Za-z0-9_]*)[ \t\n]+(?=[A-Za-z_])")
_METHOD_IMPL_RE = re.compile(r"^[ \t]*([+-])[ \t]*\(([^()]*(?:\([^()]*\)[^()]*)*)\)([^{;]*)\{", re.M)
_METHOD_DECL_RE = re.compile(r"^[ \t]*([+-])[ \t]*\(([^()]*(?:\([^()]*\)[^()]*)*)\)([^{;]*)[;{]", re.M)
_SELECTOR_PART_RE = re.compile(
    r"([A-Za-z_]\w*)[ \t\n]*:[ \t\n]*(?:\([^()]*(?:\([^()]*\)[^()]*)*\)[ \t\n]*)?[A-Za-z_]\w*"
)


def _blank(text: str) -> str:
    """Replace every non-newline character with a space."""
    return re.sub(r"[^\n]", " ", text)


def strip_comments_and_strings(text: str) -> str:
    """Blank out comments and string/char literal bodies, preserving offsets."""
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append(_blank(text[i:end]))
            i = end
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            end = j + 1 if j < n and text[j] == ch else min(j, n)
            segment = text[i:end]
            if len(segment) >= 2 and segment[-1] == ch:
                out.append(ch + _blank(segment[1:-1]) + ch)
            else:
                out.append(ch + _blank(segment[1:]))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _brace_depths(stripped: str) -> List[int]:
    """Return the brace depth before each character of ``stripped``."""
    steps = [1 if ch == "{" else -1 if ch == "}" else 0 for ch in stripped]
    return [0] + list(accumulate(steps))


def _match_brace(stripped: str, open_index: int) -> int:
    """Return the index of the ``}`` closing the ``{`` at ``open_index`` (or -1)."""
    depth = 0
    for index in range(open_index, len(stripped)):
        ch = stripped[index]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _line_of(text: str, pos: int) -> int:
    """Return the 1-based line number of ``pos``."""
    return text.count("\n", 0, pos) + 1


def _type_tokens_ok(type_tokens: str) -> bool:
    """Reject matches whose leading tokens are statements rather than types."""
    words = _IDENT_RE.findall(type_tokens)
    return bool(words) and not any(word in _STATEMENT_KEYWORDS for word in words)


def _function_definitions(stripped: str) -> List[Dict[str, Any]]:
    """Return C function definitions with their brace depth and staticness."""
    depths = _brace_depths(stripped)
    found: List[Dict[str, Any]] = []
    for match in _FUNC_DEF_RE.finditer(stripped):
        type_tokens, name, params = match.group(1), match.group(2), match.group(3)
        if name in _C_KEYWORDS or not _type_tokens_ok(type_tokens):
            continue
        open_index = match.end() - 1
        found.append({
            "name": name,
            "params": re.sub(r"\s+", " ", params).strip(),
            "static": "static" in _IDENT_RE.findall(type_tokens),
            "depth": depths[match.start(2)],
            "pos": match.start(2),
            "start": match.start(1),
            "open": open_index,
        })
    return found


def _function_body_spans(stripped: str, defs: Sequence[Dict[str, Any]]) -> List[Tuple[int, int]]:
    """Return ``(open, close)`` spans for depth-0 function bodies."""
    spans: List[Tuple[int, int]] = []
    for item in defs:
        if item["depth"] != 0:
            continue
        close = _match_brace(stripped, item["open"])
        if close > 0:
            spans.append((item["open"], close))
    return spans


def declaration_positions(stripped: str) -> Dict[str, int]:
    """Map every name the text declares (macros, functions, variables, types...) to its first offset."""
    found: Dict[str, int] = {}

    def _add(name: str, pos: int) -> None:
        """Record ``name`` at ``pos`` unless an earlier declaration exists."""
        if name not in _C_KEYWORDS and (name not in found or pos < found[name]):
            found[name] = pos

    for match in _DEFINE_RE.finditer(stripped):
        _add(match.group(1), match.start(1))
    for match in _FUNC_DECL_RE.finditer(stripped):
        if _type_tokens_ok(match.group(1)):
            _add(match.group(2), match.start(2))
    for match in _VAR_DECL_RE.finditer(stripped):
        if _type_tokens_ok(match.group(1)):
            _add(match.group(2), match.start(2))
            for name, pos in _extra_declarators(stripped, match.end(2)):
                _add(name, pos)
    for body in _ENUM_BODY_RE.finditer(stripped):
        for match in _ENUM_ITEM_RE.finditer(body.group(1)):
            _add(match.group(1), body.start(1) + match.start(1))
    for regex in (_TYPEDEF_CLOSE_RE, _TYPEDEF_RE, _FUNC_PTR_RE, _TAG_RE, _OBJC_DECL_RE, _OBJC_PARAM_RE):
        for match in regex.finditer(stripped):
            _add(match.group(1), match.start(1))
    return found


def _extra_declarators(stripped: str, pos: int) -> List[Tuple[str, int]]:
    """Return the further declarators of ``int a = 1, b, *c;`` starting after the first name."""
    end = stripped.find(";", pos)
    if end < 0 or end - pos > 400:
        return []
    found: List[Tuple[str, int]] = []
    depth = 0
    for index in range(pos, end):
        ch = stripped[index]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return found
        elif ch == "," and depth == 0:
            match = _DECLARATOR_RE.match(stripped, index + 1)
            if match:
                found.append((match.group(1), match.start(1)))
    return found


def declared_names(stripped: str) -> Set[str]:
    """Collect every name the text declares."""
    return set(declaration_positions(stripped))


def _owned_names(stripped: str) -> Set[str]:
    """Names this file defines at file scope (used to derive project prefixes)."""
    depths = _brace_depths(stripped)
    names: Set[str] = set(_DEFINE_RE.findall(stripped))
    names.update(item["name"] for item in _function_definitions(stripped) if item["depth"] == 0)
    for match in _VAR_DECL_RE.finditer(stripped):
        if depths[match.start(2)] == 0 and _type_tokens_ok(match.group(1)):
            names.add(match.group(2))
    names.update(_TYPEDEF_RE.findall(stripped))
    names.update(_TYPEDEF_CLOSE_RE.findall(stripped))
    names.update(_OBJC_DECL_RE.findall(stripped))
    return names - _C_KEYWORDS


def name_prefix(name: str) -> str:
    """Return the lower-cased project prefix of ``name`` (``bmk`` for ``BMK_X``/``bmk_y``)."""
    match = _PREFIX_RE.match(name)
    if not match:
        return ""
    prefix = match.group(1).lower()
    if len(prefix) < 3 or prefix in _GENERIC_PREFIXES:
        return ""
    return prefix


def project_prefixes(stripped: str) -> Set[str]:
    """Prefixes shared by at least two names the file defines."""
    counts: Dict[str, int] = {}
    for name in _owned_names(stripped):
        prefix = name_prefix(name)
        if prefix:
            counts[prefix] = counts.get(prefix, 0) + 1
    return {prefix for prefix, count in counts.items() if count >= 2}


def used_identifiers(stripped: str) -> Set[str]:
    """Identifiers used as C symbols (selectors, members and conditionals excluded)."""
    return set(identifier_uses(stripped))


def identifier_uses(stripped: str) -> Dict[str, int]:
    """Map identifiers used as C symbols to their first offset."""
    text = _PREPROCESSOR_COND_RE.sub(lambda m: _blank(m.group(0)), stripped)
    text = _DEFINE_LINE_RE.sub(lambda m: _blank(m.group(0)), text)
    text = _OBJC_SIGNATURE_LINE_RE.sub(lambda m: _blank(m.group(0)), text)
    used: Dict[str, int] = {}
    for match in _IDENT_RE.finditer(text):
        name = match.group(0)
        if name in _C_KEYWORDS or name in used:
            continue
        before = text[max(0, match.start() - 2):match.start()]
        if before.endswith((".", "->", "@", "::")):
            continue
        after = text[match.end():match.end() + 64]
        if re.match(r"[ \t\n]*:(?!:)", after):
            continue
        prefix_text = text[max(0, match.start() - 64):match.start()]
        if re.match(r"[ \t\n]*\]", after) and re.search(r"[\w\]\)][ \t\n]+$", prefix_text):
            continue
        used[name] = match.start()
    return used


def _used_before_declaration(stripped: str) -> Set[str]:
    """Names whose first use precedes their first declaration in the same text."""
    uses = identifier_uses(stripped)
    return {name for name, pos in declaration_positions(stripped).items() if uses.get(name, pos) < pos}


def _flatten_nested(inner: str) -> str:
    """Blank the contents of nested (), [] and {} groups inside a message."""
    out: List[str] = []
    depth = 0
    for ch in inner:
        if ch in "([{":
            depth += 1
            out.append(" ")
        elif ch in ")]}":
            depth = max(0, depth - 1)
            out.append(" ")
        else:
            out.append(" " if depth else ch)
    return "".join(out)


def message_selector(inner: str) -> str:
    """Return the selector of an Objective-C message body (receiver removed)."""
    flat = _flatten_nested(inner)
    parts = re.findall(r"([A-Za-z_]\w*)[ \t\n]*:(?!:)", flat)
    if parts:
        return "".join(part + ":" for part in parts)
    match = re.match(r"[ \t\n]*([A-Za-z_]\w*)[ \t\n]*$", flat)
    return match.group(1) if match else ""


def class_messages(stripped: str) -> Dict[Tuple[str, str], int]:
    """Map ``(ClassName, selector)`` of class messages to their first offset."""
    found: Dict[Tuple[str, str], int] = {}
    for match in _CLASS_MESSAGE_RE.finditer(stripped):
        open_index = match.start()
        depth = 0
        close = -1
        for index in range(open_index, len(stripped)):
            ch = stripped[index]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    close = index
                    break
        if close < 0:
            continue
        selector = message_selector(stripped[match.end():close])
        if selector:
            found.setdefault((match.group(1), selector), open_index)
    return found


def method_selector(rest: str) -> str:
    """Return the selector of a method signature tail (text after the return type)."""
    parts = _SELECTOR_PART_RE.findall(rest)
    if parts:
        return "".join(part + ":" for part in parts)
    match = re.match(r"[ \t\n]*([A-Za-z_]\w*)", rest)
    return match.group(1) if match else ""


def _block_regions(stripped: str, keyword: str, class_name: str, *, category: Optional[bool] = None
                   ) -> List[Tuple[int, int]]:
    """Return ``@interface``/``@implementation`` ... ``@end`` regions of ``class_name``."""
    regions: List[Tuple[int, int]] = []
    pattern = re.compile(r"@" + keyword + r"[ \t]+" + re.escape(class_name) + r"\b([ \t]*\()?")
    for match in pattern.finditer(stripped):
        is_category = bool(match.group(1))
        if category is not None and is_category != category:
            continue
        end = stripped.find("@end", match.end())
        regions.append((match.start(), end if end >= 0 else len(stripped)))
    return regions


def declared_selectors(stripped: str, class_name: str) -> Set[Tuple[str, str]]:
    """Return ``(sign, selector)`` pairs declared in ``@interface class_name`` blocks."""
    found: Set[Tuple[str, str]] = set()
    for start, end in _block_regions(stripped, "interface", class_name):
        for match in _METHOD_DECL_RE.finditer(stripped, start, end):
            found.add((match.group(1), method_selector(match.group(3))))
    return found


def implemented_methods(text: str, stripped: str, class_name: str) -> Dict[Tuple[str, str], str]:
    """Map ``(sign, selector)`` implemented for ``class_name`` to a declaration line."""
    found: Dict[Tuple[str, str], str] = {}
    for start, end in _block_regions(stripped, "implementation", class_name):
        for match in _METHOD_IMPL_RE.finditer(stripped, start, end):
            signature = re.sub(r"\s+", " ", text[match.start(1):match.end() - 1]).strip()
            found.setdefault((match.group(1), method_selector(match.group(3))), signature + ";")
    return found


def _read_text(path: Path) -> Optional[str]:
    """Read a source file as text, or None when it is not valid UTF-8."""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


class _SourceIndex:
    """Lazily indexed project sources around the changed files."""

    def __init__(self, code_roots: Sequence[str]) -> None:
        """Remember the code roots that bound every search."""
        self.code_roots = [Path(root).expanduser().resolve() for root in code_roots if str(root).strip()]
        self._by_root: Dict[Path, Dict[str, List[Path]]] = {}
        self._texts: Dict[Path, Tuple[str, str]] = {}

    def code_root_for(self, path: Path) -> Optional[Path]:
        """Return the code root that contains ``path``."""
        for root in self.code_roots:
            try:
                path.relative_to(root)
                return root
            except ValueError:
                continue
        return None

    def search_root_for(self, path: Path) -> Optional[Path]:
        """Ancestor directory (bounded by the code root) used for header lookup."""
        code_root = self.code_root_for(path)
        if code_root is None:
            return None
        current = path.parent
        for _ in range(_SEARCH_LEVELS):
            if current == code_root or code_root not in current.parents:
                break
            current = current.parent
        return current

    def files(self, search_root: Path) -> Dict[str, List[Path]]:
        """Return basename -> paths for indexed sources below ``search_root``."""
        if search_root in self._by_root:
            return self._by_root[search_root]
        mapping: Dict[str, List[Path]] = {}
        count = 0
        for dirpath, dirnames, filenames in os.walk(search_root):
            dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS and not name.startswith(".")]
            for filename in filenames:
                if Path(filename).suffix.lower() not in _INDEX_SUFFIXES:
                    continue
                mapping.setdefault(filename, []).append(Path(dirpath) / filename)
                count += 1
                if count >= _MAX_INDEX_FILES:
                    break
            if count >= _MAX_INDEX_FILES:
                break
        self._by_root[search_root] = mapping
        return mapping

    def all_files(self, search_root: Path) -> Iterable[Path]:
        """Iterate every indexed source below ``search_root``."""
        for paths in self.files(search_root).values():
            yield from paths

    def text(self, path: Path) -> Tuple[str, str]:
        """Return ``(text, stripped)`` for ``path`` (empty when unreadable)."""
        if path not in self._texts:
            text = _read_text(path) or ""
            self._texts[path] = (text, strip_comments_and_strings(text))
        return self._texts[path]

    def forget(self, path: Path) -> None:
        """Drop the cached text of a file that was just rewritten."""
        self._texts.pop(path, None)

    def resolve_import(self, importer: Path, name: str, search_root: Path) -> Optional[Path]:
        """Resolve a quoted or framework-style import to a project header."""
        direct = (importer.parent / name)
        if direct.is_file():
            return direct.resolve()
        candidates = self.files(search_root).get(Path(name).name) or []
        return candidates[0].resolve() if candidates else None

    def visible_headers(self, path: Path, search_root: Path) -> List[Path]:
        """Project headers reachable through the file's imports (bounded depth)."""
        seen: List[Path] = []
        frontier: List[Path] = [path]
        for _ in range(_IMPORT_DEPTH):
            next_frontier: List[Path] = []
            for importer in frontier:
                for name in _imports(*self.text(importer)):
                    header = self.resolve_import(importer, name, search_root)
                    if header is None or header in seen or header == path:
                        continue
                    seen.append(header)
                    next_frontier.append(header)
            frontier = next_frontier
        return seen


def _imports(text: str, stripped: str) -> List[str]:
    """Return ``#import``/``#include`` targets that are not inside comments."""
    return [
        match.group(1) for match in _IMPORT_RE.finditer(text)
        if "#" in stripped[match.start():match.end()]
    ]


def _issue(rule: str, file_path: Path, text: str, pos: int, symbol: str, message: str) -> Dict[str, Any]:
    """Build one issue record."""
    return {
        "rule": rule,
        "file": str(file_path),
        "line": _line_of(text, pos) if pos >= 0 else 0,
        "symbol": symbol,
        "message": message,
    }


def _structural_findings(stripped: str, suffix: str) -> Dict[Tuple[str, str], int]:
    """Return structural problems keyed by ``(rule, symbol)`` with an offset."""
    findings: Dict[Tuple[str, str], int] = {}
    depths = _brace_depths(stripped)
    if depths[-1] != 0 or min(depths) < 0:
        negative = next((index for index, depth in enumerate(depths) if depth < 0), len(stripped))
        findings[("brace_balance", "")] = max(0, negative - 1)
    defs = _function_definitions(stripped)
    spans = _function_body_spans(stripped, defs)
    overloadable = suffix in {".mm", ".cc", ".cpp", ".hpp"}
    seen: Dict[str, int] = {}
    for item in defs:
        inside = any(start < item["pos"] < end for start, end in spans)
        if inside:
            findings.setdefault(("nested_definition", item["name"]), item["pos"])
            continue
        key = item["name"] + ("(" + item["params"] + ")" if overloadable else "")
        if key in seen:
            findings.setdefault(("duplicate_definition", key), item["pos"])
        seen.setdefault(key, item["pos"])
    return findings


_SIGNAL_HANDLER_NAME_RE = re.compile(
    r"(?:signal\w*handler|handler\w*signal|bmk_sig_)",
    re.I,
)
_NOT_ASYNC_SIGNAL_NAME_RE = re.compile(
    r"(?:register|unregister|install|prepare|convert|setup|init|enable|disable)",
    re.I,
)
_C_BOOL_TYPE_RE = re.compile(r"\b(?:bool|_Bool)\b")
_C_TRUE_FALSE_RE = re.compile(r"\b(?:true|false)\b")
_FORBIDDEN_IN_SIGNAL_HANDLER = re.compile(
    r"\b(?:malloc|calloc|realloc|free|NSThread|NSLog|objc_msgSend|"
    r"callStackSymbols|callStackReturnAddresses|bmkmc_suspendEnvironment|"
    r"NSMutableArray|NSMutableDictionary|dispatch_\w+)\b"
    r"|\[(?:[A-Z][A-Za-z0-9_]*)[ \t\n]+[A-Za-z_]"
)


def _function_span_text(stripped: str, item: Dict[str, Any]) -> str:
    """Return the body text of a function definition."""
    close = _match_brace(stripped, item["open"])
    if close < 0:
        return ""
    return stripped[item["open"]:close + 1]


def _is_signal_handler_name(name: str, params: str = "") -> bool:
    """Whether the function is the async signal callback, not register/setup helpers."""
    if _NOT_ASYNC_SIGNAL_NAME_RE.search(name or ""):
        return False
    if re.search(r"\bsiginfo_t\b", params or ""):
        return True
    return bool(_SIGNAL_HANDLER_NAME_RE.search(name or ""))


_STRUCTURAL_MESSAGES = {
    "brace_balance": "花括号不配对，补丁可能截断或多插入了代码块",
    "nested_definition": "函数定义出现在另一个函数体内，补丁插入位置错误",
    "duplicate_definition": "同一文件中存在重复的函数定义",
}


class PatchStaticChecker:
    """Run the static checks for one applied patch."""

    def __init__(self, code_roots: Sequence[str], report_dir: Optional[Path] = None, auto_fix: bool = True) -> None:
        """Configure search roots, the backup directory and auto-fix behaviour."""
        self.index = _SourceIndex(code_roots)
        self.report_dir = Path(report_dir) if report_dir is not None else None
        self.auto_fix = auto_fix and self.report_dir is not None
        self.issues: List[Dict[str, Any]] = []
        self.warnings: List[Dict[str, Any]] = []
        self.auto_fixes: List[Dict[str, Any]] = []
        self._pending_headers: Dict[Path, List[Tuple[str, str, str]]] = {}
        self._pending_prototypes: Dict[Path, List[str]] = {}
        self._originals: Dict[Path, str] = {}

    def check_file(self, path: Path, original: Optional[str]) -> None:
        """Check one changed file against its pre-patch text."""
        text, stripped = self.index.text(path)
        if not text:
            return
        if original is not None:
            self._originals[path] = original
        original_stripped = strip_comments_and_strings(original) if original is not None else stripped
        suffix = path.suffix.lower()
        before = _structural_findings(original_stripped, suffix)
        for key, pos in _structural_findings(stripped, suffix).items():
            if key not in before:
                self.issues.append(_issue(key[0], path, text, pos, key[1], _STRUCTURAL_MESSAGES[key[0]]))
        if suffix not in _SYMBOL_CHECK_SUFFIXES:
            return
        search_root = self.index.search_root_for(path)
        if search_root is None:
            return
        headers = self.index.visible_headers(path, search_root)
        self._check_symbols(path, text, stripped, original_stripped, headers, search_root)
        self._check_class_messages(path, text, stripped, original_stripped, headers, search_root)
        self._check_static_visibility(path, text, stripped, original_stripped, headers, search_root)
        self._check_signal_handler_safety(path, text, stripped, original_stripped)
        self._check_objc_bool_style(path, text, stripped, original_stripped)

    def _check_symbols(self, path: Path, text: str, stripped: str, original_stripped: str,
                       headers: Sequence[Path], search_root: Path) -> None:
        """Flag project-prefixed identifiers the patch introduced without declaring them."""
        prefixes = project_prefixes(original_stripped) | project_prefixes(stripped)
        if not prefixes:
            return
        uses = identifier_uses(stripped)
        header_names: Set[str] = set()
        for header in headers:
            header_names |= declared_names(self.index.text(header)[1])
        declared_here = declaration_positions(stripped)
        late_before = _used_before_declaration(original_stripped)
        top_level_functions = {item["name"] for item in _function_definitions(stripped) if item["depth"] == 0}
        for name, decl_pos in sorted(declared_here.items(), key=lambda item: item[1]):
            use_pos = uses.get(name, decl_pos)
            if (use_pos < decl_pos and name_prefix(name) in prefixes and name not in header_names
                    and name not in late_before):
                if self.auto_fix and name in top_level_functions:
                    self._pending_prototypes.setdefault(path, []).append(name)
                    continue
                self.issues.append(_issue(
                    "used_before_declaration", path, text, use_pos, name,
                    f"`{name}` 在第 {_line_of(text, decl_pos)} 行才声明/定义，但在此之前已被使用，"
                    "需要前置声明或把定义移到使用之前",
                ))
        introduced = set(uses) - used_identifiers(original_stripped)
        candidates = {name for name in introduced if name_prefix(name) in prefixes}
        candidates -= set(declared_here) | header_names
        if not candidates:
            return
        found_in_header: Set[str] = set()
        found_in_source: Dict[str, Path] = {}
        pattern = re.compile(r"\b(" + "|".join(re.escape(name) for name in sorted(candidates)) + r")\b")
        for other in self.index.all_files(search_root):
            if other.resolve() == path:
                continue
            other_stripped = self.index.text(other.resolve())[1]
            for name in set(pattern.findall(other_stripped)):
                if other.suffix.lower() in _HEADER_SUFFIXES:
                    found_in_header.add(name)
                else:
                    found_in_source.setdefault(name, other)
        for name in sorted(candidates):
            pos = re.search(r"\b" + re.escape(name) + r"\b", stripped)
            offset = pos.start() if pos else -1
            if name in found_in_header:
                self.warnings.append(_issue(
                    "symbol_from_unimported_header", path, text, offset, name,
                    f"`{name}` 只在未直接 import 的头文件中声明（可能依赖 pch），请确认可见",
                ))
            elif name in found_in_source:
                self.issues.append(_issue(
                    "undeclared_symbol", path, text, offset, name,
                    f"`{name}` 未在本文件或其 import 的头文件中声明（仅定义在 {found_in_source[name].name}）",
                ))
            else:
                self.issues.append(_issue(
                    "undeclared_symbol", path, text, offset, name,
                    f"`{name}` 在本文件和工程中都没有声明或定义，补丁漏掉了它的宏/类型/变量/函数定义",
                ))

    def _class_header(self, class_name: str, search_root: Path, headers: Sequence[Path]) -> Optional[Path]:
        """Return the header holding the primary ``@interface class_name``."""
        candidates = list(headers) + [
            path.resolve() for path in self.index.files(search_root).get(class_name + ".h") or []
        ]
        for header in candidates:
            if _block_regions(self.index.text(header)[1], "interface", class_name, category=False):
                return header
        return None

    def _class_implementation(self, class_name: str, search_root: Path) -> Dict[Tuple[str, str], str]:
        """Return the implemented methods of ``class_name`` in the project."""
        files = self.index.files(search_root)
        candidates = [path.resolve() for suffix in (".m", ".mm") for path in files.get(class_name + suffix) or []]
        for path in candidates:
            text, stripped = self.index.text(path)
            methods = implemented_methods(text, stripped, class_name)
            if methods:
                return methods
        return {}

    def _check_class_messages(self, path: Path, text: str, stripped: str, original_stripped: str,
                              headers: Sequence[Path], search_root: Path) -> None:
        """Ensure new ``[ProjectClass selector]`` messages are declared in the class header."""
        before = class_messages(original_stripped)
        for (class_name, selector), pos in sorted(class_messages(stripped).items(), key=lambda item: item[1]):
            if (class_name, selector) in before:
                continue
            if _block_regions(stripped, "implementation", class_name):
                continue
            declared = declared_selectors(stripped, class_name)
            for header in headers:
                declared |= declared_selectors(self.index.text(header)[1], class_name)
            if ("+", selector) in declared:
                continue
            implemented = self._class_implementation(class_name, search_root)
            declaration = implemented.get(("+", selector))
            if declaration is None:
                continue
            header = self._class_header(class_name, search_root, headers)
            if header is not None and header in headers and self.auto_fix:
                self._pending_headers.setdefault(header, []).append((class_name, selector, declaration))
                continue
            self.issues.append(_issue(
                "objc_undeclared_method", path, text, pos, f"+[{class_name} {selector}]",
                f"`+[{class_name} {selector}]` 只在 .m 中实现，{class_name}.h 未声明，ARC 下会编译失败",
            ))

    def _check_static_visibility(self, path: Path, text: str, stripped: str, original_stripped: str,
                                 headers: Sequence[Path], search_root: Path) -> None:
        """Flag functions made ``static`` while other translation units still use them."""
        before = {item["name"]: item for item in _function_definitions(original_stripped) if item["depth"] == 0}
        for item in _function_definitions(stripped):
            old = before.get(item["name"])
            if item["depth"] != 0 or not item["static"] or old is None or old["static"]:
                continue
            pattern = re.compile(r"\b" + re.escape(item["name"]) + r"[ \t\n]*\(")
            users = [header.name for header in headers if pattern.search(self.index.text(header)[1])]
            for other in self.index.all_files(search_root):
                resolved = other.resolve()
                if resolved != path and resolved not in headers and pattern.search(self.index.text(resolved)[1]):
                    users.append(other.name)
            if users:
                self.issues.append(_issue(
                    "static_visibility_conflict", path, text, item["pos"], item["name"],
                    f"`{item['name']}` 被改成 static，但 {', '.join(sorted(set(users))[:5])} 仍在声明/调用它",
                ))

    def _check_signal_handler_safety(self, path: Path, text: str, stripped: str,
                                    original_stripped: str) -> None:
        """Flag new async-signal-unsafe calls inside signal handlers."""
        before = {
            item["name"]: _function_span_text(original_stripped, item)
            for item in _function_definitions(original_stripped) if item["depth"] == 0
        }
        for item in _function_definitions(stripped):
            if item["depth"] != 0 or not _is_signal_handler_name(item["name"], item.get("params", "")):
                continue
            body = _function_span_text(stripped, item)
            old_body = before.get(item["name"], "")
            old_hits = {hit.group(0) for hit in _FORBIDDEN_IN_SIGNAL_HANDLER.finditer(old_body)}
            for match in _FORBIDDEN_IN_SIGNAL_HANDLER.finditer(body):
                token = match.group(0)
                if token in old_hits:
                    continue
                self.issues.append(_issue(
                    "signal_unsafe_call", path, text, item["open"] + match.start(), item["name"],
                    f"`{item['name']}` 是信号处理函数，补丁新增了非 async-signal-safe 调用 `{token.strip()}`",
                ))

    def _check_objc_bool_style(self, path: Path, text: str, stripped: str,
                               original_stripped: str) -> None:
        """Flag new C ``bool`` / ``true`` / ``false`` in ObjC files that use ``BOOL``."""
        if path.suffix.lower() not in _OBJC_IMPL_SUFFIXES:
            return
        if _C_BOOL_TYPE_RE.search(original_stripped):
            return
        for pattern in (_C_BOOL_TYPE_RE, _C_TRUE_FALSE_RE):
            for match in pattern.finditer(stripped):
                token = match.group(0)
                self.issues.append(_issue(
                    "objc_bool_style", path, text, match.start(), token,
                    f"`.m` / `.mm` 真假值应使用 BOOL / YES / NO，补丁新增了 `{token}`",
                ))

    def _backup(self, header: Path, original: str) -> str:
        """Back up the pre-fix header under ``original_sources`` (first write wins)."""
        assert self.report_dir is not None
        code_root = self.index.code_root_for(header)
        relative = header.relative_to(code_root) if code_root is not None else Path(header.name)
        backup = self.report_dir / "original_sources" / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            backup.write_bytes(original.encode("utf-8"))
        return str(backup)

    def apply_header_fixes(self) -> None:
        """Insert missing class-method declarations into their headers."""
        for header, entries in self._pending_headers.items():
            text = self.index.text(header)[0]
            stripped = self.index.text(header)[1]
            original = text
            added: List[str] = []
            for class_name, selector, declaration in entries:
                if ("+", selector) in declared_selectors(stripped, class_name) or declaration in added:
                    continue
                regions = _block_regions(stripped, "interface", class_name, category=False)
                if not regions or regions[0][1] >= len(stripped):
                    continue
                at = regions[0][1]
                text = text[:at].rstrip("\n") + "\n\n" + declaration + "\n\n" + text[at:]
                stripped = strip_comments_and_strings(text)
                added.append(declaration)
            if not added:
                continue
            backup_path = self._backup(header, original)
            header.write_bytes(text.encode("utf-8"))
            self.index.forget(header)
            self.auto_fixes.append({
                "file": str(header),
                "edit_type": "header_declaration",
                "function_signature": "; ".join(added),
                "reason": "static_check_header_declaration",
                "status": "applied",
                "backup_path": backup_path,
                "replaced_preview": "\n".join(added),
            })
        self._pending_headers.clear()

    def apply_prototype_fixes(self) -> None:
        """Insert forward declarations for functions the patch uses before defining them."""
        for path, names in self._pending_prototypes.items():
            text = self.index.text(path)[0]
            added: List[str] = []
            for name in dict.fromkeys(names):
                stripped = strip_comments_and_strings(text)
                defs = [item for item in _function_definitions(stripped) if item["depth"] == 0]
                target = next((item for item in defs if item["name"] == name), None)
                use_pos = identifier_uses(stripped).get(name, -1)
                if target is None or not 0 <= use_pos < target["pos"]:
                    continue
                container = next(
                    (item for item in defs if item["start"] <= use_pos <= _match_brace(stripped, item["open"])),
                    None,
                )
                at = _line_start_before_comments(text, container["start"] if container else use_pos)
                if _brace_depths(stripped)[at] != 0:
                    continue
                header_lines = text[target["start"]:target["open"]].strip().splitlines()
                while len(header_lines) > 1 and re.fullmatch(r"[ \t]*[A-Z][A-Z0-9_]*[ \t]*", header_lines[0]):
                    header_lines.pop(0)
                prototype = re.sub(r"\s+", " ", " ".join(header_lines)).strip() + ";"
                text = text[:at] + prototype + "\n\n" + text[at:]
                added.append(prototype)
            leftover = [name for name in names if not any(_declares(proto, name) for proto in added)]
            for name in dict.fromkeys(leftover):
                self.issues.append(_issue(
                    "used_before_declaration", path, text, -1, name,
                    f"`{name}` 在定义之前已被使用，且无法自动插入前置声明",
                ))
            if not added:
                continue
            backup_path = self._backup(path, self._originals.get(path) or self.index.text(path)[0])
            path.write_bytes(text.encode("utf-8"))
            self.index.forget(path)
            self.auto_fixes.append({
                "file": str(path),
                "edit_type": "forward_declaration",
                "function_signature": "; ".join(added),
                "reason": "static_check_forward_declaration",
                "status": "applied",
                "backup_path": backup_path,
                "replaced_preview": "\n".join(added),
            })
        self._pending_prototypes.clear()


def _declares(prototype: str, name: str) -> bool:
    """Whether ``prototype`` declares the function ``name``."""
    return re.search(r"\b" + re.escape(name) + r"[ \t]*\(", prototype) is not None


def _line_start_before_comments(text: str, pos: int) -> int:
    """Start of the line holding ``pos``, moved above directly attached comment lines."""
    start = text.rfind("\n", 0, pos) + 1
    while start > 0:
        prev_start = text.rfind("\n", 0, start - 1) + 1
        line = text[prev_start:start - 1].strip()
        if not line or not line.startswith(("//", "/*", "*", "*/")):
            break
        start = prev_start
    return start


def _changed_sources(applied_fix_result: Optional[Dict[str, Any]]) -> List[Tuple[Path, Optional[str]]]:
    """Return applied C-family files with their pre-patch text (when backed up)."""
    rows = (applied_fix_result or {}).get("applied")
    out: Dict[Path, Optional[str]] = {}
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict) or item.get("status") != "applied" or not item.get("file"):
            continue
        path = Path(str(item["file"])).expanduser().resolve()
        if path.suffix.lower() not in SOURCE_SUFFIXES or not path.is_file():
            continue
        backup = str(item.get("backup_path") or "").strip()
        original = _read_text(Path(backup)) if backup and Path(backup).is_file() else None
        if out.get(path) is None:
            out[path] = original
    return list(out.items())


def run_patch_static_check(
    applied_fix_result: Optional[Dict[str, Any]],
    *,
    code_roots: Sequence[str],
    report_dir: Optional[Path] = None,
    auto_fix: bool = True,
) -> Dict[str, Any]:
    """Check the applied patch; auto-declare missing header methods when possible."""
    files = _changed_sources(applied_fix_result)
    if not files:
        return {"status": "skipped", "summary": "没有需要静态检查的 C/ObjC 修改", "checked_files": [],
                "issues": [], "warnings": [], "auto_fixes": []}
    checker = PatchStaticChecker(code_roots, report_dir=report_dir, auto_fix=auto_fix)
    for path, original in files:
        checker.check_file(path, original)
    checker.apply_prototype_fixes()
    checker.apply_header_fixes()
    status = "failed" if checker.issues else "passed"
    if checker.issues:
        summary = f"补丁静态检查发现 {len(checker.issues)} 个必然编译失败的问题"
    elif checker.auto_fixes:
        summary = f"补丁静态检查通过（已自动补充 {len(checker.auto_fixes)} 处声明）"
    else:
        summary = "补丁静态检查通过"
    return {
        "status": status,
        "summary": summary,
        "checked_files": [str(path) for path, _ in files],
        "issues": checker.issues,
        "warnings": checker.warnings,
        "auto_fixes": checker.auto_fixes,
    }


def format_static_issues(report: Dict[str, Any]) -> str:
    """Render issues as compiler-like lines for the repair feedback prompt."""
    lines = []
    for item in report.get("issues") or []:
        lines.append(f"{item.get('file')}:{item.get('line')}: error: [{item.get('rule')}] {item.get('message')}")
    return "\n".join(lines)
