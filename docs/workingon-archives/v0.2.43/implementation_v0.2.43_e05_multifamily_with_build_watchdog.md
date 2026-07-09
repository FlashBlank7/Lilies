# implementation_v0.2.43_e05_multifamily_with_build_watchdog

## 1. Scope

This implementation completes the accepted v0.2.43 target:

- make the E05 Template reuse-depth runner case-aware;
- pass Builder `max_elapsed_seconds` from the experiment runner;
- run a second, non-code-review Template-friendly paid/live case;
- produce raw evidence, chart, DOCX report, and ledger updates.

Original E05 remains open. This stage adds a second task-family evidence point; it does not prove a universal Template reuse policy.

## 2. Code Changes

Changed files:

- `scripts/e05_template_reuse_depth_experiment.py`
- `tests/test_e05_template_reuse_depth_experiment.py`

Implemented behavior:

- Added `ExperimentCase`.
- Added `code_review` and `customer_support_router` experiment cases.
- Added `E05_REUSE_DEPTH_CASE` selection.
- Added `E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS` and `build_request_payload()`.
- Updated preflight, Builder build payloads, and benchmark payloads to use the selected case.
- Added deterministic tests for customer-support case mapping and optional build deadline payload.

## 3. Focused Verification

Command:

```bash
.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q
```

Result:

```text
6 passed, 1 warning
```

Full regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

```text
105 passed, 1 warning
```

Static compile:

```bash
.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts
```

Result: passed.

## 4. Paid/Live Experiment

Command:

```bash
E05_REUSE_DEPTH_EXPERIMENT_VERSION=v0.2.43 \
E05_REUSE_DEPTH_CASE=customer_support_router \
E05_REUSE_DEPTH_RUN_ID=v0.2.43_e05_customer_support_20260709 \
E05_REUSE_DEPTH_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.43_e05_customer_support_2026_07_09.json \
E05_REUSE_DEPTH_MAX_TURNS=42 \
E05_REUSE_DEPTH_MAX_REPAIR_CYCLES=2 \
E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS=600 \
E05_REUSE_DEPTH_TIMEOUT_SECONDS=900 \
E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=180 \
.venv/bin/python scripts/e05_template_reuse_depth_experiment.py
```

Provider/model:

- Provider: `multi`
- Generator/runtime model: `deepseek-v4-pro`
- Configured model: `deepseek/deepseek-v4-pro`

Budget:

- Arms: `none`, `shallow`, `deep`
- `max_turns=42`
- `max_repair_cycles=2`
- `max_elapsed_seconds=600`
- provider stream timeout `180s`
- script wait timeout `900s`

## 5. Results

| Depth | Builder status | Elapsed | Model/tool calls | Template behavior | Benchmark case | Failure meaning |
| --- | --- | ---: | ---: | --- | --- | --- |
| `none` | `published` | `259.449s` | `37/64` | suggestions `1`, expands `0` | failed, score `0.78`, missing `if_else` | Build succeeded, but benchmark found an explicit architecture gap. |
| `shallow` | `needs_attention` | `247.175s` | `7/13` | suggestions `1`, expands `1`, `customer_support_router` source `marketplace` | passed, score `0.85` | Provider stream timeout captured as retryable `model_provider`. |
| `deep` | `needs_attention` | `581.918s` | `42/67` | suggestions `1`, expands `1`, `customer_support_router` source `marketplace` | passed, score `0.85` | Runtime invalid draft: unreachable nodes and required-node drift. |

Raw evidence:

- `docs/experiment-status/evidence/experiment_v0.2.43_e05_customer_support_2026_07_09.json`
- `docs/experiment-status/evidence/experiment_v0.2.43_e05_customer_support_summary.png`

DOCX report:

- `docs/experiment-status/reports/2026-07-10_0024_E05_customer_support_reuse_depth.docx`

## 6. Interpretation

Second-family evidence does not support a blanket rule that deeper Template reuse is better.

The result splits into three important facts:

- Template discovery and `template_expand` work for `customer_support_router`.
- `shallow` and `deep` both achieve benchmark node coverage, but neither reaches Builder success.
- `none` reaches Builder success, but fails benchmark case coverage because it omits `if_else`.

This means E05 should remain open. The next narrow engineering implication is customer-support template customization reliability, not a product policy that always prefers or rejects reuse.

## 7. DOCX QA

Structural QA:

- `unzip -t` passed.
- DOCX readback: `25` paragraphs, `3` tables, `1` inline image.
- Chart image was visually checked and redrawn once to remove label overlap.

Visual render QA:

- Attempted with Documents skill `render_docx.py`.
- Blocked by `FileNotFoundError: [Errno 2] No such file or directory: 'soffice'`.

## 8. Remaining Risk

- Original E05 still needs more task families before global closure.
- Benchmark report currently needs careful interpretation because suite `passed=true` can coexist with single-case failure when `minimum_pass_rate=0.0`.
- Customer-support template reuse needs a later repair stage for post-expansion reachability, required-node consistency, and validation reporting.
