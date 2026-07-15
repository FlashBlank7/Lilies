# Lilies Evolution Control

This directory is the durable control layer for the report-application campaign.

- `PROGRAM_CHARTER.md` freezes product intent, completion rules, and deviation authority.
- `report_intents.json` is the machine-readable coverage registry for the latest scenario/capability report.
- `stage-contracts/` stores immutable task/acceptance locks; each stage report records the lock fingerprint and its first Git baseline commit.
- Stage reports remain the only authority for selecting the next stage and version.
- Current designs expand tasks already accepted by a stage report.
- Workingon files contain intermediate results and evidence only.

The charter and intent registry constrain and audit stage reports. They do not independently select the next task.
