# implementation_v0.2.65_e08_policy_controls_surface

## Source

- Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.64_productization_lane_reselection.md`
- Source stage task: `Implement E08 policy controls surface`; `Define E08 boundary language for soft passmode vs Platform Harness hard boundary`; `Preserve complexity-router guarded rollout`
- Current design: `docs/current-design/design_e08_policy_controls_api_contract.md`; `docs/current-design/design_e08_policy_controls_studio_surface.md`; `docs/current-design/design_e08_boundary_language_and_complexity_preservation.md`

## Changes

- Extended `PlatformHarness.policy_controls()` with read-only `e08_boundary`.
- Added API regression assertions for E08 current slice, source evidence, soft passmode, hard boundary, and control statuses.
- Extended Studio TypeScript types for the new `e08_boundary` shape.
- Added a compact E08 boundary panel to the existing monitor policy-controls section.
- Added zh/en UI labels and CSS for the E08 boundary panel.
- Preserved complexity-router as a deferred guarded-rollout candidate in the stage evidence; no complexity-router defaults were changed.

## Evidence / Intermediate Results

New API field:

- `GET /api/v1/platform/harness/policy-controls` now returns `e08_boundary`.

Important returned language:

- `soft_passmode.enforcement = soft_configurable`
- `hard_boundary.enforcement = hard_boundary`
- `not_full_sidecar_completion = true`

## Verification

```text
.venv/bin/python -m pytest tests/test_workflow.py -k policy_controls
```

Result:

```text
1 passed, 71 deselected, 1 warning in 0.41s
```

Frontend verification:

```text
npm run lint
node_modules/.bin/tsc --noEmit
```

Result:

```text
npm: command not found
env: node: No such file or directory
```

Frontend TypeScript was updated narrowly and inspected statically, but executable TypeScript verification is blocked by missing local `node`.

## Remaining Risk

- The Studio change was not executable-verified because `node`/`npm` is unavailable in this shell.
- The surface is read-only and does not add editable policy controls.
- This is not full Platform Harness sidecar completion; broader cancellation, budget, worker lease lifecycle, editable controls, and runbook closure remain future work.
- Complexity-router remains deferred until guardrails and rollout design are selected by a future stage report.

## Design Decision

- Continue current design / revise current design / proceed to next design / blocked: proceed to archive with frontend verification caveat
