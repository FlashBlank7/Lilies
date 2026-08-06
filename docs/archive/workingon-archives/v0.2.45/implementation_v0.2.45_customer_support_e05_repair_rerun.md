# implementation_v0.2.45_customer_support_e05_repair_rerun

## 1. Scope

This implementation evidence closes the v0.2.45 accepted design:

- `docs/current-design/design_v0.2.45_customer_support_paid_rerun_report_v1.md`

No backend behavior was changed in v0.2.45. The stage performed a bounded paid/live experiment rerun, generated experiment artifacts, updated the experiment ledger, and archived the design/work evidence.

## 2. Paid/live Command

```bash
E05_REUSE_DEPTH_EXPERIMENT_VERSION=v0.2.45 \
E05_REUSE_DEPTH_CASE=customer_support_router \
E05_REUSE_DEPTH_RUN_ID=v0.2.45_e05_customer_support_rerun_20260710 \
E05_REUSE_DEPTH_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.45_e05_customer_support_rerun_2026_07_10.json \
E05_REUSE_DEPTH_MAX_TURNS=42 \
E05_REUSE_DEPTH_MAX_REPAIR_CYCLES=2 \
E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS=600 \
E05_REUSE_DEPTH_TIMEOUT_SECONDS=900 \
E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=180 \
.venv/bin/python scripts/e05_template_reuse_depth_experiment.py
```

Provider/model:

- Provider: `multi`
- Generator model: `deepseek-v4-pro`
- Runtime model: `deepseek-v4-pro`
- Paid/live skipped: `false`

## 3. Result

| Depth | Build status | Elapsed | Model/tool calls | Template expands | Benchmark | Failure |
| --- | --- | --- | --- | --- | --- | --- |
| `none` | `published` | `198.961s` | `25/67` | `0` | passed, score `0.85` | none |
| `shallow` | `ready` | `545.849s` | `42/54` | `1` | passed, score `0.85` | none |
| `deep` | `needs_attention` | `602.071s` | `37/56` | `1` | passed, score `0.85` | `BuildDeadlineExceeded` after `600.004s` |

Raw evidence:

- `docs/experiment-status/evidence/experiment_v0.2.45_e05_customer_support_rerun_2026_07_10.json`

Derived artifacts:

- `docs/experiment-status/evidence/experiment_v0.2.45_e05_customer_support_rerun_summary.png`
- `docs/experiment-status/reports/2026-07-10_0103_E05_customer_support_rerun_after_guardrails.docx`

## 4. Interpretation

v0.2.44 deterministic repair is partially validated:

- `shallow` improved from v0.2.43 provider timeout / `needs_attention` to v0.2.45 `ready`.
- `none` improved from published with benchmark case missing `if_else` to published with benchmark case pass.
- `deep` improved from invalid draft to benchmark-clean structure, but remains not closed because the build hit the 600 second Builder deadline.

This stage must not close original E05. The evidence is still limited to one customer-support task family, and deep reuse has a long-chain stability problem.

## 5. DOCX QA

Structural QA passed:

- `word/document.xml` exists.
- Embedded chart image exists.
- Required sections are present: background, experiment design, result, conclusion, evidence chain.

Visual render QA:

- Attempted with `render_docx.py`.
- Result: failed because `soffice` is not installed locally.
- The DOCX is still delivered with structural QA and chart visual inspection.

## 6. Verification

Focused E05 regression:

```bash
.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q
```

Result: `7 passed, 1 warning`.

Full regression:

```bash
.venv/bin/python -m pytest -q
```

Result: `108 passed, 1 warning`.

Static compile:

```bash
.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts
```

Result: passed.
