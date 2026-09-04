"""Crash-agent specific action security checks layered before PolicyEngine."""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional


@dataclass(frozen=True)
class SecurityDecision:
    allowed: bool
    reason: str
    risks: List[str] = field(default_factory=list)

    def to_dict(self):
        return {"allowed": self.allowed, "reason": self.reason, "risks": list(self.risks)}


class ActionSecurityAnalyzer:
    DANGEROUS = {"rm", "rmdir", "sudo", "chmod", "chown", "curl", "wget", "pip", "npm", "brew"}

    def analyze_action(self, action: Any, workspace: str, hypotheses: Optional[list] = None,
                       authorization: Any = None) -> SecurityDecision:
        name = str(getattr(action, "name", "") or (action or {}).get("name", ""))
        payload = action if isinstance(action, dict) else getattr(action, "payload", {})
        payload = payload if isinstance(payload, dict) else {}
        risks: List[str] = []
        roots = [Path(workspace).expanduser().resolve()] if workspace else []
        for raw_root in payload.get("code_roots", []) + payload.get("allowed_roots", []):
            try:
                roots.append(Path(str(raw_root)).expanduser().resolve())
            except (OSError, ValueError):
                continue
        for raw in payload.get("changed_files", []) + payload.get("workspace_paths", []):
            path = Path(str(raw)).expanduser()
            if not path.is_absolute() and workspace:
                path = Path(workspace) / path
            try:
                resolved = path.resolve()
                if roots and not any(resolved == root or root in resolved.parents for root in roots):
                    risks.append("path_outside_workspace")
            except Exception:
                risks.append("invalid_path")
        command = payload.get("command") or []
        if isinstance(command, str):
            command = shlex.split(command)
        if any(Path(str(token)).name.lower() in self.DANGEROUS for token in command):
            risks.append("dangerous_command")
        if name in {"apply_patch", "repair", "fix_code_applier"} and not authorization and not payload.get("isolated_worktree"):
            risks.append("repair_requires_authorization")
        if risks:
            return SecurityDecision(False, "action blocked by Crash Agent security analyzer", sorted(set(risks)))
        return SecurityDecision(True, "action accepted by Crash Agent security analyzer")
