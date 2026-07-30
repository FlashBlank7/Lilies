# EXP-LILIES-001 revision 27 platform supplementation

Status: developer supplementation verified; final Builder rerun and hidden-seed
acceptance remain pending.

This record distinguishes reusable platform assets from project configuration.
None of the assets below contains Paperless or InvenTree field mappings, fixture
answers, or a final workflow graph.

## Reusable assets discovered by the enterprise experiment

| Enterprise failure observed | Reusable platform asset | Builder-fillable boundary | Cross-domain evidence |
| --- | --- | --- | --- |
| Connector list responses can be arrays, objects, or paginated envelopes, so downstream iteration received the wrong shape. | `record_collection_normalize` converts a configured envelope into a bounded record array and reports the selected source shape. | Builder selects an explicit envelope path when the registered connector does not use a known shape. | Non-source CRM-style array, object, and envelope fixtures. |
| A required reference such as the first item of an empty result failed with an opaque index error. | Required reference errors identify the node, full path, failed segment, container type, and observed list length. | Builder can change the mapping, add an empty branch, or change the cardinality contract using the diagnostic. | Exact-cardinality reference regression. |
| Public Builder tests could not drive a `human_input` node without using the reserved runtime key `__human__`. | Acceptance cases expose test-only `simulated_human_inputs`; ordinary public run inputs still reject reserved keys. | Builder supplies typed review decisions per test and the production workflow still pauses for a real reviewer. | Generic high-risk reconciliation flow with a Boolean review decision. |
| A structural smoke test could claim H3 evidence while omitting review, runtime branches, candidate data, and required capability coverage. | Governed preflight checks mandatory capability coverage, evidence target, real tool evidence for external mutation, review carriers, connected decision branches, runtime-backed conditions, and non-empty match sources. | Builder writes the business scenarios and expected outcomes; the platform validates their evidence level and carrier coverage. | Structural-shortcut and disconnected-branch negative fixtures plus a valid generic high-risk flow. |
| A previously successful low-standard test remained publishable after the platform gained stronger acceptance rules. | Test evidence is bound to a validation-contract digest. A rule upgrade makes legacy evidence stale and the governed publish endpoint returns 409 until tests are rerun. | Builder reruns or replaces acceptance scenarios under the current rules. | Legacy-evidence migration and republish regression. |
| External systems do not share fields, pagination, authorization, idempotency, or compensation semantics. | Public orchestration manuals expose connector, mapping, review, writeback, and acceptance templates instead of provider-specific code. | Builder fills registered connector IDs, schema-backed fields, stable business keys, approval policy, compensation behavior, and expected scenario outputs from the customer requirement package. | Public template-neutrality regression rejects Paperless/InvenTree names. |

## Connector configuration correction

The Builder report `238afc98-13ae-4669-a109-b3782558f02b` correctly identified
that two task-authorized write operations were absent from the public tool
projection. The owner used the platform's generic OpenAPI generation,
operation-contract overlay, deployment binding, and task-scoped projection
surfaces to register the missing operations. No provider-specific source module
or project-only block was introduced.

The live generation/contract records are stored under the formal state root:

- `evidence/p1-connector-activation.json`
- `evidence/p1-proxy-generation-registration.json`
- `evidence/p1-proxy-generation-contract-registration.json`

The registered generations passed their generated positive and negative
connector contracts before activation. This is configuration evidence, not a
claim that the complete Project 1 workflow passes.

## Verification completed before the final Builder rerun

- 192 relevant platform, public-contract, workflow, connector, block-discovery,
  and edge-contract tests passed after the bundled supplementation.
- 93 focused governed-evidence, publication-lifecycle, and workflow tests
  passed after validation-contract evidence binding was added.
- The live revision-48 counterexample now fails preflight with the complete
  missing-capability/evidence/review/branch list.
- The same counterexample's old structural test is `stale`; a republish attempt
  returns HTTP 409.

## Claim ceiling

The reusable platform assets and deterministic regressions above are complete.
Project 1 is not complete until a same-thread Builder authors a business-valid
workflow through public APIs, the debug acceptance passes, and all three hidden
seeds pass. The historical active version 1 is retained only as an immutable
counterexample and is not accepted as a customer result.

## Builder transport attempts and accountable retry control

Invocation 5 resumed the exact Builder thread after a clean enterprise-system
reset and successful read-only checks of both registered connectors. The Codex
transport fell back from WebSockets to HTTPS and then failed because the remote
`chatgpt.com` response stream was reset. It made no draft or collaboration
mutation and returned no usage fields. Monitoring therefore records one
unknown-usage model call rather than zero tokens.

The runner originally allowed another resume only after a terminal usage
receipt or a cryptographically verified pre-provider failure. The experiment
therefore exposed a general control-plane gap for the third real outcome:
the provider process started, the transport ended without a terminal receipt,
and an authorized owner deliberately accepts the risk of duplicate token
charges or provider-side execution. The runner now provides an explicit,
one-process `--authorize-indeterminate-provider-retry` control. It requires
the resume flag and the separate external-token-spend acknowledgement, verifies
the persisted result/transcript/stderr evidence digests, writes immutable
authorization evidence, and preserves unknown usage rather than coercing it to
zero. Without this flag the fail-closed behavior is unchanged. The runner
evidence suite passes 34/34 tests and the token-monitoring suite passes 49/49
tests after this supplementation.

Invocation 6 used that control and resumed the same assignment, session,
channel, application, and Codex thread. It again fell back from WebSockets to
HTTPS and failed with a connection reset at
`https://chatgpt.com/backend-api/codex/responses`. The invocation was durably
accounted as the sixth model call with unreported token usage. It produced no
new draft revision, workflow run, or collaboration message: the application
remained at draft revision 48 with 13 historical runs and the channel remained
at four messages.

At the time of invocation 6, the official OpenAI status page reported an active
“Elevated errors affecting ChatGPT conversations” incident in
Monitoring/Degraded performance and stated that intermittent continuation
failures could persist:
https://status.openai.com/incidents/01KYDN6YPS6ARY1EC9089N089G

The repeated external blocker is therefore recorded at transport evidence
level. No unchanged seventh retry is authorized. The recheck trigger is an
official incident recovery or another material provider/network state change.
Neither transport attempt is a Project 1 acceptance result.
