# Agent Runtime Lifecycle

`AgentRuntime` is the single business lifecycle shared by the CLI, daemon and
Skill Runtime. `direct`, `langchain` and `langgraph` select only the LLM
backend; they do not define separate orchestration implementations.

`AgentRuntime` + `crash_analysis_workflow` are the only supported runtime
entry points; LangGraph is an LLM backend and not a second state graph.

## Stages

Every run uses the same stages:

```text
observe -> analyze -> plan -> act -> diff_review -> verify -> rollback/sync_worktree -> post_fix_diagnosis -> decide
```

The crash workflow completes toolchain **prepare** inside `execute_workflow_prepare`,
then `AgentRuntime` runs the context loop through `services/analyze_pipeline.py`
when `scope=full`. Each analyze round writes `artifacts/analyze_round_N.json`
and a harness checkpoint (`analyze:round:N`). Mid-round replay is not supported;
replay still hydrates from `artifacts/stage_analyze_result.json`.

Post-fix actions run through `services/repair_pipeline.py` and
`services/repair_actions.py` (`RuntimeActionExecutor` handlers:
`apply_patch`, `inspect_diff`, `verify`, `rollback`, `sync_worktree`,
`post_fix_diagnosis`) via `AgentRuntime.run_repair_and_verify()` for
`act/verify/decide`.

Runtime state is available in result metadata as `runtime_state`. Full event
streams are written to `00_runtime_trace.json` in report directories.

## Verification

Verification is opt-in. Discovery only reports candidates and never executes
them. A configured provider exposes four operations:

- `discover`: read-only candidate discovery
- `validate`: command/capability validation and approval request
- `execute`: explicit execution after approval
- `summarize`: stable result projection

When no provider is configured, a successful fix moves the run to
`verification_pending`. The daemon resumes it only after the user submits an
explicit command. Verification results include the command fingerprint and
approval record.

Verification failure triggers rollback when backups exist (CLI and daemon).
Verification failure may run **reanalyze only** (`parse_stack_only` via
`reanalyze_diagnosis`; no automatic re-apply). Verification success may run
`post_fix_diagnosis` (`parse_stack_only` re-run).

## Tool policy and approval

Tools declare harness metadata on `ToolDefinition` (`risk`, `side_effect`,
`requires_approval`). `ToolExecutionGateway` enforces `PolicyEngine` rules,
including optional path allowlists for tool inputs.

High-risk tools require an explicit, in-process `RuntimeAuthorization` issued
after the daemon approval state machine succeeds. A JSON boolean or arbitrary
request field cannot authorize a tool. The daemon entry point is
`POST /runs/{id}/tool-approval`.

## Checkpoints and trace

Runtime checkpoints are JSON serializable and contain the stage, status,
reason, and optional state snapshot. Tool and LLM events remain in `RunTrace`
and are emitted as:

- embedded `runtime_trace` in `00_run_summary.json`
- sidecar `00_runtime_trace.json`

Structured protocol types `ToolCall` and `AgentEvent` are attached to trace
events for replay and evaluation.

Each trace event carries `step_id` (`step_NNNNNN`) as the canonical step
identifier; `seq` preserves the same ordering index for trace consumers.
Parent linkage uses `parent_event_id` / `parent_step_id`.

Replay supports:

- `cli/main.py replay <report_dir>` — full request replay (`--from-stage analyze`, default)
- `cli/main.py replay <report_dir> --from-stage verify` — explicitly replay verification
- `act` replay is forbidden; a new repair task and approval are required after a failed run
- `--checkpoint-id CKPT` — locate checkpoint in `runtime_state.checkpoints` / `00_runtime_trace.json`
- `POST /runs/{id}/retry-stage` with `{"stage":"verify","execute":true,"verification":{"command":[...]}}` — daemon-triggered verification replay; the command is mandatory

Run snapshots are unified in `services/run_snapshot.py` (`HarnessRunSnapshot`)
and persisted by `services/run_store.py` for daemon restore
(`verification_pending` and `approval_required`).

`HarnessRunSnapshot.unified_timeline()` merges harness `RunTrace.events`
(`source=harness`) with daemon transport `event_log` (`source=transport`) for
evaluation and auditing. After CLI subprocess completion, daemon emits a
`trace_loaded` transport event when `00_runtime_trace.json` is adopted.

Evidence from parse/symbolize/04a/code context is ingested stage-wise via
`services/evidence_ingest.py` into `EvidenceStore` (`09_evidence.json`).

Analyze round checkpoints: `artifacts/analyze_round_N.json` with idempotency key
`analyze:round:N`. CLI: `replay --checkpoint-id analyze:round:1`. Mid-round LLM
replay within a single round is not supported.

Context loop protocol: `services/context_loop_contract.py` (see
`docs/architecture/CONTEXT_LOOP_CONTRACT.md`).

