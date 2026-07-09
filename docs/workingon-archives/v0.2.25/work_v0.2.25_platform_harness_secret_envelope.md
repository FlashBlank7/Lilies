# work_v0.2.25_platform_harness_secret_envelope

## 1. Goal

Implement the next automatic-evolution slice from `v0.2.24`: harden Platform Harness secret references so newly saved secrets are not persisted as plaintext.

This version is a local envelope-encryption slice. It does not claim external KMS completion. It must still support decrypting newly saved secret references during runtime injection and keep legacy plaintext rows readable for backward compatibility.

## 2. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.24_platform_harness_stdio_policy_controls.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Add KMS/envelope encryption or external secret-manager integration | accepted: local envelope encryption | `docs/current-design/design_platform_harness_secret_envelope_v1.md`; `docs/current-design/design_platform_harness_secret_envelope_runtime_v1.md`; `docs/current-design/design_platform_harness_secret_envelope_tests_v1.md` | Highest-risk remaining Platform Harness secret gap from v0.2.21. |
| External worker runner / durable execution queue | deferred | none | Separate durable worker version. |
| Formal experiment tranche E01/E02/E04/E05/E08 | deferred | none | Requires paid/live experiment designs and DOCX reports. |
| Browser visual QA | deferred | none | Separate UI QA stage. |
| Editable Platform Harness policy controls | deferred | none | Requires product decision on runtime policy editing. |
| Allowlist-grade stdio MCP sandbox firewalling | deferred | none | Requires deeper sandbox/firewall design. |

All next-stage tasks listed: yes.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Secret envelope persistence | `docs/current-design/design_platform_harness_secret_envelope_v1.md` | completed | Newly saved secrets are stored as authenticated encrypted envelopes, not plaintext. |
| Runtime injection and compatibility | `docs/current-design/design_platform_harness_secret_envelope_runtime_v1.md` | completed | Secret references decrypt correctly; legacy plaintext rows remain readable. |
| Regression tests and archive evidence | `docs/current-design/design_platform_harness_secret_envelope_tests_v1.md` | completed | Raw storage test, injection test, full pytest, and archive evidence pass. |

## 4. Acceptance Criteria

- A configurable `platform_harness_secret_envelope_key` exists.
- `build_services()` supplies an envelope key to `PlatformHarness`; local default may derive from API token when no explicit key is configured.
- `save_secret()` stores encrypted envelope text in `platform_secrets.value` when an envelope key exists.
- `inject_secret_references()` decrypts encrypted envelopes before injecting tool/runtime payloads.
- Existing legacy plaintext rows remain readable.
- Public secret API responses never expose secret values and show encrypted storage mode.
- Focused tests and full backend regression pass.

## 5. Evidence

Implementation files:

- `platform/backend/src/agent_platform/config.py`
- `platform/backend/src/agent_platform/api.py`
- `platform/backend/src/agent_platform/platform_harness.py`
- `tests/test_workflow.py`

Focused secret tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_secret_store_api_redacts_values tests/test_workflow.py::test_platform_harness_secret_store_uses_envelope_at_rest tests/test_workflow.py::test_platform_harness_secret_envelope_reads_legacy_plaintext_rows tests/test_workflow.py::test_platform_harness_secret_reference_injects_http_headers tests/test_workflow.py::test_platform_harness_missing_secret_reference_fails -q
```

Result:

- `5 passed, 1 warning`

Post-refactor focused tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_secret_store_uses_envelope_at_rest tests/test_workflow.py::test_platform_harness_secret_envelope_reads_legacy_plaintext_rows -q
```

Result:

- `2 passed, 1 warning`

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

- `79 passed, 1 warning`

Static checks:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
```

Result:

- passed.

Dependency check:

```bash
.venv/bin/python - <<'PY'
try:
    import cryptography
    print('cryptography available', cryptography.__version__)
except Exception as error:
    print('cryptography unavailable', type(error).__name__)
PY
```

Result:

- `cryptography unavailable ModuleNotFoundError`

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_platform_harness_secret_envelope_v1.md` | proceed to next design | Envelope save and public storage metadata implemented. | completed. |
| `design_platform_harness_secret_envelope_runtime_v1.md` | proceed to next design | Encrypted rows decrypt during injection and legacy plaintext remains readable. | completed. |
| `design_platform_harness_secret_envelope_tests_v1.md` | proceed to archive | Focused tests, full backend regression, and compileall passed. | completed. |

## 7. Review Before Archive

- Completion summary: completed local authenticated secret envelope storage for newly saved Platform Harness secrets.
- Engineering closure level claimed: platform boundary slice.
- Engineering closure actually achieved: at-rest non-plaintext storage, injection-time decryption, legacy compatibility, public redacted metadata, and regression tests.
- Remaining risk: no external KMS, no key rotation, no dependency-backed AEAD provider, no re-encryption migration for existing plaintext rows.
- Deferred tasks preserved: external worker runner, formal experiments, browser visual QA, editable policy controls, allowlist-grade stdio firewalling.
- Active current-design will be cleared after archive: yes.
- Active workingon will be cleared after archive: yes.
- Minor version target closure: completed as claimed.

## 8. Automatic Evolution

- Automatic Evolution Mode active: yes.
- Current version: `v0.2.25`.
- Archive automatically after verification: yes.
