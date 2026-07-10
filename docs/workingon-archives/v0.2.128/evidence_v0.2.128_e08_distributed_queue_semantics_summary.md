# v0.2.128 E08 distributed queue semantics

- Raw evidence: `docs/workingon-archives/v0.2.128/evidence_v0.2.128_e08_distributed_queue_semantics.json`
- Status: `completed`
- Claim-next single owner: `True`
- Expired lease requeue: `True`
- Runner uses queue claim-next: `True`
- API snapshot available: `True`
- API requeue available: `True`
- External process manager claimed: `False`
- External KMS provider integration claimed: `False`
- E08 full sidecar completion claimed: `False`
- Next boundary: Distributed queue semantics now have storage-backed claim-next and requeue behavior. External process management, external KMS provider integration, and full sidecar completion remain open.

## API Surface

- `GET /api/v1/platform/harness/queue-semantics`
- `POST /api/v1/platform/harness/queue/requeue-expired`
