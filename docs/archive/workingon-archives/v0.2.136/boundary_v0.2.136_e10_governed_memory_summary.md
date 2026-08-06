# v0.2.136 E10 governed memory boundary

- Raw evidence: `docs/workingon-archives/v0.2.136/boundary_v0.2.136_e10_governed_memory.json`
- Status: `completed`
- Accepted product scope: `True`
- Unrestricted memory allowed: `False`
- Filesystem wrapper allowed: `False`
- Missing controls: `0`
- Next version: `v0.2.137_e10_governed_memory_surface_contract`

## Controls

- `permission_scope`: Memory write/read requires explicit user or operator-scoped permission.
- `audit_log`: Every create/read/update/revoke/expire operation records actor, source, reason, and timestamp.
- `revoke`: A revoked memory item must be excluded from retrieval and retained only as redacted audit metadata.
- `retention_policy`: Every memory item has a retention class and expires unless explicitly renewed under policy.
- `source_attribution`: Every memory item stores source type, source id, captured_at, and evidence text/hash.
- `no_unrestricted_filesystem_memory`: The surface must not index arbitrary filesystem paths or background activity without scoped permission.
