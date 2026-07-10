# v0.2.130 E08 external process manager

- Raw evidence: `docs/workingon-archives/v0.2.130/evidence_v0.2.130_e08_external_process_manager.json`
- Status: `completed`
- Direct start/observe/stop: `True`
- Direct restart changes pid: `True`
- API start/observe/stop: `True`
- API restart changes pid: `True`
- Distributed queue semantics preserved: `True`
- External KMS provider integration claimed: `False`
- E08 full sidecar completion claimed: `False`
- Next boundary: External process manager now provides local subprocess start/observe/stop/restart. External KMS provider integration and full sidecar completion remain open.

## API Surface

- `GET /api/v1/platform/harness/worker-process-manager`
- `POST /api/v1/platform/harness/worker-process-manager/start`
- `POST /api/v1/platform/harness/worker-process-manager/stop`
- `POST /api/v1/platform/harness/worker-process-manager/restart`
