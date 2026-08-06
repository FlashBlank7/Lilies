# implementation_v0.2.38_e05_template_reuse_depth_live_comparison

## 1. Implemented Changes

- Added `scripts/e05_template_reuse_depth_experiment.py`.
- Added `tests/test_e05_template_reuse_depth_experiment.py`.
- Generated E05 paid/live JSON evidence and cost chart.
- Generated concise DOCX experiment report.
- Updated `docs/experiment-status/v0.2_experiment_status.md`.

## 2. Files / Modules

- `scripts/e05_template_reuse_depth_experiment.py`
- `tests/test_e05_template_reuse_depth_experiment.py`
- `docs/experiment-status/evidence/experiment_v0.2.38_e05_template_reuse_depth_2026_07_09.json`
- `docs/experiment-status/evidence/experiment_v0.2.38_e05_template_reuse_depth_calls.png`
- `docs/experiment-status/reports/2026-07-09_2051_E05_template_reuse_depth_live_comparison.docx`
- `docs/experiment-status/v0.2_experiment_status.md`

## 3. Verification

Focused deterministic verification:

```bash
.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q
```

Result: `3 passed, 1 warning`.

DOCX structural QA:

```bash
unzip -t docs/experiment-status/reports/2026-07-09_2051_E05_template_reuse_depth_live_comparison.docx
```

Result: passed.

DOCX text readback found all required experiment sections:

- `背景`
- `实验设计`
- `结果结论`
- `图片或截图`
- `证据链`
- `工程应用状态`

Visual render QA:

```bash
.venv/bin/python /Users/zhonghaoyang/.codex/plugins/cache/openai-primary-runtime/documents/26.630.12135/skills/documents/render_docx.py docs/experiment-status/reports/2026-07-09_2051_E05_template_reuse_depth_live_comparison.docx --output_dir .tmp/render_e05_v0_2_38 --emit_pdf
```

Result: failed because `soffice` is unavailable.

Full regression: pending before archive.

## 4. Remaining Risk

- The E05 result is not yet applied to engineering behavior.
- The result covers one code-review template-friendly case only.
- `shallow` and `deep` produced evidence of template expansion behavior, but the current Builder template expansion path appears costly and unstable for this case.
- `templates/drone_intervention_v1.json` failed TemplateStore validation during preflight and remains outside the loaded template corpus.

## 5. Live / Paid Model Acceptance

- Required: yes
- Provider/model: configured multi provider with `deepseek-v4-pro` generator and runtime model
- Budget boundary: three arms; `max_turns=42`, `max_repair_cycles=2`, `wait_build timeout=900s`, `provider_timeout=120s`
- Command:

```bash
E05_REUSE_DEPTH_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.38_e05_template_reuse_depth_2026_07_09.json \
E05_REUSE_DEPTH_RUN_ID=v0_2_38_rerun \
E05_REUSE_DEPTH_MAX_TURNS=42 \
E05_REUSE_DEPTH_MAX_REPAIR_CYCLES=2 \
E05_REUSE_DEPTH_TIMEOUT_SECONDS=900 \
E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=120 \
.venv/bin/python scripts/e05_template_reuse_depth_experiment.py
```

Result:

| Depth | Build status | Benchmark | Model/tool calls | Template expand |
| --- | --- | --- | --- | --- |
| `none` | `published` | `0.85 / 1.0` | `24 / 41` | `0` |
| `shallow` | `needs_attention` | `0.733 / 0.0` | `42 / 73` | `2` |
| `deep` | `needs_attention` | `0.733 / 0.0` | `42 / 77` | `2` |

## 6. Next Design Decision

- Current design status: completed
- Evidence: JSON evidence, DOCX report, focused tests, ledger update
- Next-stage guidance: prohibited here; record it in the stage report only.
