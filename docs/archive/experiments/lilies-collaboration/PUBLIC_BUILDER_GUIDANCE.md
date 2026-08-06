# Public Builder Operating Guide

Version: 1.0

Use this card for any public-API workflow assignment. It describes operating discipline, not a finished workflow or a scenario-specific solution.

1. Establish the public trust boundary first. Read the handoff once, resolve `workspace.path`, then open `.lilies-mount-manifest.json`. Verify every declared relative path, byte size, and SHA-256 digest before using a file. Stop on a mismatch or an undeclared file.
2. Discover interfaces instead of guessing. Fetch the current public platform contract and connector descriptors, then use the exact exposed connector identifier, version, operation, and request schema.
3. Plan the whole business flow before editing. Define decisions, human stops, external mutations, artifacts, and failure exits, then statically audit every node, branch, port, reference, and terminal path. Apply coherent changes rather than repeatedly patching isolated symptoms.
4. Treat `[REDACTED]` inspection values as display-only placeholders. Never persist them into a draft, request, test, or report, and never use them to replace an existing secret reference or configuration value.
5. Make tests independent, order-safe, and safe to run concurrently. Each test must own its fixture and deterministic mutation identity; no test may depend on another test's side effect or execution order.
6. Treat live test mutations as real side effects. The task author owns cleanup through authorized public controls after evidence capture. Confirm the exact cleanup receipts and restore a measured zero baseline before an acceptance run.
7. Budget for the final run before live testing. Write budgets and idempotency ledgers persist after compensation or deletion, so cleanup does not refund them. Reserve enough mutation capacity for the complete acceptance run.
8. Bind idempotency to the exact canonical mutation identity. Include the connector identifier and version, operation, target, and a digest of the canonical business-impacting payload. An exact replay reuses the same key; a different payload uses a different deterministic key. Never randomize keys or erase a ledger to bypass a conflict.
9. Classify connector conflicts separately from permission denials. Preserve the public error category and receipt; do not relabel an idempotency or payload-binding conflict as an authorization failure.
10. Decode spreadsheet dates semantically. A styled numeric cell may be an Excel serial date, so honor the workbook date system and cell style before normalization instead of treating the value as an ordinary number.
11. Prove retry success with a receipt. Any result claimed as successful after a retry requires an applied connector receipt whose `attempt_count` is greater than one; a decision label, message, or final success alone is insufficient.
12. Stop on a new error category and diagnose the whole boundary. Preserve the public error, trace, and receipts, then inspect the workflow, contract, and side-effect path together before choosing one coherent repair. Do not repeat speculative edits. If the evidence proves a reusable platform gap, submit the public gap report and end the Builder attempt.
