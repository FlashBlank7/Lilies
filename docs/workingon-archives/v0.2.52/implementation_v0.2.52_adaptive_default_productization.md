# implementation_v0.2.52_adaptive_default_productization

## Goal

Close the accepted `v0.2.52` design set:

1. make `adaptive` the default Builder/API suggestion mode,
2. add observability and rollback metadata for that default switch.

## Changes

### Design 1: adaptive default API and Builder behavior

- Added shared default suggestion metadata in `template_strategy.py`:
  - `DEFAULT_TEMPLATE_SUGGESTION_REUSE_DEPTH = "adaptive"`
  - `DEFAULT_TEMPLATE_SUGGESTION_POLICY_VERSION = "v0.2.52_adaptive_default_productization"`
  - `suggestion_default_metadata(...)`
- Updated `/api/v1/templates/suggestions` so omitted `reuse_depth` now defaults to `adaptive` instead of `shallow`.
- Updated Builder `template_suggestions` tool execution so omitted `reuse_depth` defaults to:
  - concrete BuildPlan depth when one already exists,
  - otherwise policy-default `adaptive`.
- Tightened Builder prompt wording so adaptive is the preferred default suggestion mode unless a task or experiment explicitly asks for fixed depth.

### Design 2: observability and rollback metadata

- Added response metadata that distinguishes explicit requests from policy-defaulted ones:
  - `reuse_depth_source`
  - `defaulted_by_policy`
  - `default_policy_version`
  - `available_overrides`
- Propagated the same metadata to:
  - API template suggestion results
  - Builder `template_suggestions` tool results
  - individual template payloads inside those results
- Kept explicit override semantics intact: callers can still force `none`, `shallow`, `deep`, or `adaptive`.

## Files

- `platform/backend/src/agent_platform/template_strategy.py`
- `platform/backend/src/agent_platform/api.py`
- `platform/backend/src/agent_platform/builder.py`
- `tests/test_workflow.py`

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| Focused backend regression | `3 passed, 69 deselected, 1 warning` | `./.venv/bin/python -m pytest tests/test_workflow.py -k 'template_suggestions_include_reuse_depth_actions or builder_template_suggestions' -q` |

## Live / Paid Acceptance

- Required: no
- Provider/model:
- Budget:
- Command:
- Result:
- Skip reason: this stage productizes a policy whose paid/live gate already closed in `v0.2.51`.

## Remaining Risk

- Adaptive is now the product default suggestion mode, but `v0.2.52` does not yet include a post-switch bounded live acceptance slice.
- No new UI was needed for rollback because explicit API/tool overrides already exist; a later stage can still add richer surfacing if operators need it.

## Design Decision

- archive current version
