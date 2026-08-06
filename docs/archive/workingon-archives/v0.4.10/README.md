# v0.4.10 Working Evidence Archive

This directory preserves the closure evidence for the governed Connector and customer-system embedding vertical implemented in commit `517be66b3ea6fed083ddce93d255e68a9e5d29e3`.

## Evidence Boundary

- `browser/browser-evidence.json` records a reproducible Engineer Studio controlled preview, Customer Runtime test-tenant dry-run, mobile receipt disclosure, and Governance Connector Operations journey.
- The browser fixture made one authenticated controlled read and zero writes or compensations. Secret injection is retained only as `bearer_injected: true`; no header or secret value is stored in this archive.
- Customer Runtime exposed only the business request, two customer-readable progress phases, and a redacted receipt reading `仅预演（未写入）` and `未产生`. It did not render the raw Connector result, signature, secret, policy internals, or engineering mapping steps.
- Governance showed three tenant-scoped receipts and explicit unsupported production support while excluding raw payload and secret values. Desktop and mobile checks reported no document/surface overflow, off-screen controls, console errors, or failed requests; one expected aborted Next.js navigation request is recorded separately.
- `full-suite-classification.json` proves the final repository diagnostic contained 728 passes, 85 exact archived-expectation conflicts, and no current, unknown, or missing failure.
- Focused controlled-HTTP and restart tests prove immutable contract versions, schema validation, signed identity, replay and cross-tenant denial, exact-payload preauthorization, emergency stop, durable idempotent receipts, failed-side-effect visibility, ordered callbacks, compensation, exercises, editable workflow execution, H1/H3 Evaluation, H4/H5 refusal, and tenant-safe Governance.
- The strongest stage claim is H3 controlled test-tenant integration. This archive does not prove a customer IdP, customer-live mutation, private deployment compliance, production SLO, distributed reliability, production incident response, H4, or H5.

## Final Artifact Digests

| Artifact | SHA-256 |
| --- | --- |
| `browser/browser-evidence.json` | `8f5ee517c2f8399f0ed732c9a588dbe65ee0ff03a1ad74844351aadc6d5d4800` |
| `browser/customer-runtime-receipt-desktop.png` | `b3a0b6e8ba17f49f47d0722b207f56da850496e7f7bb6fad81b2a1afdf285877` |
| `browser/customer-runtime-receipt-mobile.png` | `a132bd328d4aa12f0e3d41ab6eb7b99f19ccac4d53de7a60b1ecc8b1f4974db4` |
| `browser/engineer-integrations-desktop.png` | `09530e1a7c133fc63996392090a5525f8539b36b88bf9366c0a5d4081426989b` |
| `browser/governance-connectors-desktop.png` | `ef03a0cbc15d3ccffc45744a7c1ceaaa34f23e897117b078e1dc615b86e2bc96` |
| `full-suite-classification.json` | `610980f6c940988a62cc88ecb19b1e803119f36a552b6f6694bde2fa17981b05` |

The authoritative closure, claim ceilings, evidence debt, and terminal report-intent audit are recorded in `docs/stage-reports/v0.4.10_connector_embedding_governed_writeback.md`.
