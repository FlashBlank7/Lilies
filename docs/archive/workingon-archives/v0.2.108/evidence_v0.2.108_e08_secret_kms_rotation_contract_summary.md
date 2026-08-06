# v0.2.108 E08 secret KMS/rotation contract

- Raw evidence: `docs/workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract.json`
- Status: `completed`
- External KMS integrated: `False`
- E08 full sidecar completion claimed: `False`
- Next boundary: This closes the local KMS/rotation-grade envelope slice only; external KMS integration, complete handler catalog, distributed heartbeat registry, and full sidecar completion remain open.

## Checks

| Check | Result |
| --- | --- |
| `new_secret_uses_v2_key_id` | `True` |
| `rotated_v2_reads_with_previous_key` | `True` |
| `missing_previous_key_blocks_old_v2` | `True` |
| `legacy_v1_read_supported` | `True` |
| `legacy_plaintext_read_supported` | `True` |
| `secret_material_redacted_from_public_metadata` | `True` |
| `secret_material_not_plaintext_at_rest_for_encrypted_rows` | `True` |
| `policy_controls_report_rotation_contract` | `True` |

## Public Modes

| Secret class | Public storage mode |
| --- | --- |
| `rotated_old` | `encrypted_v2:kms-old` |
| `current_new` | `encrypted_v2:kms-new` |
| `legacy_v1` | `encrypted_v1` |
| `legacy_plaintext` | `legacy_plaintext` |

## Existing Evidence Preserved

- `docs/stage-report-archives/v0.2.x/v0.2.15_platform_harness_secret_policy.md`
- `docs/stage-report-archives/v0.2.x/v0.2.25_platform_harness_secret_envelope.md`

## Implementation Paths

- `platform/backend/src/agent_platform/config.py`
- `platform/backend/src/agent_platform/api.py`
- `platform/backend/src/agent_platform/platform_harness.py`
- `tests/test_v02_108_e08_secret_kms_rotation_contract.py`
- `tests/test_workflow.py`
