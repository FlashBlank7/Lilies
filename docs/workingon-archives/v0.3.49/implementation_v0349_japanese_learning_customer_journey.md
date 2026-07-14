# v0.3.49 implementation archive: Japanese learning customer journey

## Scope

This stage specialized the v0.3.x usability work around the concrete ordinary-user scenario from the user: a Japanese-language student wants a workflow that accepts a topic, gathers public video-comment expression clues, extracts real spoken Japanese expressions, and returns a daily learning summary.

## Implementation Summary

- Added a `japanese_language_student` homepage customer example in zh/en copy.
- Expanded requirement readiness so `学生`, `学习者`, `learner`, and `student` count as valid audience signals.
- Added no-model Japanese-learning safe draft seeding:
  - `topic` Start input labeled `关注的日语主题`.
  - Comment-clue placeholder step.
  - Spoken-expression extraction placeholder step.
  - Daily spoken Japanese summary answer step.
  - Structural acceptance case using sample topic `校园生活`.
- Added Run tab scenario detection from workflow name, description, requirement, node labels, and config.
- Added learning-specific Run tab guidance:
  - scenario guidance card,
  - topic input hint,
  - learning-language progress labels,
  - final result expectation checklist.
- Added v0.3.49 static evidence and tests.
- Updated the current v0.3.x release gate from 271 to 279 expected passes.

## Safety Boundary

- No Builder Team call was made by the v0.3.49 harness.
- No workflow run/test/publish/draft mutation endpoint is called by the evidence script.
- Default evidence is static/source-only; live mode is limited to read-only `GET /health`.
- External video scraping is not claimed in this version. The draft uses placeholder transform steps until a bounded external-evidence validation version is designed.

## Verification

| Check | Result |
| --- | --- |
| Frontend TypeScript | pass |
| Focused v0.3.49 pytest | `8 passed` |
| v0.3.49 evidence script | pass |
| Current v0.3.x release gate | `279 passed, 1 warning` |

## Evidence

- `docs/workingon-archives/v0.3.49/japanese_learning_customer_journey_v0.3.49.json`
- `scripts/v03_49_japanese_learning_customer_journey.py`
- `tests/test_v03_49_japanese_learning_customer_journey.py`
- `docs/testing/regression_lanes.json`

## Historical Designs

- `docs/historical-designs/v0.3.49_design_japanese_learning_intake_example_v1.md`
- `docs/historical-designs/v0.3.49_design_japanese_learning_safe_draft_v1.md`
- `docs/historical-designs/v0.3.49_design_japanese_learning_run_guidance_v1.md`
- `docs/historical-designs/v0.3.49_design_japanese_learning_result_expectation_v1.md`
