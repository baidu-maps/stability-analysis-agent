"""Re-run deterministic diagnosis after a verified fix sync."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def run_post_fix_diagnosis(
    *,
    crash_log: Optional[str] = None,
    crash_log_content: Optional[str] = None,
    library_dir: Optional[str] = None,
    code_roots: Optional[Sequence[str]] = None,
    project_root: Optional[Path] = None,
    cli_main: Optional[Path] = None,
    timeout_sec: float = 300,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run parse_stack_only after verification to ensure diagnosis still holds."""
    root = project_root or Path(__file__).resolve().parents[1]
    main_py = cli_main or (root / "cli" / "main.py")
    if not main_py.is_file():
        return {"status": "skipped", "reason": "cli entry not found"}

    cmd: List[str] = [sys.executable, str(main_py)]
    stdin_text: Optional[str] = None
    if crash_log_content:
        cmd += ["--crash-log-file", "-"]
        stdin_text = crash_log_content
    elif crash_log and str(crash_log).strip() and str(crash_log).strip() != "-":
        cmd += ["--crash-log-file", str(crash_log)]
    else:
        return {"status": "skipped", "reason": "crash log unavailable"}

    if library_dir:
        cmd += ["--library-dir", str(library_dir)]
    roots = [str(x) for x in (code_roots or []) if str(x).strip()]
    for root in roots:
        cmd += ["--code-roots", root]
    cmd += ["--scope", "parse_stack_only", "--no-apply-ai-fixes", "--output-format", "json"]

    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=max(30, float(timeout_sec)),
            check=False,
            env=run_env,
        )
        output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        return {
            "status": "passed" if proc.returncode == 0 else "failed",
            "exit_code": proc.returncode,
            "output": output[-12000:],
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": "post-fix diagnosis timed out"}
    except Exception as exc:
        return {"status": "failed", "error": str(exc)}


def run_post_fix_diagnosis_from_request(request: Any, *, project_root: Optional[Path] = None,
                                        env: Optional[Dict[str, str]] = None,
                                        post_fix_enabled: bool = True) -> Dict[str, Any]:
    """Adapter for daemon RunRequest objects."""
    if not post_fix_enabled:
        return {"status": "skipped", "reason": "post_fix_diagnosis disabled"}
    if request is None:
        return {"status": "skipped", "reason": "original request is unavailable"}
    from protocol.models import normalize_run_code_roots

    roots = normalize_run_code_roots(request)
    return run_post_fix_diagnosis(
        crash_log=getattr(request, "crash_log", None),
        crash_log_content=getattr(request, "crash_log_content", None),
        library_dir=getattr(request, "library_dir", None),
        code_roots=roots,
        project_root=project_root,
        env=env,
    )
