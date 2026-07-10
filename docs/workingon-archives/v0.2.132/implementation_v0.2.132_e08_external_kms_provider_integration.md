# v0.2.132 E08 External KMS Provider Integration Implementation

## Source

- Source stage report: `docs/stage-reports/v0.2.131_e08_remaining_sidecar_architecture_reselection.md`
- Version: `v0.2.132_e08_external_kms_provider_integration`

## Completed Work

- Added `SecretKMSProvider` protocol and deterministic `LocalSecretKMSProvider`.
- Added v3 provider-backed secret envelopes.
- Wired KMS provider settings into API service construction.
- Exposed provider status in policy controls and public secret metadata.
- Preserved v2/v1/plaintext secret compatibility.
- Added focused tests and generated evidence.

## Boundaries

- External KMS provider integration is implemented through a provider contract.
- The first provider is local and deterministic for executable tests.
- Cloud provider deployment is not claimed.
- Full Platform Harness sidecar completion is not claimed.

## Verification

- `.venv/bin/python -m pytest tests/test_v02_132_e08_external_kms_provider_integration.py tests/test_v02_108_e08_secret_kms_rotation_contract.py -q`
- `.venv/bin/python scripts/v02_108_e08_secret_kms_rotation_contract.py --output-dir .tmp/v02_108_rerun_check`
- `.venv/bin/python scripts/v02_132_e08_external_kms_provider_integration.py`
