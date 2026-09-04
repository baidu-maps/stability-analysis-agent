# Evaluation Matrix

Model-independent regression signals live in `services/evaluation.py`.

## Diagnosis fields

- `category` / `fault_mode` / `location` / `function`: compared against expected values from the case manifest.
- Nested `04a_crash_diagnosis.json` is normalized via `services/evidence_ingest.normalize_diagnosis_for_evaluation()` (`crash_classification`, `stack_summary`, `evidence_compass`).
- `missing_evidence_count`: reads `evidence_compass.missing_evidence` when present.
- `evidence_layers_available` / `evidence_layers_total` / `confidence_ceiling`: derived from `evidence_compass.layers`.

## Runtime trace fields

- `context_request_valid_rate`, `tool_success_rate`, `policy_denials`, `rollback_triggered` — from `00_runtime_trace.json` events.
- `evidence_item_count` / `evidence_sources` — from result metadata or `09_evidence.json`.

## Harness snapshot

- `HarnessRunSnapshot.unified_timeline()` merges harness trace and daemon transport events for offline evaluation fixtures.

- `context_loop_valid_rate_avg` — matrix summary average of per-case valid rates

Run matrix tests:

```bash
python3 -B -m unittest test.harness.test_evaluation_matrix test.harness.test_evaluation_suite test.harness.test_evidence_ingest test.harness.test_context_loop_contract
python3 scripts/run_evaluation_matrix.py --manifest examples/crash_cases/demo_basic/evaluation_manifest.json --report-root test/harness/fixtures/reports
```
