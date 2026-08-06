# implementation_v0.2.42_builder_build_level_watchdog

## 1. Completion Time

2026-07-09 23:20 JST

## 2. Implemented Changes

### API and storage

- Added optional `max_elapsed_seconds` to `BuildRequest`.
- Added a compatible `builds.max_elapsed_seconds` SQLite migration.
- Updated `WorkflowStorage.create_build()` to insert explicit columns and persist the optional value.
- Passed the value through `/api/v1/applications/{application_id}/builds`.

### Builder runtime boundary

- Added `BuildDeadlineExceeded`.
- Builder now records `max_elapsed_seconds` in Platform Harness task metadata when configured.
- Builder emits:
  - `build.deadline.configured`
  - `build.deadline.exceeded`
- Builder wraps the top-level coordinator `_agent_loop()` in `asyncio.timeout()` when `max_elapsed_seconds` is set.
- Build-level timeout now becomes:
  - build status: `needs_attention`
  - Harness task status: `failed`
  - failure metadata: `failure.type=build_timeout`, `timeout_like=true`, `retryable=true`

### Regression tests

- Added `SlowBuilderProvider`.
- Added `test_builder_build_level_watchdog_records_harness_metadata`.
- Test distinguishes build-level timeout from provider stream timeout by setting provider stream timeout high and build-level deadline tiny.

## 3. Verification

Focused watchdog test:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_builder_build_level_watchdog_records_harness_metadata -q
```

Result:

```text
1 passed, 1 warning
```

Focused timeout distinction tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_builder_records_provider_timeout_in_harness_metadata tests/test_workflow.py::test_builder_build_level_watchdog_records_harness_metadata -q
```

Result:

```text
2 passed, 1 warning
```

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

```text
103 passed, 1 warning
```

Compile check:

```bash
.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts
```

Result:

```text
compileall completed successfully
```

## 4. Closure Assessment

Claimed closure level:

- backend slice;
- platform boundary slice.

Achieved:

- API can set an optional whole-build elapsed-time boundary.
- Storage persists the deadline without breaking old rows.
- Builder records deadline configuration and timeout in stream events.
- Platform Harness task metadata distinguishes `build_timeout` from `model_provider`.
- Deterministic tests cover both provider timeout and build-level timeout.

Carried forward:

- UI control/display for `max_elapsed_seconds`.
- A broader retry/backoff policy for timed-out builds.
- Applying build-level timeout settings to E05 follow-up paid runs.
- Extending the boundary to post-agent-loop validation if those paths become long-running.
