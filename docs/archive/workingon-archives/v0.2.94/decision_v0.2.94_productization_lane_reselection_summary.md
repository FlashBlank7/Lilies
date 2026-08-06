# v0.2.94 productization lane reselection

- Raw evidence: `docs/workingon-archives/v0.2.94/decision_v0.2.94_productization_lane_reselection.json`
- Status: `completed`
- Decision: `select_e08_followup_controls`
- Selected lane: `e08_followup_controls`
- Next version: `v0.2.95_e08_followup_controls_scope`
- First design: `docs/current-design/design_v0_2_95_e08_followup_controls_scope.md`
- Reason: E07 is productized; E08 is the highest-priority unblocked remaining productization gap
- E07 invariant: `guarded_default_rollout_implemented`

## Ranked Candidates

- `e08_followup_controls` score `75`; blocked `False`; status `deferred_but_unblocked`
- `e05_scheduled_monitoring` score `55`; blocked `False`; status `completed_slice_optional_extension`
- `e10_governed_memory_surface` score `-100`; blocked `True`; status `blocked`
- `e02_true_human_panel` score `-100`; blocked `True`; status `blocked`
