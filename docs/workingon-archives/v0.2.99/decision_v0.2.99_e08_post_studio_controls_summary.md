# v0.2.99 E08 post-Studio controls decision

- Raw evidence: `docs/workingon-archives/v0.2.99/decision_v0.2.99_e08_post_studio_controls.json`
- Status: `completed`
- Decision: `select_operator_runbook_lifecycle`
- Selected candidate: `operator_runbook_lifecycle`
- Next version: `v0.2.100_e08_operator_runbook_lifecycle`
- First design: `docs/current-design/design_v0_2_100_e08_operator_runbook_lifecycle.md`
- E07 invariant: `preserved`
- Reason: After API and Studio controls, the next highest-value bounded step is an operator runbook lifecycle.

## Ranked Candidates

- `operator_runbook_lifecycle` score `95`; status `candidate`; disposition: select because backend API and Studio surface now need operational procedure, rollback, and escalation guidance
- `pause_e08_after_studio_controls` score `85`; status `candidate`; disposition: reject for now because operator runbook is the natural closure after an operator surface
- `broader_sidecar_boundary_closure` score `35`; status `candidate`; disposition: defer because full boundary closure remains broader than the immediate post-Studio slice

## v0.2.100 Verification Targets

- operator runbook document or product surface under docs/current-design then archived
- runbook checklist covering before-change, apply-change, rollback, and incident escalation
- linkage to backend PATCH API and Studio editable controls evidence
- stage report template validation and active directory cleanup