Analyze context engineering is implemented by `services/context_engine.py`.
`ContextEngine` owns session/turn state, incremental evidence selection,
request deduplication, resolver dispatch, and prompt budgeting. It does not own
LLM transport, retries, runtime lifecycle, repair, or verification.
Analyze LLM routing, retry, token fallback, and phase display are isolated in
`services/analyze_llm.py`.

Tool execution should go through `ToolExecutionGateway`; embedded helpers
(for example disassembly) emit trace via `services/trace_only_gateway.py`.
Shared routing for snippet and similar calls: `services/tool_invoke.py`
(`invoke_tool`, `snippet_extractor_executor`).

## Decide stage

After repair verify/post_fix_diagnosis, `services/decide_scorer.py`
aggregates four dimensions (patch valid, diff review, verification,
diagnosis stable) into `accept` / `reject` / `pending` / `partial`.
Runtime and offline evaluation share the same scorer. The sidecar artifact
is `10_decide.json` (via `services/stage_artifacts.save_decide_artifact`).

`RunTrace.run_id` aligns with `00_run_request.json` / `STABILITY_AGENT_RUN_ID`
when set on the problem or environment.

## Safety rules

- Candidate commands are never executed implicitly.
- Worktree synchronization occurs only after verification passes.
- Verification failure remains a failure and may trigger rollback.
- An invalid engine is rejected; it is never silently mapped to another
  backend.

## Tool risk defaults

All built-in tools declare harness metadata on `ToolDefinition`:

| risk | cost_class | 典型工具 |
| --- | --- | --- |
| `read_only` | `low` | `crash_log_parser`, `add2line_resolver`, `code_content_provider`, `snippet_extractor`, `symbol_callsite_finder`, diagnosis 读类 |
| `read_only` | `medium` | `repo_search`, `vector_memory_retriever`, `appfreeze_diagnosis`, `jank_analyzer`, `native_leak_analyzer` |
| `read_only` | `medium` | `fix_code_extractor` |
| `workspace_write` | `medium` | `fix_code_applier`（`requires_approval=true`） |
| `execute` | `high` | `run_build`, `run_tests`, `run_static_check`, `reproduce_crash` |

Policy defaults：`read_only` 工具无需 approval；`workspace_write` / `execute` 经 `PolicyEngine` + `RuntimeAuthorization` 显式批准。

`config.metadata.runtime_budget` 与 CLI `--max-llm-calls` / `--max-tool-calls` / `--max-runtime-seconds` 共用同一 `RuntimeBudget` 计数器；repair act 阶段 apply 计入 `llm` 槽位（当 LLM adapter 可用时）。

Daemon `/tool-system/analyze` 与 `/tool-system/native-leak` 经 `_run_tool_system_workflow()` 统一走 `AgentRuntime.run()`，不再直接调用 `ConfigDrivenExecutor.execute_workflow()`。

`ToolExecutionGateway` 强制执行 `ToolDefinition.timeout_sec`、合并 per-tool / policy `allowed_roots`，并在 trace 中记录 `cost_class` / `timeout_sec` / `timed_out`。

`run_build` / `run_tests` / `run_static_check` / `reproduce_crash` 已注册为一等 `RuntimeAction`；`verify` 在显式 `verification.tool` 配置时可委派给对应 action。

## Harness alignment boundary

The runtime exposes one governed surface for the eight in-scope Harness
concerns (multi-agent is intentionally deferred):

| Concern | Canonical implementation | Enforcement/output |
| --- | --- | --- |
| Agent loop | `services/agent_context_loop.py` + `ContextEngine` + `services/feedback_analyze.py` | bounded rounds; verify/judge failure may run one feedback analyze |
| Context engineering | `services/context_engine.py` + `services/context_observation_resolver.py` | stable context, round delta, memory/trace/verification requests |
| Tool system | `ToolExecutionGateway` | validation, timeout, cost budget, policy and observations |
| Observation/feedback | `services/observations.py` | deduplicated tool, policy, verification and judge feedback injected into analyze |
| Memory | `services/memory_feedback.py` + `rag/case_writer.py` | pattern feedback + optional case commit after passed verification |
| Skills | `skill_system/runtime.py` | capability projection, `allowed-tools`, permissions → `PolicyEngine` |
| Safety/permissions | `services/policy.py` + worktree approval | default deny, path roots, explicit authorization |
| Evaluator/Judge | `services/harness_judge.py` + `services/evaluation.py` | deterministic gates, `11_judge.json`, manifest expectations |

The analyze loop is context-request-driven by design: domain workflows still
own deterministic preparation and repair/verification remain explicit stages.
This prevents an unbounded arbitrary-tool agent from bypassing existing
approval and replay contracts; a generic autonomous tool planner is a future
capability, not silently implied by this alignment.

`EvidenceContextManager.assemble_context_loop_prompt()` 是 context loop 提示词骨架的唯一组装入口；`WorkflowContext.select_prompt()` 是 LLM 调用前的唯一预算门控。
