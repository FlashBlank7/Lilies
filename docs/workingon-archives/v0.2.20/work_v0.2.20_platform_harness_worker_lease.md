# work_v0.2.20_platform_harness_worker_lease

## 1. Goal

Implement the next Platform Harness hard-boundary slice from `v0.2.19`: worker lease and durable execution semantics.

This version must close a real backend/platform objective, not merely write another audit. The target is a durable worker-lease boundary for Platform Harness task records:

- task records can carry `worker_id`, `lease_expires_at`, and `lease_version`;
- a worker can claim, renew, and release a lease through Harness methods and API endpoints;
- expired leases can be reconciled into failed task records with explicit metadata and events;
- lease state is persisted and visible through existing task monitor surfaces;
- focused tests prove persistence, conflict rejection, expiry reconciliation, and API behavior.

This is not a full external distributed execution queue. It is the durable lease primitive needed before a later queue/worker refactor.

## 2. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.19_full_task_set_product_visibility.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Worker lease and durable execution semantics | accepted | `docs/current-design/design_platform_harness_worker_lease_record_v1.md`; `docs/current-design/design_platform_harness_worker_lease_api_v1.md`; `docs/current-design/design_platform_harness_worker_lease_visibility_tests_v1.md` | This is the first hard-boundary Platform Harness gap named by v0.2.19 and can be closed as a backend/API/visibility slice. |
| Secret reference injection | deferred | none | Depends on a separate secret-reference data model and runtime injection policy. |
| Stdio MCP sandbox/container egress | deferred | none | Requires process/container egress design after lease semantics are stable. |
| Formal experiment tranche E01/E02/E04/E05/E08 | deferred | none | Requires paid/live experiment design and DOCX reports; not mixed into this backend Harness stage. |
| Browser visual QA for new UI panels | deferred | none | This is a UI verification stage; v0.2.20 is backend Harness. |
| Platform Harness policy controls UI/API | deferred | none | Should follow after worker lease and policy model stabilization. |

All next-stage tasks listed: yes.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Lease record and reconciliation | `docs/current-design/design_platform_harness_worker_lease_record_v1.md` | completed | PlatformTaskRecord persists lease fields; expired leases reconcile deterministically. |
| Lease API | `docs/current-design/design_platform_harness_worker_lease_api_v1.md` | completed | Claim/renew/release/reconcile endpoints expose the lease boundary without broad source changes. |
| Visibility and tests | `docs/current-design/design_platform_harness_worker_lease_visibility_tests_v1.md` | completed | Tests and monitor typing prove the boundary is observable and durable. |

## 4. Acceptance Criteria

- Every accepted design is implemented or explicitly revised with evidence.
- Expired leases cannot be silently finished as successful work.
- A second worker cannot steal an unexpired lease.
- Expired lease reconciliation persists failure state and metadata across Harness instances.
- API endpoints expose claim, renew, release, and reconcile operations.
- Studio task record typing and monitor metadata can display worker lease state.
- Focused tests and full backend regression pass.

## 5. Evidence

Implementation files:

- `platform/backend/src/agent_platform/platform_harness.py`
- `platform/backend/src/agent_platform/storage.py`
- `platform/backend/src/agent_platform/config.py`
- `platform/backend/src/agent_platform/api.py`
- `platform/frontend/lib/platform.ts`
- `platform/frontend/lib/i18n.ts`
- `platform/frontend/app/applications/[id]/page.tsx`
- `tests/test_workflow.py`

Focused worker lease tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_worker_lease_conflicts_and_persists tests/test_workflow.py::test_platform_harness_reconciles_expired_worker_leases tests/test_workflow.py::test_platform_harness_worker_lease_api -q
```

Result:

- `3 passed, 1 warning`

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

- `67 passed, 1 warning`

Frontend typecheck:

```bash
PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint
```

Result:

- `tsc --noEmit` passed.

Frontend production build:

```bash
PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH" npm run build
```

Result:

- Next.js production build passed.

Static checks:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
git diff --check
```

Result:

- both passed.

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_platform_harness_worker_lease_record_v1.md` | proceed to next design | Lease fields, claim/renew/release methods, expiry reconciliation, late-success rejection, and persistence behavior implemented and tested. | completed. |
| `design_platform_harness_worker_lease_api_v1.md` | proceed to next design | Claim/renew/release/reconcile endpoints implemented and API test passed. | completed. |
| `design_platform_harness_worker_lease_visibility_tests_v1.md` | proceed to archive | Monitor typing/display and deterministic tests completed; full regression passed. | completed. |

## 7. Review Before Archive

- Completion summary: completed a backend/API/visibility Platform Harness worker lease slice.
- Engineering closure level claimed: platform boundary slice, not full durable execution queue.
- Engineering closure actually achieved: durable lease fields, conflict rejection, explicit API operations, expiry reconciliation, late-success rejection, event emission, persistence, and monitor visibility.
- Remaining risk: no external multi-process worker runner exists yet; lease renewal is exposed but not wired to a queue worker loop.
- Deferred tasks preserved: secret references, stdio sandbox egress, formal experiments, browser visual QA, policy controls UI/API.
- Active current-design will be cleared after archive: yes.
- Active workingon will be cleared after archive: yes.
- Minor version target closure: completed as claimed.

## 8. Automatic Evolution

- Automatic Evolution Mode active: yes.
- Current version: `v0.2.20`.
- Archive automatically after verification: yes.
- Next version selection source after archive: only the v0.2.20 stage report.
