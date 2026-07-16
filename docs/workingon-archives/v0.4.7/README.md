# v0.4.7 Working Evidence Archive

This directory preserves the closure evidence for the independent Customer Runtime, Engineer Studio, and Governance Console vertical implemented in commit `48d2482012530f2731354fde8d37a33b1e583c7c`.

## Evidence Boundary

- `browser/browser-evidence.json` records six desktop/mobile surface checks, a real Customer Runtime start-to-result journey, Markdown structure, legacy route handoff, 100-row governance pagination, and an 11-span trace drill-down.
- `browser/` contains the final acceptance screenshots. `browser/initial/` is retained only as intermediate diagnostic evidence from before the serialized-Markdown display correction; it is not the acceptance image set.
- `full-suite-classification.json` proves the final full-history run contained 701 passes, 85 exact strict historical conflicts, and no unknown or current failure.
- The strongest claim is local H3 integration across durable storage, authenticated APIs, three product routes, and deterministic browser journeys. Production tenant isolation, provider billing reconciliation, production SLOs, and paging remain outside this archive.

## Final Artifact Digests

| Artifact | SHA-256 |
| --- | --- |
| `browser/browser-evidence.json` | `8c4ce4275a39a3227b2dd774302f8d7a5352b69fd3bfd143aadabd8854ef6995` |
| `browser/customer-runtime-desktop.png` | `329f4dce1b184dbf64e133a44ae881c9cc6638e6733a5c33feea3f1bbc14edc6` |
| `browser/customer-runtime-mobile.png` | `73e04b2d21430f67ce6c0528f1a7ab6f17cb1d00db58880cbe62ad364a580cd1` |
| `browser/customer-runtime-mobile-result.png` | `b257d69f7f3cfa4a54a4adfa133433b1d9e01867fbe588d64d3e078af1f6d947` |
| `browser/engineer-studio-desktop.png` | `65487b2c993def75d526ea004db56daf844d973ca5c677d56e8a784f651e444b` |
| `browser/engineer-studio-mobile.png` | `c6fb4d1f623124a1c318bbb022f5b887fd35ae0dec61263ee11495f08fac8b76` |
| `browser/governance-desktop.png` | `978dd3a879cb017549182bc03790351b45a154b4e1c4dcdeae15232653ed8b66` |
| `browser/governance-mobile.png` | `b8d20ba1386a3b048e98bcb9ca6e13bb7693927ff901f6f9e443193223e0e813` |
| `browser/governance-pagination-desktop.png` | `e2a37a99e48563ea8bec10f92c528507936a6e27ad9874597fe88ca6d9964c92` |
| `browser/governance-trace-desktop.png` | `ec4b8ecdff780b2c4b5cdb40217f82e0fb159fa1a5761a3500823d9e91aeb3b4` |
| `full-suite-classification.json` | `4a1de35919ba0a0b06bd4583f279d16985f0ae70370c741d5770fa5635b8d00c` |

The authoritative closure, claim ceilings, evidence debt, and v0.4.8 handoff are recorded in `docs/stage-reports/v0.4.7_three_interface_governance_console.md`.
