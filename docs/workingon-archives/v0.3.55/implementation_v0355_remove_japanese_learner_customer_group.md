# v0.3.55 Remove Japanese Learner Customer Group

- Source stage report: `docs/stage-reports/v0.3.54_acceptance_auto_repair.md`
- User-triggered task: remove Japanese learner from the customer groups.
- Closure: homepage customer scenarios and customer requirement examples no longer expose Japanese learner / 日语学习者.

## Completed

| Area | Result | Evidence |
| --- | --- | --- |
| Customer scenarios | Removed `日语学习者` and `Japanese learner` cards from the bilingual customer group list. | `platform/frontend/lib/i18n.ts` |
| Customer examples | Removed the `japanese_language_student` requirement example from Chinese and English examples. | `platform/frontend/lib/i18n.ts` |
| Explicit workflow support | Preserved hidden explicit Japanese workflow support for existing/typed requirements. | `platform/frontend/app/page.tsx`; `platform/frontend/app/applications/[id]/page.tsx` |
| Historical compatibility | Updated v0.3.49 regression checks to treat the customer persona as superseded while keeping explicit workflow support. | `scripts/v03_49_japanese_learning_customer_journey.py`; `tests/test_v03_49_japanese_learning_customer_journey.py` |
| Release gate | Added v0.3.55 test and raised current gate to 319 passes. | `docs/testing/regression_lanes.json` |

## Verification

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest tests/test_v03_55_remove_japanese_learner_customer_group.py tests/test_v03_49_japanese_learning_customer_journey.py tests/test_v03_50_bounded_japanese_learning_runtime_validation.py -q` | `21 passed, 1 warning` |
| `.venv/bin/python scripts/v03_55_remove_japanese_learner_customer_group.py --output docs/workingon-archives/v0.3.55/remove_japanese_learner_customer_group_v0.3.55.json` | `status=passed` |
| `PATH="/Users/zhonghaoyang/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint` in `platform/frontend` | pass |
| Current v0.3.x release gate from `docs/testing/regression_lanes.json` | `319 passed, 1 warning` |

## Notes

- This change removes the customer-facing persona and sample, not the lower-level ability to handle an explicitly typed Japanese-learning requirement.
- The retained explicit support avoids breaking existing drafts or tests that validate the bounded Japanese-learning workflow path.
