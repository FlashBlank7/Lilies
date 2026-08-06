# v0.2.138 E10 Runtime Memory Retrieval Integration Implementation

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.137_e10_governed_memory_surface_contract.md`

## Completed

- Added opt-in runtime governed memory retrieval through `WorkflowRunRequest.inputs["__governed_memory__"]`.
- Injected retrieved active scoped records into `inputs["__governed_memory_context__"]`.
- Used `GovernedMemorySurface.read` so runtime retrieval writes read audit events.
- Excluded revoked and expired memory by reusing governed memory surface retrieval constraints.
- Preserved no-opt-in behavior: runtime does not retrieve ambient memory unless explicitly requested.
- Added focused runtime/API tests and evidence generator.

## Boundary

This version integrates governed memory with workflow runtime. It does not implement Studio/operator UI and does not claim global experiment completion.

## Verification

- `.venv/bin/python -m pytest tests/test_v02_138_e10_runtime_memory_retrieval_integration.py -q`
- `.venv/bin/python scripts/v02_138_e10_runtime_memory_retrieval_integration.py`
