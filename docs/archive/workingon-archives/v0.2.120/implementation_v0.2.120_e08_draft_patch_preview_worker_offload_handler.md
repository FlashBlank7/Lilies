# v0.2.120 E08 draft_patch_preview worker offload handler implementation

## Source

- Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.119_e08_remaining_sidecar_slice_reselection.md`
- Source task: `Implement draft_patch_preview worker offload handler`
- Version: `v0.2.120_e08_draft_patch_preview_worker_offload_handler`

## Completed Implementation

- Added `draft_patch_preview_handler()` to `platform/backend/src/agent_platform/worker_runner.py`.
- Registered `draft_patch_preview` in `build_platform_worker_handlers()`.
- Moved `draft_patch_preview` from unavailable to implemented in the worker catalog.
- Preserved the deterministic, model-free `DraftPatchPreviewer.preview()` behavior.
- Preserved the direct API path `/api/v1/applications/{application_id}/draft/preview-patch`.

## Evidence

- Focused tests: `4 passed`
- Catalog/handler regression tests: `12 passed`
- Generated evidence: `docs/workingon/evidence_v0.2.120_e08_draft_patch_preview_worker_offload_handler_summary.md`

## Boundary Preserved

- Draft preview still returns operations only and does not mutate draft revision/content hash.
- Unsupported preview fails deterministically in the worker path.
- Remaining unavailable worker handlers are still `builder_build` and `benchmark`.
- Full Platform Harness sidecar completion is not claimed.

