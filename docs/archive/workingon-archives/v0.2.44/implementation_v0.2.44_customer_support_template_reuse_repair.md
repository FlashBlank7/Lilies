# implementation_v0.2.44_customer_support_template_reuse_repair

## 1. Scope

This implementation closes the v0.2.44 deterministic repair stage after v0.2.43 exposed customer-support Template reuse problems.

It does not close original E05. It repairs the deterministic runner/reporting and Builder guardrails needed before a credible paid/live rerun.

## 2. Code Changes

Changed files:

- `scripts/e05_template_reuse_depth_experiment.py`
- `platform/backend/src/agent_platform/builder.py`
- `tests/test_e05_template_reuse_depth_experiment.py`
- `tests/test_workflow.py`

Implemented behavior:

- E05 arm results now include `benchmark_outcome`.
- Future E05 suite evaluation defaults `E05_BENCHMARK_MINIMUM_PASS_RATE` to `1.0`.
- Builder `template_expand` now returns:
  - `node_types`
  - `edge_count`
  - expanded-subgraph `validation`
  - current-draft `draft_validation`
  - marketplace `template_contract`
- Builder prompt now tells the team to preserve `template_contract.min_blocks_required`.
- Builder refuses to remove the last node of a type required by a mandatory test.
- Risky draft mutation results now include validation feedback.

## 3. Focused Verification

E05 reporting:

```bash
.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q
```

Result:

```text
7 passed, 1 warning
```

Combined v0.2.44 focused tests:

```bash
.venv/bin/python -m pytest \
  tests/test_workflow.py::test_builder_customer_support_template_expand_returns_contract_and_validation \
  tests/test_workflow.py::test_builder_refuses_to_remove_last_node_required_by_mandatory_test \
  tests/test_e05_template_reuse_depth_experiment.py -q
```

Result:

```text
9 passed, 1 warning
```

## 4. Regression

Full regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

```text
108 passed, 1 warning
```

Static compile:

```bash
.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts
```

Result: passed.

## 5. Closure Assessment

Completed:

- deterministic reporting fix;
- template expansion contract visibility;
- mutation guard against mandatory-test required-node drift;
- focused and full tests.

Not completed:

- paid/live rerun after the repair;
- global E05 closure;
- UI/API surface for Builder `max_elapsed_seconds`;
- broader template-customization policy across all templates.

Recommended next stage:

- Run bounded paid/live customer-support E05 rerun using the repaired runner and Builder guardrails, then decide whether further template-contract changes are needed.
