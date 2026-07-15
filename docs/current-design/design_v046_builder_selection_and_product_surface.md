# v0.4.6 Builder Selection And Product Surface

Status: active

Tasks: `V04-06-T01D`, `V04-06-T01E`

## Builder Decision

Builder Team receives module suggestions with exact `module:<id>@<version>` references. Compatibility is calculated from the current Capability Build Contract, not only requirement keywords:

- required capability IDs covered and missing;
- module-only capabilities disclosed;
- required and supported execution envelope;
- dependency and external-contract gaps;
- strongest evidence status and claim scope;
- known boundaries that affect the requested scenario.

Only a verified, envelope-compatible exact version enters the reusable-module resource inventory. Legacy or draft versions can be inspected or manually expanded, but their names, ratings, confidence, and node count do not satisfy strict capability closure.

## Studio Surface

Engineer Studio shows module identity and version, lifecycle status, capability coverage, input/output ports, envelope, evidence level, and known boundaries before expansion. The insert action always sends an exact version. Verification failures explain the missing evidence or incompatible capability instead of degrading into a generic template error.

## Reference Module

The existing Codex-like workspace-agent template becomes the first built-in verified reference module only after its contract is tied to existing implementation and executable tests. Its claim remains bounded to deterministic local component behavior; provider availability, arbitrary repositories, and production reliability are excluded.
