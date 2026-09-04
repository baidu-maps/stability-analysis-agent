"""Low-cost, read-only repository history and test evidence for investigations."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


class RepositoryEvidenceService:
    """Collect candidate evidence; callers must corroborate it with source/crash data."""

    def __init__(self, code_roots: Sequence[str], *, timeout_sec: float = 8.0):
        self.code_roots = [str(Path(root).expanduser().resolve()) for root in code_roots or []]
        self.timeout_sec = max(1.0, float(timeout_sec))

    def _root_for(self, file_path: str) -> Optional[str]:
        try:
            target = Path(file_path).expanduser().resolve()
            for root in self.code_roots:
                if root == str(target) or str(target).startswith(root + "/"):
                    return root
        except Exception:
            return None
        return self.code_roots[0] if self.code_roots else None

    def _git(self, root: str, args: List[str]) -> Dict[str, Any]:
        try:
            proc = subprocess.run(["git", "-C", root, *args], capture_output=True,
                                  text=True, timeout=self.timeout_sec, check=False)
            return {"success": proc.returncode == 0, "exit_code": proc.returncode,
                    "stdout": proc.stdout[-12000:], "stderr": proc.stderr[-4000:]}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def find_history(self, file_path: str, *, line: int = 0, symbol: str = "") -> Dict[str, Any]:
        root = self._root_for(file_path)
        if not root:
            return {"success": False, "error": "file is outside configured code roots"}
        target = str(Path(file_path).expanduser().resolve())
        rel = str(Path(target).relative_to(root))
        blame_args = ["blame", "-L", f"{max(1, int(line))},{max(1, int(line))}", "--", rel] if line else ["log", "-n", "8", "--format=%H%x09%ad%x09%s", "--date=short", "--", rel]
        history = self._git(root, blame_args)
        log = self._git(root, ["log", "-n", "8", "--format=%H%x09%ad%x09%s", "--date=short", "-S", str(symbol), "--", rel]) if symbol else None
        return {"success": bool(history.get("success")), "file": target, "line": int(line or 0),
                "symbol": str(symbol or ""), "blame_or_log": history.get("stdout", ""),
                "symbol_history": (log or {}).get("stdout", ""), "error": history.get("error")}

    def find_tests(self, symbol: str, *, max_results: int = 20) -> Dict[str, Any]:
        matches: List[Dict[str, Any]] = []
        needle = str(symbol or "").strip()
        if not needle:
            return {"success": False, "error": "symbol is required"}
        for root in self.code_roots:
            result = self._git(root, ["grep", "-n", "-I", "-E", needle])
            if not result.get("success") and not result.get("stdout"):
                continue
            for row in str(result.get("stdout") or "").splitlines():
                parts = row.split(":", 2)
                if len(parts) == 3 and ("test" in parts[0].lower() or "spec" in parts[0].lower()):
                    matches.append({"file": str(Path(root, parts[0]).resolve()), "line": parts[1], "text": parts[2]})
                    if len(matches) >= max_results:
                        break
        return {"success": True, "symbol": needle, "matches": matches, "candidate_only": True}
