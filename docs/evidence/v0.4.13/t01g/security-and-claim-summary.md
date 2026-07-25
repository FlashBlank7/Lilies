# V04-13-T01G Security And Claim Summary

- Qualification is enabling-architecture evidence only:
  `enterprise_denominator=false`.
- Ordinary customer and formal Builder surfaces do not discover the
  collaborative-development API, tools, prompts, or grants.
- Development authority is assignment- and role-scoped across workspace
  paths, exact argv, network hosts, side effects, secret references, budgets,
  deadlines, and stop state. Autonomous dispatch does not mutate a frozen
  grant.
- Lilies and Codex receive independent workspaces. Review uses a separately
  prepared immutable review snapshot; a developer result cannot promote
  itself.
- External worker results cannot forge command receipts that were not metered
  by the fenced service. A rejected result releases the lease and returns the
  work item to `awaiting_dispatch`.
- Audit 1 exposed a SQLite sidecar permission-tightening race. Permission
  enforcement now opens database/WAL/SHM paths with `O_NOFOLLOW`, validates
  and tightens the opened descriptor with `fstat/fchmod`, and safely accepts a
  sidecar disappearing after the last connection closes. The formerly flaky
  atomic-budget regression passed 100 consecutive isolated iterations.
- The current Codex CLI probes `/etc/codex/requirements.toml`; the live
  Seatbelt profile permits only that exact path and its macOS symlink target,
  not `/etc` generally.
- The current Codex CLI may probe optional `oaiusercontent.com` and
  `ab.chatgpt.com` hosts. Those hosts remain outside the declared grant and
  were denied by the loopback CONNECT proxy with zero upstream, request, or
  response bytes. Only declared OpenAI/ChatGPT provider hosts were connected.
- A model-proposed replacement may cover one line or the containing function,
  but the trusted worker accepts it only when applying the replacement yields
  the exact expected file whose sole semantic text change is subtraction to
  addition.
- The bounded live handoff used real Codex subscription transport and a real
  DeepSeek Lilies reviewer. Both provider reservations and receipts are bound
  to the effective role authority and persisted budget ledger.
- The fixed Q01-Q28 bundle passed all 28 mandatory cases. Reconnect,
  idempotency, lease, and concurrency lanes each retained 100 actual
  iterations.
- Browser runtime discovery remained empty. Browser layout, accessibility,
  screenshot, console, and browser-network claims remain
  `blocked_by_environment`; deterministic, live HTTP, CLI, worker, real-model,
  and fault-injection claims are unaffected.
- This task does not prove Paperless/InvenTree enterprise success, 36/36
  hidden-oracle correctness, three stable enterprise seeds, T01I persona
  accessibility, or v0.4.13 release closure. Those remain T01H-I-J.
