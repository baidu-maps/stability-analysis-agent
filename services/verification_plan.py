"""Capability-selected, reproducible verification plan contracts."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
import hashlib, json
from typing import Any, Dict, List, Mapping, Optional
from services.verification_profile import VerificationCheck, VerificationProfile

PURPOSES = frozenset({"pre_fix_reproduce", "post_fix_verify", "compile", "static_check", "test", "reproduce"})

@dataclass(frozen=True)
class VerificationClaim:
    statement: str
    required_evidence: List[str] = field(default_factory=list)
    minimum_level: str = "L1"
    def to_dict(self): return asdict(self)

@dataclass(frozen=True)
class VerificationCapability:
    check_id: str
    kind: str
    provider: str
    description: str = ""
    evidence_types: List[str] = field(default_factory=list)
    verification_level: str = "L1"
    fixture: Optional[str] = None
    iterations: int = 1
    expected_signature: Optional[str] = None
    requires_approval: bool = True
    @classmethod
    def from_check(cls, check):
        return cls(check.id, check.kind, check.provider, check.description, list(check.evidence_types), check.verification_level, check.fixture, check.iterations, check.expected_signature, check.requires_approval)
    def to_dict(self): return asdict(self)

@dataclass(frozen=True)
class ReproductionPlan:
    check_id: str
    purpose: str
    claim: VerificationClaim
    plan_fingerprint: str
    def to_dict(self): return {"check_id": self.check_id, "purpose": self.purpose, "claim": self.claim.to_dict(), "plan_fingerprint": self.plan_fingerprint}

@dataclass(frozen=True)
class VerificationPlan:
    claim: VerificationClaim
    profile_id: Optional[str] = None
    check_ids: List[str] = field(default_factory=list)
    frontend_available: bool = False
    runtime_available: bool = False
    selected_check: Optional[Dict[str, Any]] = None
    purpose: Optional[str] = None
    @property
    def fingerprint(self):
        raw = json.dumps({"claim": self.claim.to_dict(), "profile_id": self.profile_id, "check_ids": self.check_ids, "selected_check": self.selected_check, "purpose": self.purpose, "frontend_available": self.frontend_available, "runtime_available": self.runtime_available}, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()[:20]
    def to_dict(self): return {"claim": self.claim.to_dict(), "profile_id": self.profile_id, "check_ids": list(self.check_ids), "frontend_available": self.frontend_available, "runtime_available": self.runtime_available, "selected_check": self.selected_check, "purpose": self.purpose, "plan_fingerprint": self.fingerprint}

def capabilities_from_profile(profile):
    return [VerificationCapability.from_check(item) for item in (profile.checks if profile else [])]

def _claim(value):
    raw = value if isinstance(value, Mapping) else {}
    statement = str(raw.get("statement") or raw.get("claim") or "").strip()
    if not statement: raise ValueError("verification claim statement is required")
    minimum = str(raw.get("minimum_level") or "L1").upper()
    if minimum not in {"L0", "L1", "L2", "L3", "L4"}: raise ValueError("invalid verification minimum level")
    evidence = raw.get("required_evidence") or []
    if not isinstance(evidence, list) or not all(isinstance(item, str) and item for item in evidence): raise ValueError("required_evidence must be a string list")
    return VerificationClaim(statement, list(evidence), minimum)

def build_verification_plan(claim, profile=None, reproduction_plan=None, overrides=None):
    parsed_claim = _claim(claim)
    if profile is not None and not isinstance(profile, VerificationProfile): profile = VerificationProfile.from_mapping(profile)
    selection = reproduction_plan if isinstance(reproduction_plan, Mapping) else {}
    if any(key in selection for key in ("command", "shell", "argv")): raise ValueError("reproduction plan cannot provide a command")
    check_id, purpose = str(selection.get("check_id") or "").strip(), str(selection.get("purpose") or "").strip()
    if check_id and profile is None: raise ValueError("verification check is not configured")
    if check_id and purpose not in PURPOSES: raise ValueError("invalid reproduction plan purpose")
    selected = None
    if check_id:
        try: check = profile.check(check_id)
        except KeyError as exc: raise ValueError(str(exc))
        selected = asdict(check)
        override_values = overrides if isinstance(overrides, Mapping) else {}
        forbidden = set(override_values) - set(check.allowed_override_fields)
        if forbidden: raise ValueError("verification override is not allowed: %s" % ", ".join(sorted(forbidden)))
        for key, value in override_values.items():
            if key == "iterations" and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000000): raise ValueError("verification iterations override is invalid")
            if key == "timeout_sec" and (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 86400): raise ValueError("verification timeout override is invalid")
            selected[key] = value
    return VerificationPlan(parsed_claim, profile.profile_id if profile else None, [x.id for x in profile.checks] if profile else [], bool(profile.frontend_available) if profile else False, bool(profile.runtime_available) if profile else False, selected, purpose or None)

def build_reproduction_plan(claim, profile, selection, overrides=None):
    plan = build_verification_plan(claim, profile, selection, overrides)
    if not plan.selected_check or not plan.purpose: raise ValueError("reproduction plan requires a declared check_id and purpose")
    return ReproductionPlan(str(plan.selected_check["id"]), plan.purpose, plan.claim, plan.fingerprint)
