# Lilies Evolution Control

This directory is the durable control layer for the product and report-application campaign.

- `../PRODUCT_NORTH_STAR.md` is the highest product authority and defines the target customer, real workflow families, and scenario gates.
- `PROGRAM_CHARTER.md` freezes the current product/report intent, completion rules, and deviation authority.
- `program_charter_lock.json` fingerprints that charter and is checked against the lock file's first Git commit before stage closure.
- `report_intents.json` is the machine-readable product and enabling-capability coverage registry.
- `stage-contracts/` stores immutable task/acceptance locks; each stage report records the lock fingerprint and its first Git baseline commit.
- Stage reports remain the only authority for selecting the next stage and version.
- Current designs expand tasks already accepted by a stage report.
- Workingon files contain intermediate results and evidence only.

The Product North Star, charter, and intent registry constrain and audit stage reports. They do not independently select the next task. The legacy capability-boundary report is an enabling-architecture source and cannot replace the traditional-enterprise product definition.
