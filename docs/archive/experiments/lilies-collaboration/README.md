# Lilies real-project portfolio

This directory has two independent levels:

- `portfolio-v04-13-t01h.json` selects the six real customer projects.
- `EXP-LILIES-NNN/<revision>/` stores immutable task-package revisions for one
  project.

A project number is not a task-package revision. In particular,
`EXP-LILIES-001/1` through `EXP-LILIES-001/10` are ten iterations of the same
Paperless/InvenTree project, not ten projects.

Projects execute one at a time. A failed, budget-exhausted, or
environment-failed attempt remains in that project's denominator. Platform
capability work discovered during a project is recorded separately in
`platform-capability-gaps.json`; capability tests never count as customer
project success.

