# v0.4.9 Daily collection provenance and access boundary

Status: active
Source task: `V04-09-T01`
Mandatory tasks: `V04-09-T01C`, `V04-09-T01E`
Source intents: `ARCH-007`, `EVAL-003`, `EVAL-004`, `SCENARIO-002`

## Editable scenario

`daily_web_collection` becomes a first-class scenario and workflow template. Its visible graph contains a durable daily schedule, an approved-source collection block, a deterministic digest transform, provenance recording, and a customer result. Applying the scenario writes its Capability Build Contract, workflow, and capability-scoped acceptance cases through the existing guarded draft operation; the result remains ordinary editable Lilies content.

## Approved-source collector

The collector accepts typed source entries and an explicit host allowlist. Each source carries a permission basis. Requests enforce Platform Harness egress policy, scheme/host validation, robots policy, timeout, response-size limit, redirect visibility, and sequential bounded access. A local controlled HTTP fixture is the H3 contract environment; the product does not infer legal permission from reachability or an allowlist.

Each source produces a durable receipt containing job, run, application, requested and final URL, canonical URL, host, collected time, permission basis, robots result, HTTP status, content type/size, normalized content hash, status, prior receipt linkage, and transformation summary. Status distinguishes new, changed, unchanged, resumed, denied, oversized, and failed results. Job-local receipt uniqueness plus cross-day canonical URL/content hashes prevents duplicate processing while retaining evidence that the source was checked again.

The collector checkpoints after each source. A recovered attempt reconstructs completed source work from receipts and skips it without another request. Digest output includes counts, changed/new items, skipped/denied/failed sources, citations, and excluded delivery claims.

## Evaluation boundary

Generated Evaluation Harness cases must cover schedule identity, retry/recovery, checkpoint and dedupe, provenance completeness, source access denial, storage, and notification. H1 proves graph and contract shape. H3 proves local services, controlled HTTP, restart, APIs, and customer output. H4 remains `blocked_by_environment` until an explicitly authorized live source and eligible evidence are configured; local HTTP success cannot support arbitrary-site or external-notification claims.
