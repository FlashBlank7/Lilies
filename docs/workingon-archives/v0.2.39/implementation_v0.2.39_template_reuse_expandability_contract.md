# implementation_v0.2.39_template_reuse_expandability_contract

## 1. Implementation Summary

v0.2.39 applied the E05 marketplace Template expandability fix.

Changed files:

- `platform/backend/src/agent_platform/builder.py`
- `scripts/e05_template_reuse_depth_experiment.py`
- `tests/test_workflow.py`
- `docs/experiment-status/v0.2_experiment_status.md`
- `docs/experiment-status/evidence/experiment_v0.2.39_e05_after_expandability_fix_2026_07_09.json`
- `docs/experiment-status/reports/2026-07-09_2305_E05_after_expandability_fix_validation.docx`

## 2. Design 1 Evidence: Marketplace Template Expandability Contract

Implemented:

- `template_list` now returns both server-defined templates and marketplace templates.
- Every returned template includes a `source` field:
  - `server_defined` for `BlockRegistry.template_names()`.
  - `marketplace` for `TemplateStore.list()`.
- `template_suggestions` now marks suggested marketplace templates with `source="marketplace"`.
- `template_expand` now:
  - checks `TemplateStore.names()` first;
  - expands marketplace templates with `TemplateStore.expand_into_workflow()`;
  - falls back to `BlockRegistry.expand_template()` for server-defined templates;
  - returns `source` in the tool result.

Deterministic verification:

```bash
.venv/bin/python -m pytest \
  tests/test_e05_template_reuse_depth_experiment.py \
  tests/test_workflow.py::test_builder_can_expand_claude_like_template_into_editable_draft \
  tests/test_workflow.py::test_builder_template_list_includes_marketplace_and_server_defined_templates \
  tests/test_workflow.py::test_builder_can_expand_marketplace_template_into_editable_draft \
  -q
```

Result:

```text
6 passed, 1 warning
```

## 3. Design 2 Evidence: E05 Post-fix Paid/Live Validation

Command:

```bash
E05_REUSE_DEPTH_EXPERIMENT_VERSION=v0.2.39 \
E05_REUSE_DEPTH_RUN_ID=v0.2.39_e05_after_expandability_fix_20260709 \
E05_REUSE_DEPTH_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.39_e05_after_expandability_fix_2026_07_09.json \
E05_REUSE_DEPTH_MAX_TURNS=42 \
E05_REUSE_DEPTH_MAX_REPAIR_CYCLES=2 \
E05_REUSE_DEPTH_TIMEOUT_SECONDS=900 \
E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=180 \
.venv/bin/python scripts/e05_template_reuse_depth_experiment.py
```

Provider/model:

- Provider: `multi`
- Configured model: `deepseek/deepseek-v4-pro`
- Generator/runtime model: `deepseek-v4-pro`

Result summary:

| arm | build status | elapsed | model/tool calls | template_expand | benchmark |
| --- | --- | ---: | ---: | --- | --- |
| `none` | `needs_attention` | `536.444s` | `46/59` | none | score `0.85`, pass_rate `1.0` |
| `shallow` | `needs_attention` | `482.334s` | `22/30` | `code_reviewer`, `source=marketplace`, success | score `0.85`, pass_rate `1.0` |
| `deep` | `published` | `147.504s` | `23/32` | `code_reviewer`, `source=marketplace`, success | score `0.85`, pass_rate `1.0` |

Evidence:

- Raw JSON: `docs/experiment-status/evidence/experiment_v0.2.39_e05_after_expandability_fix_2026_07_09.json`
- DOCX report: `docs/experiment-status/reports/2026-07-09_2305_E05_after_expandability_fix_validation.docx`

DOCX QA:

- Structural readback passed: 24 paragraphs, 3 tables, and embedded `word/media/image1.png`.
- Visual render QA not completed because local `soffice` is unavailable.

## 4. Completion Decision

`design_v0.2.39_marketplace_template_expand_contract_v1.md`: completed.

Reason:

- Code path implemented.
- Server-defined template expansion still passes.
- Marketplace template listing and expansion are covered by deterministic tests.
- Paid/live shallow and deep arms both executed successful `template_expand(code_reviewer)` with `source=marketplace`.

`design_v0.2.39_e05_after_expandability_fix_validation_v1.md`: completed with bounded conclusion.

Reason:

- Paid/live rerun completed and produced raw evidence + DOCX report.
- Ledger updated without overstating the result.
- E05 marketplace expandability contract is `验证应用`.
- Original E05 quality-benefit question remains open.

## 5. Remaining Risk

- The E05 original question is not fully closed. The next experiment should validate the success conditions for deep reuse, not merely template expandability.
- `none` and `shallow` still ended `needs_attention`; this should not be hidden by deep's success.
- `shallow` failed due `DeepSeek request timed out`, which points to provider-timeout/build-timeout behavior as a possible Platform Harness task.
- The invalid built-in `drone_intervention_v1.json` remains out of scope.

## 6. Archive Readiness

- Deterministic tests: passed.
- Paid/live validation: completed.
- DOCX experiment report: completed.
- Experiment ledger: updated.
- Current-design archive: completed.
- Workingon archive: completed.
- Stage report: completed: `docs/stage-reports/v0.2.39_template_reuse_expandability_contract.md`.
