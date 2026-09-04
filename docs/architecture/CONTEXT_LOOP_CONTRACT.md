# Context Loop Contract

`services/context_engine.py` owns the per-run context session. It parses model
decisions, maintains the request ledger, invokes registered context resolvers,
and assembles a bounded follow-up prompt. It never calls the LLM; turn control
remains in `services/agent_context_loop.py` and lifecycle/budget control remains
in `AgentRuntime` / `WorkflowContext`. Analyze-stage LLM transport and retry
policy live in `services/analyze_llm.py`.

The built-in source resolvers live in `services/context_source_resolver.py`;
return-form normalization and rendering live in
`services/context_request_contract.py`. Crash, ANR, and native-leak workflows
therefore share the same resolver registry without inheriting crash-workflow
context-loop methods.

Context is split into stable task context, the current round's evidence delta,
the compact request ledger, and mandatory control sections. Follow-up turns do
not re-inject the complete round-0 `EvidenceStore` package.

`services/context_loop_contract.py` is the single source for analyze-stage multi-round control:

- **Schema**: `ContextRequest`, `AgentDecision` (`services/agent_schema.py`)
- **Parser**: `parse_agent_decision()` (`services/agent_output_parser.py`)
- **Prompt sections**: round-0 output format, per-round task block, JSON reminder
- **Assembler**: `assemble_loop_prompt()` used by `EvidenceContextManager` and `WorkflowContext`

## Flow

1. Workflow prepare builds factual prompt skeleton (`06_ai_prompt.md` for crash via `_build_prompt_final_tip`).
2. When `agent_loop=context_loop`, round-0 sections come from `build_round0_*` helpers in the contract.
3. `AgentRuntime` runs `run_agent_context_loop()` via `services/analyze_pipeline.py`.
4. Follow-up rounds inject resolved context + `## 本轮任务` + `## 输出契约（续轮提醒）`.

## Artifacts

- `round_N/06_ai_prompt.md` — prompt snapshot per round
- `artifacts/analyze_round_N.json` — harness checkpoint payload
- `context_session.json` — canonical schema-v2 session, including rounds,
  request ledger, statistics, status, and termination reason

`agent_rounds` remains a compatibility projection of `context_session.rounds`.
Round artifacts and `05b_pre_round_add_res.json` include `schema_version: 2`.

Termination reasons are `model_final`, `max_rounds`,
`all_requests_blocked`, `invalid_schema`, `llm_budget_exhausted`, and
`llm_error`.

## Context request types

| type | symbol | resolver |
| --- | --- | --- |
| `function` / `field` / `references` / `callers` | symbol or file+line | `context_source_resolver` |
| `grep` | pattern (required); optional `file` as path_glob | `context_repo_search_resolver` |
| `read_file` | `file` (required); optional `line_number` / `line_end` | `context_repo_search_resolver` |
| `memory_pattern` | query text (required) | `context_observation_resolver` |
| `verification_log` | optional provider filter | observation store + verification payload |
| `trace_snippet` | `recent` or event prefix | read-only `RunTrace` snapshot |

Runtime observations are stored separately from evidence. Tool results,
failures, policy denials, verification, and judge feedback can be exposed to a
follow-up turn without replaying the full trace. The deterministic judge runs
at the decide boundary and requires executable verification for repair claims.
- `09_evidence.json` — evidence store sidecar (not merged into 06 skeleton by default)

## Related

- [AGENT_RUNTIME_LIFECYCLE.md](./AGENT_RUNTIME_LIFECYCLE.md)
- [HARNESS_MIGRATION.md](../cli/HARNESS_MIGRATION.md)
