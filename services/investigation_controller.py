"""Bounded, crash-aware investigation planning.

The controller supplies the same navigation affordances users expect from a
coding agent, but it never executes a tool or invents a command.  Its output
is an ordered set of typed requests that ContextEngine may validate and run.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union


@dataclass(frozen=True)
class InvestigationAction:
    kind: str
    target: str = ""
    reason: str = ""
    priority: str = "normal"
    hypothesis_id: str = ""
    expected_return_form: str = ""

    def to_request(self) -> Dict[str, Any]:
        """Convert a suggestion to the legacy ContextRequest shape."""
        request_type = {
            "locate": "function",
            "find_callers": "callers",
            "find_references": "references",
            "inspect_field": "field",
            "inspect_tests": "grep",
            "read_candidate": "read_file",
        }.get(self.kind, "grep")
        payload: Dict[str, Any] = {
            "type": request_type,
            "symbol": self.target if request_type != "read_file" else "",
            "reason": self.reason,
            "priority": self.priority,
        }
        if request_type == "read_file":
            payload["file"] = self.target
        if self.expected_return_form:
            payload["expected_return_form"] = self.expected_return_form
        return payload

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class InvestigationState:
    actions: List[Dict[str, Any]] = field(default_factory=list)
    completed: List[str] = field(default_factory=list)
    blocked: List[str] = field(default_factory=list)
    round_index: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "actions": list(self.actions),
            "completed": list(self.completed),
            "blocked": list(self.blocked),
            "round_index": self.round_index,
        }


class InvestigationController:
    """Create deterministic next-best navigation actions from crash anchors."""

    def __init__(self, *, max_actions: int = 6, max_failures: int = 2):
        self.max_actions = max(1, min(int(max_actions), 16))
        self.max_failures = max(1, min(int(max_failures), 8))
        self.state = InvestigationState()
        self._failures: Dict[str, int] = {}

    @staticmethod
    def _values(anchors: Mapping[str, Any], key: str) -> List[str]:
        raw = anchors.get(key) or []
        values: List[str] = []
        for item in raw if isinstance(raw, list) else [raw]:
            if isinstance(item, dict):
                item = item.get("function") or item.get("symbol") or item.get("name")
            text = str(item or "").strip()
            if text and text not in values:
                values.append(text)
        return values

    def plan(self, anchors: Optional[Mapping[str, Any]] = None,
             candidates: Optional[Iterable[Mapping[str, Any]]] = None,
             *, round_index: int = 0) -> List[InvestigationAction]:
        value = anchors if isinstance(anchors, Mapping) else {}
        self.state.round_index = int(round_index)
        symbols = self._values(value, "stack_symbols") or self._values(value, "stack_frames")
        fields = self._values(value, "fields")
        hypotheses = value.get("hypotheses") if isinstance(value.get("hypotheses"), list) else []
        hypothesis_id = str(hypotheses[0].get("id") or "") if hypotheses and isinstance(hypotheses[0], dict) else ""
        actions: List[InvestigationAction] = []
        if symbols:
            symbol = symbols[0]
            actions.append(InvestigationAction("locate", symbol, "确认崩溃栈帧对应的唯一函数定义", "critical", hypothesis_id, "function_source"))
            actions.append(InvestigationAction("find_callers", symbol, "确认调用线程、异步入口和生命周期路径", "high", hypothesis_id, "caller_snippets"))
            actions.append(InvestigationAction("find_references", symbol, "确认函数及相关对象的跨文件引用", "high", hypothesis_id, "read_write_references"))
        for field in fields[:2]:
            actions.append(InvestigationAction("inspect_field", field, "确认字段声明、初始化和释放关系", "high", hypothesis_id, "member_declaration"))
        if any(str(x).lower() in {"uaf", "use_after_free", "deadlock", "race", "oob"} for x in self._values(value, "problem_types")):
            actions.append(InvestigationAction("inspect_tests", "test", "查找覆盖当前生命周期或并发路径的测试", "normal", hypothesis_id, "grep_matches"))
        for candidate in candidates or ():
            if not isinstance(candidate, Mapping):
                continue
            path = str(candidate.get("file") or "").strip()
            if path:
                actions.append(InvestigationAction("read_candidate", path, "读取 RepoMap 检索出的候选源码", "normal", hypothesis_id, "file_snippet"))
        unique: List[InvestigationAction] = []
        seen = set(self.state.completed) | set(self.state.blocked)
        unique_keys = set()
        for action in actions:
            key = f"{action.kind}:{action.target}"
            if key in seen or key in unique_keys:
                continue
            unique.append(action)
            unique_keys.add(key)
            if len(unique) >= self.max_actions:
                break
        self.state.actions = [item.to_dict() for item in unique]
        return unique

    def record_result(self, action: Union[InvestigationAction, Mapping[str, Any]], *, success: bool,
                      round_index: Optional[int] = None) -> None:
        value = action.to_dict() if isinstance(action, InvestigationAction) else dict(action or {})
        key = f"{value.get('kind')}:{value.get('target')}"
        if success:
            if key not in self.state.completed:
                self.state.completed.append(key)
            self._failures.pop(key, None)
        else:
            self._failures[key] = self._failures.get(key, 0) + 1
            if self._failures[key] >= self.max_failures and key not in self.state.blocked:
                self.state.blocked.append(key)
        if round_index is not None:
            self.state.round_index = int(round_index)
