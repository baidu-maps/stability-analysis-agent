"""Small, explicit policy gate for tools and local verification commands."""
from __future__ import annotations

import os
import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

from services.policy_paths import extract_workspace_paths, paths_within_allowed_roots


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    decision: str
    reason: str
    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "decision": self.decision, "reason": self.reason}


class PolicyEngine:
    """Default-deny boundary for execution, network and destructive actions."""
    def __init__(self, *, allowed_commands: Optional[Iterable[str]] = None,
                 allowed_roots: Optional[Iterable[str]] = None,
                 allow_network: bool = False, allow_destructive: bool = False,
                 permission_rules: Optional[Iterable[Dict[str, Any]]] = None):
        self.allowed_commands = {str(x) for x in (allowed_commands or []) if str(x)}
        self.allowed_roots = [Path(x).expanduser().resolve() for x in (allowed_roots or []) if str(x)]
        self.allow_network = bool(allow_network)
        self.allow_destructive = bool(allow_destructive)
        self.permission_rules = [dict(x) for x in (permission_rules or []) if isinstance(x, dict)]

    def check_permission(self, permission: str, path: Optional[str] = None, *, scope: str = "task") -> PolicyDecision:
        """Evaluate optional OpenCode-style pattern rules before action execution."""
        permission = str(permission or "read").strip().lower()
        candidate = str(path or "")
        for rule in self.permission_rules:
            if str(rule.get("permission") or "").lower() != permission:
                continue
            patterns = rule.get("patterns") or ["*"]
            if candidate and not any(fnmatch.fnmatch(candidate, str(pattern)) for pattern in patterns):
                continue
            decision = str(rule.get("decision") or "ask").lower()
            if decision == "allow":
                return PolicyDecision(True, "allowed", "permission rule allows this action")
            if decision == "deny":
                return PolicyDecision(False, "denied", "permission rule denies this action")
            return PolicyDecision(False, "approval_required", "permission rule requires approval")
        return PolicyDecision(True, "allowed", "no restrictive permission rule")

    def check_tool(self, *, risk: str = "read_only", side_effect: bool = False,
                   approved: bool = False, requires_approval: bool = False,
                   workspace_paths: Optional[Sequence[str]] = None,
                   isolated: bool = False) -> PolicyDecision:
        risk = str(risk or "read_only")
        if risk == "network" and not self.allow_network:
            return PolicyDecision(False, "denied", "network access is disabled")
        if risk == "destructive" and not self.allow_destructive:
            return PolicyDecision(False, "denied", "destructive actions are disabled")
        if self.allowed_roots and workspace_paths:
            if not paths_within_allowed_roots(workspace_paths, self.allowed_roots):
                return PolicyDecision(False, "denied", "tool paths are outside allowed roots")
        isolated_write = bool(isolated and risk == "workspace_write")
        if (risk in {"execute", "workspace_write"} or side_effect or requires_approval) and not approved and not isolated_write:
            return PolicyDecision(False, "approval_required", "explicit approval is required")
        return PolicyDecision(True, "allowed", "policy allows this action")

    def check_command(self, argv: Sequence[str], *, workspace: Optional[str] = None,
                      approved: bool = False) -> PolicyDecision:
        if not argv:
            return PolicyDecision(False, "denied", "empty command")
        executable = os.path.basename(str(argv[0]))
        if self.allowed_commands and executable not in self.allowed_commands and str(argv[0]) not in self.allowed_commands:
            return PolicyDecision(False, "denied", f"command is not allowlisted: {executable}")
        if self.allowed_roots and workspace:
            root = Path(workspace).expanduser().resolve()
            if not any(root == allowed or allowed in root.parents for allowed in self.allowed_roots):
                return PolicyDecision(False, "denied", "workspace is outside allowed roots")
        if not approved:
            return PolicyDecision(False, "approval_required", "verification command requires explicit approval")
        return PolicyDecision(True, "allowed", "policy allows this command")
