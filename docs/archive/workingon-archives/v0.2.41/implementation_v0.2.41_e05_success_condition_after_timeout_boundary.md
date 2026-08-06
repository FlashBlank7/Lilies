# implementation_v0.2.41_e05_success_condition_after_timeout_boundary

## 1. Completion Time

2026-07-09 23:11 JST

## 2. Implemented Changes

### E05 evidence capture

- Updated `scripts/e05_template_reuse_depth_experiment.py`.
- `summarize_events()` now extracts:
  - provider failure events;
  - model timeout events;
  - `build.needs_attention` failure metadata.
- Added `summarize_failure()` so every completed arm records:
  - build status/error;
  - Platform Harness task status/error;
  - `task_failure`;
  - `event_failures`;
  - timeout-like classification.

### Regression tests

- Updated `tests/test_e05_template_reuse_depth_experiment.py`.
- Added deterministic coverage for v0.2.40 timeout/failure metadata shapes.

### Paid/live experiment

Command:

```bash
E05_REUSE_DEPTH_EXPERIMENT_VERSION=v0.2.41 \
E05_REUSE_DEPTH_RUN_ID=v0.2.41_e05_success_condition_20260709 \
E05_REUSE_DEPTH_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.41_e05_success_condition_2026_07_09.json \
E05_REUSE_DEPTH_MAX_TURNS=42 \
E05_REUSE_DEPTH_MAX_REPAIR_CYCLES=2 \
E05_REUSE_DEPTH_TIMEOUT_SECONDS=900 \
E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=180 \
.venv/bin/python scripts/e05_template_reuse_depth_experiment.py
```

Provider/model:

- Provider: `multi`
- Generator model: `deepseek-v4-pro`
- Runtime model: `deepseek-v4-pro`

Raw evidence:

- `docs/experiment-status/evidence/experiment_v0.2.41_e05_success_condition_2026_07_09.json`
- `docs/experiment-status/evidence/experiment_v0.2.41_e05_success_condition_summary.png`

DOCX report:

- `docs/experiment-status/reports/2026-07-09_2311_E05_success_condition_after_timeout_boundary.docx`

## 3. Paid/Live Result

| Depth | Build status | Elapsed | Model/tool calls | Suggest/expand | Benchmark | Timeout evidence |
| --- | --- | --- | --- | --- | --- | --- |
| `none` | `needs_attention` | `437.063s` | `27 / 45` | `1 / 0` | score `0.85`, pass_rate `1.0` | `model stream timed out after 180s`; `task_failure.type=model_provider`; `timeout_like=true` |
| `shallow` | `published` | `123.267s` | `20 / 30` | `1 / 1` | score `0.85`, pass_rate `1.0` | none |
| `deep` | `ready` | `331.196s` | `47 / 57` | `1 / 1` | score `0.85`, pass_rate `1.0` | none |

## 4. Verification

Focused tests:

```bash
.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q
```

Result:

```text
4 passed, 1 warning
```

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

```text
102 passed, 1 warning
```

Compile check:

```bash
.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts
```

Result:

```text
compileall completed successfully
```

DOCX QA:

- `unzip -t` passed.
- Structural readback: 31 paragraphs, 1 table, 1 inline image.
- Visual render QA failed because `soffice` is unavailable on this machine.

## 5. Conclusion

For this code-review Template-friendly case, `shallow` reuse is the best supported current operating point: it reached `published`, expanded a marketplace template once, passed benchmark, and used fewer calls than `deep`.

Original E05 remains open. This is still one requirement family, and `none` failed due a provider stream timeout rather than a clean no-reuse quality comparison. The result supports a product hypothesis, not a global rule.

## 6. Carried Forward

- Add more E05 cases before closing original Template reuse-depth question.
- Consider a build-level watchdog or progress telemetry for long Builder runs; v0.2.40 covers stream timeout, not whole-build duration.
- Investigate pydantic serializer warnings seen during deep arm if they recur in later experiments.
