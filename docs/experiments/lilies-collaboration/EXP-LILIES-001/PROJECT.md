# EXP-LILIES-001 — supplier-document reconciliation

- Portfolio position: 1 of 6
- Capability family: document/OCR, procurement matching, Excel, governed host writeback
- Hosts: Paperless-ngx and InvenTree
- Current package revision: 28
- Status: active under r7; no new LiliesAgent rerun exists and historical customer business acceptance remains `0/3`

Directories `1/` through `28/` are immutable revisions of this single project.
They remain in the original location so their digests, attempts, and failure
denominator are not rewritten by the portfolio correction.

Revision 21 preserves the revision-20 business requirement, fixtures, hidden
inputs, oracle, budget, hosts, and acceptance. It advances only the immutable
public-contract and assignment boundary after the revision-20 credential
deadline. Model egress stays disabled until a real run is explicitly
authorized and the remaining external Codex budget can support it.

Revision 22 preserves the same business requirement, fixtures, hidden oracle,
budget, hosts, failure denominator, and acceptance. It replaces the invalid
project-specific connector aliases with official OpenAPI `operationId` values,
adds supplier-part and document-metadata reads required for unambiguous
matching and duplicate evidence, and excludes InvenTree attachment/metadata
writes whose frozen official contracts do not describe a safe request. Those
operations remain explicit host-contract evidence debt; no adapter or guessed
payload was introduced. This revision also disables workflow model access:
the run consumes Paperless's existing OCR text and uses deterministic generic
blocks, so it cannot create provider-token spend.

Revision 23 preserves the revision-22 business goal, fixtures, hidden oracle,
budget, hosts, failure denominator, `model_access=false`, and acceptance.
Official InvenTree 1.4.2 source and serializer evidence now establishes
link-only attachment create/list/destroy and the exact metadata
retrieve/partial-update/full-update contracts. The live OpenAPI metadata
schema omission may be completed only through the generic operation-contract
overlay; it is not permission to add a host-specific adapter or mapping.
Attachment acceptance is capped at an external URI association and does not
claim binary document copying.

Revision 24 preserves every customer input, business rule, host operation,
budget, hidden seed and oracle while re-freezing the current generic
verification-policy source closure.

Revision 25 preserves those same customer and acceptance inputs, adds a
canonical public customer-requirement-package manifest over the existing
customer documents and system materials, and authorizes the generic
task-scoped exact single-use Connector write-authorization operation. It does
not add a project-specific adapter, mapping, wrapper or final workflow.

Revision 26 preserves the complete revision-25 business and verification
denominator. It binds the pre-assignment contract digest to the exact frozen
task action and Connector policy, and introduces a generic owner-setup-only
phase so official Connector generations, bindings and policies are complete
before the immutable Builder handoff is rendered. No host-specific business
logic or completed workflow is preloaded.

Revision 28 preserves the complete revision-27 business and verification
denominator. It records the user's authorization that a fresh external Codex
may be the isolated Builder when needed, while retaining the actual black-box
boundary: the Builder receives only the public customer requirement package,
filtered workspace, Builder manual and public platform contract, and uses only
Lilies platform public APIs and functions. The adjacent revision supplies a
fresh assignment, channel, credential and write-authorization budget after the
predecessor environment reset; it does not add a mapping, adapter, final graph,
hidden answer or customer-specific platform code.

Revision 27 preserves the revision-26 business and verification denominator
after the public Builder recorded a durable `task_spec_gap`. It clarifies that
the final workflow and workbook are Builder deliverables, not customer
prerequisites, and requires the Builder to derive a candidate mapping from
customer materials and official schemas before reporting only a concrete
unsafe ambiguity. It supplies no mapping, final graph or answer.
