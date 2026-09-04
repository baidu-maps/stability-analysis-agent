"""Per-run context engineering for the bounded analyze agent loop."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence

from services.agent_output_parser import parse_agent_decision
from services.agent_schema import CONTEXT_REQUEST_TYPES
from services.context_loop_contract import assemble_loop_prompt
from services.context_request_contract import (
    all_context_requests_blocked,
    build_pre_round_add_res,
)
from services.context_compactor import ContextCompactor
from services.context_parts import ContextPart, parts_from_evidence
from services.evidence_graph import EvidenceGraph


TERMINATION_REASONS = frozenset({
    "model_final",
    "max_rounds",
    "all_requests_blocked",
    "invalid_schema",
    "llm_budget_exhausted",
    "llm_error",
    "ready_to_fix",
    "insufficient_evidence",
})


def resolve_agent_loop(
    problem: Optional[Dict[str, Any]] = None,
    *,
    explicit: Optional[str] = None,
) -> str:
    """Default: ``scope=full`` uses bounded ``context_loop`` unless explicitly ``single``."""
    if explicit in {"single", "context_loop"}:
        return explicit
    value = problem if isinstance(problem, dict) else {}
    raw = str(value.get("agent_loop") or "").strip().lower()
    if raw in {"single", "context_loop"}:
        return raw
    scope = str(value.get("scope") or "full").strip().lower()
    prompt_mode = str(value.get("prompt_mode") or "fix").strip().lower()
    if scope == "full" or prompt_mode == "analysis":
        return "context_loop"
    return "single"


def resolve_max_agent_rounds(problem: Optional[Dict[str, Any]]) -> int:
    value = problem if isinstance(problem, dict) else {}
    try:
        configured = int(value.get("max_agent_rounds") or 0)
    except (TypeError, ValueError):
        configured = 0
    if configured > 0:
        return max(1, min(configured, 8))
    scope = str(value.get("scope") or "full").strip().lower()
    prompt_mode = str(value.get("prompt_mode") or "fix").strip().lower()
    if scope == "full" or prompt_mode == "analysis":
        return 5
    return 1


def extract_stack_priority_classes(problem: Optional[Dict[str, Any]]) -> List[str]:
    value = problem if isinstance(problem, dict) else {}
    classes: List[str] = []
    seen: set[str] = set()

    def _add(name: Any) -> None:
        token = str(name or "").strip()
        if token and token not in seen:
            seen.add(token)
            classes.append(token)

    for key in ("stack_priority_classes", "crash_priority_classes"):
        raw = value.get(key)
        if isinstance(raw, list):
            for item in raw:
                _add(item)
    for blob_key in ("parsed_stack", "symbolized_stack", "resolve_stack"):
        blob = value.get(blob_key)
        if not isinstance(blob, dict):
            continue
        for thread in blob.get("threads", []) or []:
            if not isinstance(thread, dict):
                continue
            for frame in thread.get("frames", []) or []:
                if not isinstance(frame, dict):
                    continue
                for token_key in ("function", "symbol", "raw_symbol"):
                    text = str(frame.get(token_key) or "")
                    for match in re.finditer(r"\b([A-Z][A-Za-z0-9_]*(?:::[A-Za-z0-9_~]+)*)\b", text):
                        head = match.group(1).split("::")[0]
                        if head.endswith("Layer") or head.endswith("Control") or head.startswith("C"):
                            _add(head)
    return classes[:12]


@dataclass(frozen=True)
class ContextEngineConfig:
    max_requests: int = 5
    max_chars: int = 24000
    max_tokens: int = 0
    supported_request_types: frozenset[str] = CONTEXT_REQUEST_TYPES

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_requests", max(1, min(int(self.max_requests), 16)))
        object.__setattr__(self, "max_chars", max(4000, int(self.max_chars)))
        object.__setattr__(self, "max_tokens", max(0, int(self.max_tokens)))

    @property
    def effective_max_chars(self) -> int:
        if self.max_tokens:
            return min(self.max_chars, max(4000, self.max_tokens * 4))
        return self.max_chars


@dataclass
class ContextTurn:
    round_index: int
    kind: str
    prompt: str = ""
    analysis: str = ""
    decision: Dict[str, Any] = field(default_factory=dict)
    context_requests: List[Dict[str, Any]] = field(default_factory=list)
    invalid_context_requests: List[Dict[str, Any]] = field(default_factory=list)
    resolved_context: List[Dict[str, Any]] = field(default_factory=list)
    evidence_delta: List[Dict[str, Any]] = field(default_factory=list)
    pre_round_add_res: Optional[Dict[str, Any]] = None
    termination_reason: Optional[str] = None
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    next_action: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["schema_version"] = 2
        value["round"] = self.round_index
        return value


@dataclass
class ContextSession:
    mode: str = "single"
    status: str = "running"
    termination_reason: Optional[str] = None
    turns: List[ContextTurn] = field(default_factory=list)
    request_ledger: List[Dict[str, Any]] = field(default_factory=list)
    budget: Dict[str, int] = field(default_factory=dict)
    stats: Dict[str, int] = field(default_factory=lambda: {
        "model_turns": 0,
        "valid_requests": 0,
        "invalid_requests": 0,
        "resolved_requests": 0,
    })
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    investigation_actions: List[Dict[str, Any]] = field(default_factory=list)
    evidence_confidence: Dict[str, float] = field(default_factory=lambda: {
        "deterministic": 0.0,
        "repository": 0.0,
        "executable": 0.0,
    })
    repo_map: Dict[str, Any] = field(default_factory=dict)
    compaction: Dict[str, Any] = field(default_factory=dict)
    focus_chain: List[Dict[str, Any]] = field(default_factory=list)
    parts: List[Dict[str, Any]] = field(default_factory=list)
    verification_claim: Dict[str, Any] = field(default_factory=dict)
    verification_capabilities: List[Dict[str, Any]] = field(default_factory=list)
    reproduction_plan: Dict[str, Any] = field(default_factory=dict)
    verification_plan_fingerprint: str = ""
    evidence_graph: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": 2,
            "mode": self.mode,
            "status": self.status,
            "termination_reason": self.termination_reason,
            "rounds": [turn.to_dict() for turn in self.turns],
            "request_ledger": list(self.request_ledger),
            "budget": dict(self.budget),
            "stats": dict(self.stats),
            "hypotheses": list(self.hypotheses),
            "investigation_actions": list(self.investigation_actions),
            "evidence_confidence": dict(self.evidence_confidence),
            "repo_map": dict(self.repo_map),
            "compaction": dict(self.compaction),
            "focus_chain": list(self.focus_chain),
            "context_parts": list(self.parts),
            "verification_claim": dict(self.verification_claim),
            "verification_capabilities": list(self.verification_capabilities),
            "reproduction_plan": dict(self.reproduction_plan),
            "verification_plan_fingerprint": self.verification_plan_fingerprint,
            "evidence_graph": dict(self.evidence_graph),
        }


class ContextResolver(Protocol):
    request_type: str

    def resolve(self, request: Dict[str, Any]) -> Dict[str, Any]: ...


@dataclass
class CallableContextResolver:
    request_type: str
    callback: Callable[[Dict[str, Any]], Dict[str, Any]]

    def resolve(self, request: Dict[str, Any]) -> Dict[str, Any]:
        return self.callback(request)


class ContextResolverRegistry:
    def __init__(self, resolvers: Optional[Iterable[ContextResolver]] = None):
        self._resolvers: Dict[str, ContextResolver] = {}
        for resolver in resolvers or ():
            self.register(resolver)

    def register(self, resolver: ContextResolver) -> None:
        request_type = str(resolver.request_type or "").strip().lower()
        if not request_type:
            raise ValueError("context resolver request_type is required")
        self._resolvers[request_type] = resolver

    @property
    def request_types(self) -> frozenset[str]:
        return frozenset(self._resolvers)

    def resolve(self, request: Dict[str, Any]) -> Dict[str, Any]:
        request_type = str(request.get("type") or "function").strip().lower()
        resolver = self._resolvers.get(request_type)
        if resolver is None:
            return {
                "request": dict(request),
                "success": False,
                "rejected": True,
                "reject_reason": "unsupported_request_type",
                "error": f"unsupported context request type: {request_type}",
            }
        result = resolver.resolve(dict(request))
        if not isinstance(result, dict):
            result = {"success": False, "error": "context resolver returned a non-object"}
        result.setdefault("request", dict(request))
        content = str(result.get("content") or result.get("source_content") or "")
        result.setdefault("provider", request_type)
        result.setdefault("source", "context_resolver")
        result.setdefault("evidence_type", "repository")
        result.setdefault("confidence", 0.7 if result.get("success") else 0.0)
        result.setdefault("cost", {"chars": len(content), "tokens": max(1, len(content) // 4) if content else 0})
        result.setdefault("provenance", {
            "file": result.get("file") or request.get("file"),
            "line_start": result.get("line_start") or result.get("line_number") or request.get("line_number"),
            "line_end": result.get("line_end"),
        })
        return result


class RequestLedger:
    def __init__(self):
        self._entries: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def key(request: Dict[str, Any]) -> str:
        request_type = str(request.get("type") or "function").strip().lower()
        symbol = " ".join(str(request.get("symbol") or "").strip().split())
        file_path = str(request.get("file") or "").strip()
        line = int(request.get("line_number") or request.get("line") or 0)
        return f"{request_type}:{file_path}:{line}:{symbol}"

    def get(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        value = self._entries.get(self.key(request))
        return dict(value) if value is not None else None

    def record(self, request: Dict[str, Any], resolution: Dict[str, Any], round_index: int) -> Dict[str, Any]:
        if resolution.get("success"):
            status = "success"
        elif resolution.get("rejected"):
            status = "rejected"
        elif resolution.get("skipped"):
            status = "duplicate"
        elif resolution.get("lookup_exhausted"):
            status = "exhausted"
        else:
            status = "failed"
        entry = {
            "key": self.key(request),
            "type": str(request.get("type") or "function"),
            "symbol": str(request.get("symbol") or ""),
            "file": str(request.get("file") or ""),
            "line_number": int(request.get("line_number") or request.get("line") or 0),
            "status": status,
            "round": int(round_index),
            "error": str(resolution.get("error") or "") or None,
            "attempts": [{
                "round": int(round_index),
                "status": status,
                "error": str(resolution.get("error") or "") or None,
            }],
        }
        self._entries[entry["key"]] = entry
        return dict(entry)

    def record_duplicate(self, request: Dict[str, Any], round_index: int) -> Dict[str, Any]:
        key = self.key(request)
        entry = self._entries[key]
        attempts = list(entry.get("attempts") or [])
        attempts.append({
            "round": int(round_index),
            "status": "duplicate",
            "error": "duplicate context request",
        })
        entry["attempts"] = attempts
        entry["duplicate_count"] = int(entry.get("duplicate_count") or 0) + 1
        return dict(entry)

    def record_rejected(self, request: Dict[str, Any], round_index: int, error: str) -> Dict[str, Any]:
        resolution = {"success": False, "rejected": True, "error": error}
        prior = self._entries.get(self.key(request))
        if prior is None:
            return self.record(request, resolution, round_index)
        attempts = list(prior.get("attempts") or [])
        attempts.append({"round": int(round_index), "status": "rejected", "error": error})
        prior["attempts"] = attempts
        prior["rejected_count"] = int(prior.get("rejected_count") or 0) + 1
        return dict(prior)

    def entries(self) -> List[Dict[str, Any]]:
        return [dict(value) for value in self._entries.values()]

    def markdown(self, *, max_chars: int = 3000) -> str:
        if not self._entries:
            return ""
        lines = ["## 已处理的上下文请求", "以下请求不得在后续轮次重复提出："]
        for item in self._entries.values():
            target = item.get("symbol") or item.get("file") or item.get("key")
            line = f"- `{item.get('type')}: {target}`：{item.get('status')}"
            if item.get("error"):
                line += f"；{item.get('error')}"
            lines.append(line)
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[: max_chars - 24] + "\n...[ledger truncated]"


class ContextEngine:
    """Own context state and request fulfillment without invoking the model."""

    def __init__(
        self,
        config: ContextEngineConfig,
        resolver_registry: ContextResolverRegistry,
        *,
        format_resolution: Optional[Callable[[Dict[str, Any]], str]] = None,
        decision_parser: Optional[Callable[[str], Dict[str, Any]]] = None,
        observation_store: Any = None,
        repo_map: Optional[Dict[str, Any]] = None,
        trace: Any = None,
        evidence_retriever: Any = None,
        investigation_controller: Any = None,
        verification_profile: Any = None,
    ):
        self.config = config
        self.resolvers = resolver_registry
        self.format_resolution = format_resolution or (lambda item: str(item))
        self.decision_parser = decision_parser
        self.observation_store = observation_store
        self.trace = trace
        self.evidence_retriever = evidence_retriever
        self.investigation_controller = investigation_controller
        self.ledger = RequestLedger()
        self.session = ContextSession(mode="context_loop")
        if verification_profile is not None:
            try:
                from services.verification_plan import capabilities_from_profile
                from services.verification_profile import VerificationProfile
                parsed_profile = verification_profile if isinstance(verification_profile, VerificationProfile) else VerificationProfile.from_mapping(verification_profile)
                self.session.verification_capabilities = [item.to_dict() for item in capabilities_from_profile(parsed_profile)]
            except Exception:
                self.session.verification_capabilities = []
        self.session.budget = {
            "max_requests_per_round": config.max_requests,
            "max_chars_per_prompt": config.effective_max_chars,
            "max_tokens_per_prompt": config.max_tokens,
            "prompt_chars": 0,
        }
        self.session.repo_map = dict(repo_map or {})
        self.compactor = ContextCompactor()
        self.evidence_graph = EvidenceGraph()
        if isinstance(self.session.repo_map.get("evidence_graph"), dict):
            self.evidence_graph = EvidenceGraph.from_dict(self.session.repo_map["evidence_graph"])

    def retrieve_evidence(self, anchors: Optional[Dict[str, Any]] = None, *, limit: int = 12) -> List[Dict[str, Any]]:
        """Return ranked repository candidates without invoking an LLM."""
        if self.evidence_retriever is None:
            return []
        try:
            values = self.evidence_retriever.retrieve(anchors or {}, limit=limit)
            return [dict(item) for item in values if isinstance(item, dict)]
        except Exception:
            return []

    def parse_decision(self, analysis_text: str) -> Dict[str, Any]:
        if self.decision_parser is not None:
            parsed = self.decision_parser(analysis_text)
        else:
            parsed = parse_agent_decision(
                analysis_text,
                allowed_types=set(self.config.supported_request_types),
            )
        raw = parsed.get("raw_payload") if isinstance(parsed.get("raw_payload"), dict) else {}
        requests = list(parsed.get("context_requests") or [])[: self.config.max_requests]
        invalid = list(parsed.get("invalid_context_requests") or [])
        wants_more = (
            raw.get("agent_can_fetch_more") is True
            if raw
            else parsed.get("agent_can_fetch_more") is True
        )
        self.session.stats["valid_requests"] += len(requests)
        self.session.stats["invalid_requests"] += len(invalid)
        if self.trace is not None:
            self.trace.emit("agent.decision", kind="agent", name="context_engine", status="success",
                            request_count=len(requests), invalid_count=len(invalid))
        verification_claim = self._normalize_verification_claim(raw.get("verification_claim"))
        if verification_claim:
            self.session.verification_claim = verification_claim
        reproduction_plan = self._normalize_reproduction_plan(raw.get("reproduction_plan"))
        if reproduction_plan:
            capabilities = self.session.verification_capabilities
            allowed = {str(item.get("check_id")) for item in capabilities if isinstance(item, dict)}
            if reproduction_plan.get("check_id") not in allowed:
                reproduction_plan = {}
            else:
                self.session.reproduction_plan = reproduction_plan
                self.session.verification_plan_fingerprint = str(raw.get("verification_plan_fingerprint") or "")
        return {
            **parsed,
            "agent_can_fetch_more": bool(wants_more and requests),
            "requested_more": wants_more,
            "has_control_contract": isinstance(raw.get("agent_can_fetch_more"), bool),
            "context_requests": requests,
            "invalid_context_requests": invalid,
            "hypotheses": self._normalize_hypotheses(raw.get("hypotheses")),
            "next_action": self._normalize_next_action(raw.get("next_action")),
            "verification_claim": verification_claim,
            "reproduction_plan": reproduction_plan,
        }

    @staticmethod
    def _normalize_reproduction_plan(value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        check_id = str(value.get("check_id") or "").strip()
        purpose = str(value.get("purpose") or "").strip()
        if not check_id or purpose not in {"pre_fix_reproduce", "post_fix_verify", "compile", "static_check", "test", "reproduce"}:
            return {}
        # Commands are never accepted from the model decision.
        return {"check_id": check_id, "purpose": purpose}

    @staticmethod
    def _normalize_verification_claim(value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        statement = str(value.get("statement") or value.get("claim") or "").strip()
        if not statement:
            return {}
        evidence = value.get("required_evidence") or []
        if not isinstance(evidence, list):
            evidence = []
        level = str(value.get("minimum_level") or "L1").upper()
        if level not in {"L0", "L1", "L2", "L3", "L4"}:
            level = "L1"
        return {"statement": statement[:1000],
                "required_evidence": [str(x)[:120] for x in evidence if isinstance(x, str)][:16],
                "minimum_level": level}

    @staticmethod
    def _normalize_hypotheses(value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result: List[Dict[str, Any]] = []
        for index, item in enumerate(value[:8]):
            if not isinstance(item, dict):
                continue
            statement = str(item.get("statement") or item.get("hypothesis") or "").strip()
            if not statement:
                continue
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
            result.append({
                "id": str(item.get("id") or f"h{index + 1}"),
                "statement": statement[:1000],
                "status": str(item.get("status") or "open").lower(),
                "confidence": confidence,
                "supporting_evidence": list(item.get("supporting_evidence") or [])[:20],
                "contradicting_evidence": list(item.get("contradicting_evidence") or [])[:20],
                "missing_evidence": list(item.get("missing_evidence") or [])[:20],
                "next_action": str(item.get("next_action") or "")[:200],
            })
        return result

    @staticmethod
    def _normalize_next_action(value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        kind = str(value.get("kind") or "").strip().lower()
        allowed = {
            "inspect", "search", "test", "reproduce", "final",
            "explore", "propose_fix", "insufficient_evidence",
        }
        if kind not in allowed:
            return {}
        return {
            "kind": kind,
            "target": str(value.get("target") or "")[:500],
            "reason": str(value.get("reason") or "")[:1000],
        }

    def update_investigation(self, decision: Dict[str, Any], *, round_index: int, resolved: Sequence[Dict[str, Any]] = ()) -> None:
        hypotheses = self._normalize_hypotheses(decision.get("hypotheses"))
        if hypotheses:
            self.session.hypotheses = hypotheses
            for hypothesis in hypotheses:
                self.evidence_graph.add_node("hypothesis", hypothesis, round=round_index)
        action = self._normalize_next_action(decision.get("next_action"))
        if action:
            self.session.investigation_actions.append({"round": int(round_index), **action})
            self.session.investigation_actions = self.session.investigation_actions[-32:]
        # Keep one explicit, replayable focus for the current investigation.
        if action or hypotheses:
            goal = (action.get("reason") or action.get("target") or
                    (hypotheses[0].get("statement") if hypotheses else "investigate crash root cause"))
            focus = self.session.focus_chain[-1] if self.session.focus_chain else None
            if not focus or focus.get("status") != "open" or focus.get("goal") != goal:
                focus = {"id": f"f{len(self.session.focus_chain) + 1}", "goal": str(goal)[:500],
                         "status": "open", "hypothesis_ids": [h.get("id") for h in hypotheses],
                         "evidence_ids": [], "next_actions": [action.get("kind")] if action else [],
                         "last_updated_round": int(round_index)}
                self.session.focus_chain.append(focus)
            else:
                focus["hypothesis_ids"] = [h.get("id") for h in hypotheses] or focus.get("hypothesis_ids", [])
                focus["next_actions"] = [action.get("kind")] if action else focus.get("next_actions", [])
                focus["last_updated_round"] = int(round_index)
            self.session.focus_chain = self.session.focus_chain[-16:]
        # New hypotheses and focus goals are retrieval signals.  Refresh only
        # the bounded candidate list; source content is still fetched through
        # explicit ContextRequests, so this cannot inflate the prompt or
        # bypass the normal resolver/permission path.
        if self.evidence_retriever is not None and (hypotheses or action):
            try:
                anchors = dict(self.session.repo_map.get("investigation_anchors") or {})
                anchors["hypotheses"] = self.session.hypotheses
                anchors["next_actions"] = [action.get("kind")] if action else []
                anchors["focus_goal"] = self.session.focus_chain[-1] if self.session.focus_chain else ""
                anchors["recent_observations"] = self.session.parts[-8:]
                refreshed = self.retrieve_evidence(anchors, limit=12)
                existing = self.session.repo_map.get("retrieval_candidates") or []
                merged: Dict[str, Dict[str, Any]] = {}
                for item in list(existing) + list(refreshed):
                    if not isinstance(item, dict) or not item.get("file"):
                        continue
                    key = "%s:%s:%s" % (item.get("file"), item.get("line_start", 0), item.get("symbol", ""))
                    prior = merged.get(key)
                    if prior is None or float(item.get("score") or 0) > float(prior.get("score") or 0):
                        merged[key] = dict(item)
                ordered = sorted(merged.values(), key=lambda item: (-float(item.get("score") or 0), str(item.get("file") or "")))[:24]
                self.session.repo_map["retrieval_candidates"] = ordered
            except Exception:
                pass
        if resolved:
            successes = sum(1 for item in resolved if isinstance(item, dict) and item.get("success"))
            total = sum(1 for item in resolved if isinstance(item, dict))
            if total:
                prior = float(self.session.evidence_confidence.get("repository") or 0.0)
                current = float(successes) / float(total)
                self.session.evidence_confidence["repository"] = round(prior * 0.6 + current * 0.4, 4)
            for item in resolved:
                if not isinstance(item, dict):
                    continue
                request = item.get("request") if isinstance(item.get("request"), dict) else {}
                source_id = self.evidence_graph.add_node(
                    "observation", {"request": request, "success": bool(item.get("success"))},
                    round=round_index,
                )
                for hypothesis in self.session.hypotheses:
                    target_id = self.evidence_graph.add_node("hypothesis", hypothesis)
                    self.evidence_graph.add_edge(
                        source_id, "supports" if item.get("success") else "informs", target_id,
                        round=round_index,
                    )
            self.session.evidence_graph = self.evidence_graph.to_dict()
            if self.investigation_controller is not None:
                try:
                    planned = list(getattr(self.investigation_controller, "state", None).actions or [])
                    for item in resolved:
                        request = item.get("request") if isinstance(item, dict) else {}
                        request_kind = {
                            "function": "locate", "callers": "find_callers",
                            "references": "find_references", "field": "inspect_field",
                            "grep": "inspect_tests", "read_file": "read_candidate",
                        }.get(request.get("type"), request.get("type"))
                        key = {"kind": request_kind, "target": request.get("symbol") or request.get("file")}
                        self.investigation_controller.record_result(
                            key, success=bool(item.get("success")), round_index=round_index
                        )
                    anchors = dict(self.session.repo_map.get("investigation_anchors") or {})
                    anchors["hypotheses"] = self.session.hypotheses
                    candidates = self.session.repo_map.get("retrieval_candidates") or []
                    next_actions = self.investigation_controller.plan(anchors, candidates, round_index=round_index)
                    self.session.repo_map["investigation_plan"] = [item.to_dict() for item in next_actions]
                except Exception:
                    # Planning is advisory and must never break evidence resolution.
                    pass
        self.session.evidence_graph = self.evidence_graph.to_dict()

    def ingest_observation(self, observation: Dict[str, Any], *, round_index: int = 0) -> None:
        """Feed executable/runtime feedback into open hypotheses without treating it as proof."""
        if not isinstance(observation, dict):
            return
        self.session.parts.extend(p.to_dict() for p in parts_from_evidence([{**observation, "round": round_index}]))
        self.session.parts = self.session.parts[-256:]
        if self.observation_store is not None and hasattr(self.observation_store, "add"):
            try:
                from services.observations import Observation
                self.observation_store.add(Observation(
                    kind=str(observation.get("kind") or "runtime_event"),
                    source=str(observation.get("source") or "context_engine"),
                    status=str(observation.get("status") or "unknown"),
                    summary=str(observation.get("summary") or observation.get("error") or ""),
                    round=int(round_index), details=dict(observation), actionable=True,
                ))
            except Exception:
                pass
        status = str(observation.get("status") or "").lower()
        failure_class = str(observation.get("failure_class") or "").lower()
        contradiction_classes = {"compile_error", "test_failure", "reproduce_failure"}
        evidence = {"round": int(round_index), "source": observation.get("source", "runtime"),
                    "summary": str(observation.get("summary") or observation.get("error") or "")[:1000]}
        observation_id = self.evidence_graph.add_node("observation", evidence)
        for hypothesis in self.session.hypotheses:
            if not isinstance(hypothesis, dict) or hypothesis.get("status") not in {"open", "testing"}:
                continue
            if status == "passed":
                hypothesis.setdefault("supporting_evidence", []).append(evidence)
                relation = "supports"
            elif status in {"failed", "timeout", "rejected"} and (not failure_class or failure_class in contradiction_classes):
                hypothesis.setdefault("contradicting_evidence", []).append(evidence)
                relation = "contradicts"
            else:
                relation = "informs"
            hypothesis_id = self.evidence_graph.add_node("hypothesis", hypothesis)
            self.evidence_graph.add_edge(observation_id, relation, hypothesis_id, round=round_index)
            hypothesis["supporting_evidence"] = list(hypothesis.get("supporting_evidence") or [])[-20:]
            hypothesis["contradicting_evidence"] = list(hypothesis.get("contradicting_evidence") or [])[-20:]
        self.session.evidence_graph = self.evidence_graph.to_dict()

    def resolve_requests(self, requests: Sequence[Dict[str, Any]], *, round_index: int) -> List[Dict[str, Any]]:
        resolved: List[Dict[str, Any]] = []
        for request in list(requests)[: self.config.max_requests]:
            prior = self.ledger.get(request)
            if prior is not None:
                item = {
                    "request": dict(request),
                    "success": False,
                    "skipped": True,
                    "skip_reason": "duplicate_request",
                    "prior_status": prior.get("status"),
                    "lookup_exhausted": prior.get("status") != "success",
                    "error": f"duplicate context request; prior status={prior.get('status')}",
                }
                self.ledger.record_duplicate(request, round_index)
            else:
                item = self.resolvers.resolve(request)
                self.ledger.record(request, item, round_index)
            if self.trace is not None:
                self.trace.emit("context.observation", kind="observation", name=str(request.get("type") or "context"),
                                status="success" if item.get("success") else "failed", round=round_index,
                                output_hash=str(item.get("output_hash") or ""))
            resolved.append(item)
            if item.get("success"):
                self.session.parts.extend(p.to_dict() for p in parts_from_evidence([{**item, "round": round_index}]))
        self.session.parts = self.session.parts[-256:]
        self.session.request_ledger = self.ledger.entries()
        self.session.stats["resolved_requests"] += len(resolved)
        return resolved

    def record_invalid_requests(
        self,
        invalid_requests: Sequence[Dict[str, Any]],
        *,
        round_index: int,
    ) -> None:
        for index, invalid in enumerate(invalid_requests):
            value = dict(invalid) if isinstance(invalid, dict) else {"value": invalid}
            raw_request = value.get("request") if isinstance(value.get("request"), dict) else value
            request = dict(raw_request)
            request.setdefault("type", str(request.get("type") or "invalid"))
            if not request.get("symbol") and not request.get("file"):
                request["symbol"] = f"invalid_request_{round_index}_{index}"
            error = str(value.get("error") or value.get("reason") or "invalid context request")
            self.ledger.record_rejected(request, round_index, error)
        self.session.request_ledger = self.ledger.entries()

    @staticmethod
    def all_requests_blocked(resolved: Sequence[Dict[str, Any]]) -> bool:
        return all_context_requests_blocked(list(resolved))

    @staticmethod
    def build_pre_round_add_res(
        *,
        source_round: int,
        target_round: int,
        resolved_context: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return build_pre_round_add_res(
            source_round=source_round,
            target_round=target_round,
            resolved_context=list(resolved_context),
        )

    def evidence_delta(self, resolved: Sequence[Dict[str, Any]], *, round_index: int) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for resolution in resolved:
            content = str(self.format_resolution(resolution) or "").strip()
            if not content:
                continue
            items.append({
                "kind": "source_code" if resolution.get("success") else "context_result",
                "content": content,
                "source": "context_loop",
                "file": resolution.get("file"),
                "round": int(round_index),
            })
        ledger = self.ledger.markdown()
        if ledger:
            items.append({
                "kind": "request_ledger",
                "content": ledger,
                "source": "context_engine",
                "round": int(round_index),
            })
        if self.observation_store is not None and hasattr(self.observation_store, "markdown"):
            observation_text = self.observation_store.markdown(since_round=max(0, int(round_index) - 1))
            if observation_text:
                items.append({
                    "kind": "runtime_observation",
                    "content": observation_text,
                    "source": "observation_store",
                    "round": int(round_index),
                })
        return items

    @staticmethod
    def _compress_stable_context(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        marker = "\n...[stable context compressed by budget]...\n"
        if limit <= len(marker):
            return text[: max(0, limit)]
        available = max(0, limit - len(marker))
        head = int(available * 0.7)
        return text[:head] + marker + text[-(available - head):]

    def build_prompt(
        self,
        stable_context: str,
        *,
        evidence_delta: Sequence[Dict[str, Any]] = (),
        is_final_round: bool = False,
        early_final_reason: Optional[str] = None,
    ) -> str:
        def _bound_items(
            items: Sequence[Dict[str, Any]],
            limit: int,
            truncated_label: str,
        ) -> tuple[List[Dict[str, Any]], int]:
            selected: List[Dict[str, Any]] = []
            used = 0
            suffix = f"\n...[{truncated_label} truncated]"
            for item in items:
                content = str(item.get("content") or "")
                remaining = limit - used
                if remaining <= 0:
                    break
                if len(content) <= remaining:
                    bounded = content
                elif remaining > len(suffix):
                    bounded = content[: remaining - len(suffix)] + suffix
                else:
                    bounded = content[:remaining]
                selected.append({**item, "content": bounded})
                used += len(bounded)
            return selected, used

        max_chars = self.config.effective_max_chars
        evidence_delta = list(evidence_delta)
        if not evidence_delta and self.observation_store is not None and hasattr(self.observation_store, "markdown"):
            observation_text = self.observation_store.markdown(max_chars=4000)
            if observation_text:
                evidence_delta.append({
                    "kind": "runtime_observation",
                    "content": observation_text,
                    "source": "observation_store",
                    "round": 0,
                })
        delta_items = [
            item for item in evidence_delta
            if str(item.get("kind") or "") != "request_ledger"
        ]
        ledger_items = [
            item for item in evidence_delta
            if str(item.get("kind") or "") == "request_ledger"
        ]
        compacted = self.compactor.compact(
            [
                {"priority": "recent_observation", **item} for item in delta_items
            ] + [
                {"priority": "ledger", **item} for item in ledger_items
            ],
            max_chars=max(0, int(max_chars * 0.45)),
            round_index=len(self.session.turns),
        )
        self.session.compaction = dict(compacted.metadata)
        control_only = str(assemble_loop_prompt(
            "",
            evidence_package={"items": []},
            is_final_round=is_final_round,
            early_final_reason=early_final_reason,
            include_json_reminder=True,
            investigation_state={
                "hypotheses": self.session.hypotheses,
                "next_action": self.session.investigation_actions[-1] if self.session.investigation_actions else {},
                "focus_chain": self.session.focus_chain[-4:],
                "verification_claim": self.session.verification_claim,
                "verification_capabilities": self.session.verification_capabilities,
                "reproduction_plan": self.session.reproduction_plan,
                "investigation_plan": self.session.repo_map.get("investigation_plan") or [],
            },
        )["content"])
        content_budget = max(0, max_chars - len(control_only) - 256)
        delta_limit = int(content_budget * 0.30)
        ledger_limit = int(content_budget * 0.15)
        bounded_delta, used_delta = _bound_items(delta_items, delta_limit, "delta")
        bounded_ledger, used_ledger = _bound_items(ledger_items, ledger_limit, "ledger")
        stable_limit = max(0, content_budget - used_delta - used_ledger)
        stable_text = str(stable_context or "")
        bounded_stable = self._compress_stable_context(stable_text, stable_limit)

        def _assemble() -> str:
            return str(assemble_loop_prompt(
                bounded_stable,
                evidence_package={"items": [*bounded_delta, *bounded_ledger]},
                is_final_round=is_final_round,
                early_final_reason=early_final_reason,
                include_json_reminder=True,
                investigation_state={
                    "hypotheses": self.session.hypotheses,
                    "next_action": self.session.investigation_actions[-1] if self.session.investigation_actions else {},
                    "focus_chain": self.session.focus_chain[-4:],
                    "verification_claim": self.session.verification_claim,
                    "verification_capabilities": self.session.verification_capabilities,
                    "reproduction_plan": self.session.reproduction_plan,
                    "investigation_plan": self.session.repo_map.get("investigation_plan") or [],
                },
            )["content"])

        assembled = _assemble()
        if len(assembled) > max_chars and bounded_stable:
            overflow = len(assembled) - max_chars
            stable_limit = max(0, len(bounded_stable) - overflow - 64)
            bounded_stable = self._compress_stable_context(stable_text, stable_limit)
            assembled = _assemble()
        if len(assembled) > max_chars and used_delta:
            overflow = len(assembled) - max_chars
            bounded_delta, used_delta = _bound_items(
                delta_items,
                max(0, used_delta - overflow - 64),
                "delta",
            )
            assembled = _assemble()
        if len(assembled) > max_chars and used_ledger:
            overflow = len(assembled) - max_chars
            bounded_ledger, used_ledger = _bound_items(
                ledger_items,
                max(0, used_ledger - overflow - 64),
                "ledger",
            )
            assembled = _assemble()
        self.session.budget["last_prompt_chars"] = len(assembled)
        self.session.budget["last_prompt_overflow_chars"] = max(0, len(assembled) - max_chars)
        return str(assembled)

    def add_turn(self, turn: ContextTurn) -> None:
        self.session.turns.append(turn)
        self.session.stats["model_turns"] = len(self.session.turns)
        self.session.budget["prompt_chars"] = int(self.session.budget.get("prompt_chars") or 0) + len(turn.prompt)

    def finish(self, reason: str, *, degraded: bool = False) -> None:
        if reason not in TERMINATION_REASONS:
            raise ValueError(f"unsupported context-loop termination reason: {reason}")
        self.session.status = "degraded" if degraded else "completed"
        self.session.termination_reason = reason
        if self.session.turns:
            self.session.turns[-1].termination_reason = reason
        if self.trace is not None:
            self.trace.emit("agent.termination", kind="agent", name="context_engine",
                            status="degraded" if degraded else "success", termination_reason=reason)
