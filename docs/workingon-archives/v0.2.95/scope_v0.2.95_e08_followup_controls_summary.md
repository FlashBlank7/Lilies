# v0.2.95 E08 follow-up controls scope

- Raw evidence: `docs/workingon-archives/v0.2.95/scope_v0.2.95_e08_followup_controls.json`
- Status: `completed`
- Decision: `select_editable_policy_controls_api`
- Selected slice: `editable_policy_controls_api`
- Next version: `v0.2.96_e08_editable_policy_controls_api`
- First design: `docs/current-design/design_v0_2_96_e08_editable_policy_controls_api.md`
- E07 invariant: `preserved`
- Reason: Existing E08 read-only, matrix, cancellation/budget, and worker lease evidence should not be repeated; the next product gap is an audited backend mutation contract for policy controls.

## Ranked Candidates

- `editable_policy_controls_api` score `65`; already_closed `False`; blocked `False`; status `selected_candidate`
- `studio_editable_controls` score `15`; already_closed `False`; blocked `False`; status `defer_until_backend_contract`
- `worker_lease_behavior_repeat` score `-50`; already_closed `True`; blocked `False`; status `already_closed_slice`
- `cancellation_budget_behavior_repeat` score `-50`; already_closed `True`; blocked `False`; status `already_closed_slice`
- `full_sidecar_completion_claim` score `-100`; already_closed `False`; blocked `True`; status `blocked_by_scope`

## v0.2.96 Verification Targets

- backend API mutation tests for editable policy-controls
- invalid or unsafe policy change rejection tests
- before/after policy-controls evidence artifact
- E07 guarded default no-change assertion
