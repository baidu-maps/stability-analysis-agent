"""Evidence and source-identity contracts for safe automated repair."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Union


UNSAFE_IDENTITY_CONFIDENCE = frozenset(
    {"blocked", "low", "mismatch", "unsafe", "untrusted"}
)
STRICT_COMMIT_POLICIES = frozenset(
    {"exact", "locked", "match", "require_match", "strict"}
)
UNPINNED_COMMIT_POLICIES = frozenset(
    {"branch_latest", "latest", "mixed", "unpinned"}
)


def source_identity_from_mapping(value: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Normalize additive flat or nested source-identity protocol fields."""
    root = value if isinstance(value, Mapping) else {}
    nested = root.get("source_identity")
    identity = dict(nested) if isinstance(nested, Mapping) else {}
    safety = root.get("repair_safety")
    if not isinstance(safety, Mapping):
        safety = identity.get("repair_safety") if isinstance(identity.get("repair_safety"), Mapping) else {}
    aliases = {
        "source_origin": ("source_origin", "origin"),
        "commit_policy": ("commit_policy",),
        "base_commit": ("base_commit",),
        "identity_confidence": ("identity_confidence", "confidence"),
        "block_reason": ("block_reason", "signal"),
        "exactly_pinned": ("exactly_pinned",),
        "source_may_include_fix": ("source_may_include_fix",),
        "hard_block_auto_repair": ("hard_block_auto_repair",),
    }
    for target, keys in aliases.items():
        if identity.get(target) not in (None, ""):
            continue
        for key in keys:
            if root.get(key) not in (None, ""):
                identity[target] = root.get(key)
                break
            if isinstance(safety, Mapping) and safety.get(key) not in (None, ""):
                identity[target] = safety.get(key)
                break
    if identity.get("hard_block_auto_repair") in (None, "") and isinstance(safety, Mapping):
        identity["hard_block_auto_repair"] = safety.get("hard_block_auto_repair")
    base_commit = identity.get("base_commit")
    if isinstance(base_commit, (list, tuple)):
        base_commit = str(base_commit[0] or "").strip() if base_commit else ""
    else:
        base_commit = str(base_commit or "").strip()
    return {
        "source_origin": str(identity.get("source_origin") or "").strip(),
        "commit_policy": str(identity.get("commit_policy") or "").strip().lower(),
        "base_commit": base_commit,
        "identity_confidence": str(identity.get("identity_confidence") or "").strip().lower(),
        "block_reason": str(identity.get("block_reason") or "").strip(),
        "exactly_pinned": identity.get("exactly_pinned"),
        "source_may_include_fix": bool(identity.get("source_may_include_fix")),
        "hard_block_auto_repair": bool(identity.get("hard_block_auto_repair")),
    }


def evaluate_source_identity(
    value: Optional[Mapping[str, Any]],
    *,
    observed_commit: Optional[Union[str, Sequence[str]]] = None,
) -> Dict[str, Any]:
    """Return source-identity risk for reviewers.

    ``hard_block`` means the checkout is not a pinned match. Callers that
    already have a concrete patch should still apply it and keep this payload
    on the report.
    """
    identity = source_identity_from_mapping(value)
    if isinstance(observed_commit, str):
        observed = [observed_commit.strip()] if observed_commit.strip() else []
    else:
        observed = [
            str(item).strip()
            for item in (observed_commit or [])
            if str(item).strip()
        ]
    reasons = []
    if identity["hard_block_auto_repair"]:
        reasons.append(identity["block_reason"] or "source identity forbids automatic repair")
    elif identity["block_reason"] in {"forbid_auto_code_write"}:
        reasons.append(identity["block_reason"])
    elif identity["block_reason"]:
        reasons.append(identity["block_reason"])
    if identity["identity_confidence"] in UNSAFE_IDENTITY_CONFIDENCE:
        reasons.append(
            "source identity confidence is {}".format(identity["identity_confidence"])
        )
    if identity["commit_policy"] in UNPINNED_COMMIT_POLICIES:
        reasons.append("source commit is not pinned ({})".format(identity["commit_policy"]))
    if identity["source_may_include_fix"]:
        reasons.append("checked out source may already contain the card fix")
    if identity.get("exactly_pinned") is False and identity["commit_policy"]:
        reasons.append("source commit is not exactly pinned")
    if identity["commit_policy"] in STRICT_COMMIT_POLICIES:
        expected = identity["base_commit"]
        if not expected:
            reasons.append("strict commit policy requires base_commit")
        elif not observed:
            reasons.append("strict commit policy could not resolve source commit")
        elif not any(
            expected == actual
            or actual.startswith(expected)
            or expected.startswith(actual)
            for actual in observed
        ):
            reasons.append(
                "base commit mismatch: expected {}, observed {}".format(
                    expected,
                    ", ".join(observed),
                )
            )
    unique_reasons = list(dict.fromkeys(str(item) for item in reasons if str(item).strip()))
    return {
        "schema_version": 1,
        **identity,
        "observed_commits": observed,
        "safe": not unique_reasons,
        "hard_block": bool(unique_reasons),
        "reasons": unique_reasons,
    }


def build_evidence_contract(
    value: Optional[Mapping[str, Any]],
    *,
    observed_commit: Optional[Union[str, Sequence[str]]] = None,
) -> Dict[str, Any]:
    """Build the structured contract persisted by analysis and repair stages."""
    return {
        "schema_version": 1,
        "source_identity": evaluate_source_identity(
            value,
            observed_commit=observed_commit,
        ),
        "fact_kinds": ["log", "symbol", "source", "inference"],
    }
