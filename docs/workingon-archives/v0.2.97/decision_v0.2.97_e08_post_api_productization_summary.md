# v0.2.97 E08 post-API productization decision

- Raw evidence: `docs/workingon-archives/v0.2.97/decision_v0.2.97_e08_post_api_productization.json`
- Status: `completed`
- Decision: `select_studio_editable_policy_controls`
- Selected path: `studio_editable_policy_controls`
- Next version: `v0.2.98_e08_studio_editable_policy_controls`
- First design: `docs/current-design/design_v0_2_98_e08_studio_editable_policy_controls.md`
- E07 invariant: `preserved`
- Reason: Backend editable policy-controls API exists; the next product value is exposing it safely to operators.

## Ranked Candidates

- `studio_editable_policy_controls` score `80`; status `candidate`; disposition: select as next product slice because backend PATCH contract exists and operator surface is now valuable
- `pause_e08_after_api` score `55`; status `candidate`; disposition: reject for now because the API is useful but not yet operator-accessible
- `operator_runbook_lifecycle` score `45`; status `candidate`; disposition: defer until Studio or equivalent operator surface exists
- `broader_sidecar_boundary_closure` score `-10`; status `candidate`; disposition: defer because it is too broad for the immediate post-API slice

## v0.2.98 Verification Targets

- frontend type contract for policy-controls PATCH request/response
- Studio operator form or control surface for editable policy controls
- browser or frontend executable verification
- backend policy-controls API regression reuse
- E07 guarded default no-change assertion
