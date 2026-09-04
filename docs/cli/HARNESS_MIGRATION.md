# Harness Migration Guide

## Removed entry points

- `agent/ai_stability_agent.py` — use `tool_system.agent_runtime.AgentRuntime` + CLI/daemon `/runs`.

## New report artifacts

| File | Purpose |
|------|---------|
| `09_evidence.json` | EvidenceStore snapshot |
| `10_decide.json` | Unified decide scorer output |
| `11_judge.json` | Deterministic harness judge output |
| `09_vector_db_commit.json` | Crash Engineering Memory commit audit |
| `artifacts/analyze_round_N.json` | Context loop round checkpoints |
| `artifacts/feedback_analyze_0.json` | Post-verify/judge feedback analyze snapshot |
| `00_evaluation.json` | Offline evaluation matrix sidecar (AI regression) |

## Runtime metadata

- `metadata.runtime_trace` / `00_runtime_trace.json` — harness events + budget
- `metadata.decide` — same schema as `10_decide.json`
- Run status `verification_pending` / `approval_required` — pauses for user action

## Replay

```bash
python3 cli/main.py replay <report_dir> --from-stage analyze
python3 cli/main.py replay <report_dir> --checkpoint-id analyze:round:1
python3 cli/main.py replay <report_dir> --checkpoint-id analyze:feedback:0
```

`act` replay is forbidden; create a new repair task after failed runs.

## Defaults (phase 2)

- `--scope full` defaults to `--agent-loop context_loop` with up to **3** analyze rounds unless `--agent-loop single` is set.
- Verification/judge failure may trigger one bounded **feedback analyze** round (`analyze:feedback:0`) that revises analysis only (no automatic re-apply).

## Context request types

See [CONTEXT_LOOP_CONTRACT.md](../architecture/CONTEXT_LOOP_CONTRACT.md) for `function` / `field` / `references` / `callers` plus `memory_pattern`, `verification_log`, and `trace_snippet`.

## Verification

Configure explicit commands or pick from `discovered_candidates` in daemon/Web UI. See [VERIFICATION_PROVIDERS.md](../tools/VERIFICATION_PROVIDERS.md) and `configs/verification_presets.example.json`.
