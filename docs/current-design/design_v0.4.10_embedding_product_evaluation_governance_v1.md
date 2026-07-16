# v0.4.10 Embedding product, evaluation, and governance vertical

Status: active
Source task: `V04-10-T01`
Mandatory tasks: `V04-10-T01E`, `V04-10-T01F`, `V04-10-T01G`
Source intents: `ARCH-008`, `EVAL-002`, `EVAL-004`, `GOV-004`, `SCENARIO-003`

## Editable scenario and runtime

`customer_system_embedding` is a first-class scenario and workflow template. Its graph visibly receives a tenant-scoped request, reads context through a Connector action, makes a decision, performs a governed writeback through another Connector action, and returns a customer-readable receipt. The writeback block references the Connector contract; it does not carry customer endpoint, credential, or policy internals.

Engineer Studio receives an Integrations workspace for manifest/profile status, tenant bindings, domain policy and emergency stop, scoped authorizations, execution receipts, callbacks, compensation, and evidence boundaries. Customer Runtime offers a bounded test-tenant request launcher and result view; it shows tenant-safe business status, external reference, writeback/compensation state, and next action without exposing secrets, raw signatures, policy grant internals, adapter request headers, or other tenants.

## Evaluation and Governance

The scenario binds identity, schema, isolation, idempotency, writeback, compensation, callback, deployment, and audit capabilities to the actual Connector service and visible workflow carriers. H1 proves editable structure. H3 uses a controlled customer HTTP fixture, signed ingress, real schema validation, one governed writeback, callback, compensation, restart reconstruction, and bounded product surfaces. H4 remains blocked until an explicitly configured customer test/live environment is eligible; H5 requires production telemetry, incidents, SLO, rollback exercises, and organization evidence.

Governance can filter policies and executions by connector, tenant, operation, status, and emergency state, then inspect the tenant-redacted audit chain, authorization decision, receipt, callbacks, compensation, and exercise. Capability Evidence Registry entries cap Connector SDK and high-risk governance at local H3.

## Closure

The version closes only after controlled fixture, API, workflow, evaluation, Governance, frontend build, desktop/mobile browser, current gate, evolution control, full-history classification, and report-intent terminal validation all pass. The final report audit must find no non-terminal report intent; unavailable H4/H5 evidence remains explicit debt rather than a reason to falsify completion.
