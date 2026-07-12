# v0.2.117 E08 remaining sidecar slice reselection implementation

## Source

- Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.116_e08_workflow_run_worker_offload_handler.md`
- Source task: `Re-select remaining E08 sidecar slice`
- Version: `v0.2.117_e08_remaining_sidecar_slice_reselection`

## Completed

- Added deterministic selector `scripts/v02_117_e08_remaining_sidecar_slice_reselection.py`.
- Added focused tests in `tests/test_v02_117_e08_remaining_sidecar_slice_reselection.py`.
- Generated decision evidence selecting `test_suite_worker_offload_handler`.
- Preserved completed `workflow_run_worker_offload_handler`, `scheduler_trigger_worker_offload_handler`, and prior completed sidecar slices.
- Kept `e08_full_sidecar_completion_claimed=False`.

## Verification

- `.venv/bin/python -m pytest tests/test_v02_117_e08_remaining_sidecar_slice_reselection.py -q`
- `.venv/bin/python scripts/v02_117_e08_remaining_sidecar_slice_reselection.py`

## Boundary

This version selects the next implementation slice only. It does not implement `test_suite_worker_offload_handler` and does not claim full Platform Harness sidecar completion.
