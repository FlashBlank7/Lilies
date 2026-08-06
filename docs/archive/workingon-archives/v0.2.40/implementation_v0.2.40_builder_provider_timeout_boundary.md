# implementation_v0.2.40_builder_provider_timeout_boundary

## 1. Completion Time

2026-07-09 22:40 JST

## 2. Implemented Changes

### Runtime stream boundary

- Updated `platform/backend/src/agent_platform/runtime.py`.
- Added `timeout_seconds` to `AgentRuntime._collect_stream()`.
- Defaulted the timeout to `settings.deepseek_timeout_seconds`.
- Moved the prior stream parsing body into `_collect_stream_unbounded()`.
- Added `<event_prefix>.timeout` event on `asyncio.TimeoutError`.
- Added `<event_prefix>.failed` event on `ProviderError`.
- Timeout now raises retryable `ProviderError("model stream timed out after ...")`.

### Builder Harness metadata

- Updated `platform/backend/src/agent_platform/builder.py`.
- Imported `ProviderError`.
- Added `_failure_metadata(error)` to classify failures.
- Builder `needs_attention` failures now write structured failure metadata to:
  - Platform Harness task metadata;
  - `build.needs_attention` stream event.
- Provider failures are classified as `model_provider`.
- Timeout-like failures include `timeout_like=true`.

### Regression tests

- Updated `tests/test_runtime.py`.
- Added `SlowStreamProvider`.
- Added `test_collect_stream_timeout_emits_retryable_provider_error`.
- Updated `tests/test_workflow.py`.
- Added `TimeoutBuilderProvider`.
- Added `test_builder_records_provider_timeout_in_harness_metadata`.

## 3. Verification

Focused tests:

```bash
.venv/bin/python -m pytest tests/test_runtime.py::test_collect_stream_timeout_emits_retryable_provider_error tests/test_workflow.py::test_builder_records_provider_timeout_in_harness_metadata -q
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
101 passed, 1 warning
```

Compile check:

```bash
.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts
```

Result:

```text
compileall completed successfully
```

## 4. Paid / Live Validation Boundary

No new paid/live provider call was required for this stage.

Reason:

- v0.2.39 already produced paid/live evidence of a real DeepSeek timeout during E05 shallow reuse.
- v0.2.40 is a deterministic Platform Harness and runtime boundary fix for that observed failure.
- Forcing a real paid-provider timeout would be nondeterministic and would test network/provider instability rather than the code boundary.

The next E05 stage should reuse the v0.2.40 deterministic timeout boundary, then run a bounded paid/live validation of the template-reuse success condition.

## 5. Closure Assessment

Claimed closure level:

- backend slice;
- platform boundary slice.

Achieved:

- Runtime stream timeouts are bounded and observable.
- Provider failures are visible in stream events.
- Builder provider failures are recorded as failed Platform Harness tasks with structured metadata.
- Regression coverage exists for runtime and Builder/Harness behavior.

Carried forward:

- UI surfacing of structured timeout metadata.
- Post-timeout E05 paid/live success-condition validation.
- Broader task monitor product boundary work.
