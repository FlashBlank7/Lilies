# v0.2.108 E08 secret KMS/rotation contract implementation

## Source

- Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.107_e08_remaining_sidecar_slice_reselection.md`
- Source task set: implement KMS/rotation-grade secret envelope contract; preserve existing secret evidence; preserve full-sidecar boundary; maintain executable verification discipline.

## Completed

- Added `secret-envelope:v2:` for newly saved encrypted Platform Harness secrets.
- Added explicit `key_id` to v2 envelopes and public redacted metadata.
- Added Platform Harness current key id plus previous-key keyring lookup for rotation-aware decrypt.
- Added Settings/API startup path for `platform_harness_secret_envelope_key_id` and `platform_harness_secret_envelope_previous_keys`.
- Preserved v1 envelope reads and legacy plaintext reads.
- Preserved public secret redaction and no plaintext-at-rest claim for encrypted rows.
- Updated policy controls to report the rotation contract without claiming external KMS integration.

## Boundary

- External KMS integration is not implemented.
- Full E08 Platform Harness sidecar completion is not claimed.
- Remaining sidecar slices still include complete handler catalog and distributed heartbeat registry.

## Verification

- `.venv/bin/python -m pytest tests/test_v02_108_e08_secret_kms_rotation_contract.py -q`
- `.venv/bin/python -m pytest tests/test_workflow.py -q -k 'secret_store or secret_envelope or policy_controls'`
- `.venv/bin/python scripts/v02_108_e08_secret_kms_rotation_contract.py --output-dir docs/workingon-archives/v0.2.108`
