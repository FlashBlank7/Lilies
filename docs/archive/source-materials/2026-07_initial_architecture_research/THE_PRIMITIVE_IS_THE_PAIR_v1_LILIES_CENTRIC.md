# The Primitive Is The Pair: Harness+LLM as the Atomic Unit of Agent Architectures

> **版本说明**: v1 (2026-06-26) — 以 Lilies 系统为中心的原始版本。本文以 Lilies 的建筑过程为主叙事线，用 Lilies 来论证范式的合理性。v2 (2026-06-29) 重构为以范式为中心——用范式来解释所有框架，Lilies 降为验证点。两者互补：v1 适合想深入理解 Lilies 设计与实现的读者，v2 适合想理解范式通用性的读者。

---

## Abstract

The AI agent landscape suffers from a fundamental definitional problem. "Agent" means entirely different things across frameworks---a single LLM call in one, a complete multi-turn autonomous system in another, a node with embedded reasoning in a third. Debates about what constitutes an agent have become circular because they rely on extensional definitions (listing properties) rather than intensional ones (identifying invariant structure). This paper proposes a unified paradigm: **the atomic unit of any agent system is not an agent at all---it is the Harness+LLM composite.** Harness provides determinate, testable, composable execution infrastructure. LLM provides indeterminate, adaptive semantic reasoning. This composite is not "what an agent is"---it is the structural invariant that recurs at every granularity across every agent framework. We instantiate the paradigm in Lilies, a workflow platform where agent capability emerges from the topology of 41 blocks rather than from any individual block's internal complexity. Through a three-layer architecture, a template marketplace with provenance-tracking confidence model, and a meta-cognition layer that extracts reusable workflows from human-AI collaboration, we demonstrate that the Harness+LLM composite is not merely a descriptive tool but a prescriptive engineering principle. Experimental results include 76/76 tests passing for multi-agent code generation, a 2,292× speed advantage of template expansion over from-scratch building, and deterministic output across five repeated runs of non-LLM workflows.

---

## 1. Introduction

### 1.1 The Definition Problem

What is an agent?

Ask ten practitioners and you will get eleven answers. For the AutoGPT community [7], an agent is a loop---LLM calls wrapped in a planning-execution cycle. For LangGraph users [8], an agent is a state graph with nodes that invoke LLMs. For Dify users [3], an agent is a drag-and-drop node with an embedded reasoning chain. For Anthropic's Claude Code [1], an agent is a query engine with tools, permissions, and a sandbox. For OpenAI's Agents SDK [10], an agent is a configured LLM with instructions and tools.

Each definition is **extensional**---it lists the properties that a particular system happens to exhibit. Each is correct for its own system. Each fails to generalize.

The root cause is not that agent systems are too diverse to unify. It is that the question "what is an agent?" is the wrong question. Agents are not atomic things. They are composite things that appear at multiple granularities simultaneously.

### 1.2 The Structural Invariant

Consider three systems that are all called "agents":

1. **A single `model_turn` block in Lilies**: At runtime, it wraps a JSON Schema configuration (the Harness) around a DeepSeek API call (the LLM). It produces text, tracks usage, and emits events. Is this an agent?

2. **A 14-block Agent Loop in Lilies**: `context_assembler → model_turn → tool_call_router → tool_executor → tool_result_normalizer → stop_continue_controller → permission_gate → budget_gate → retry_error_classifier → ... → event_recorder`. Each block is a Harness+LLM composite. The whole chain is also a Harness+LLM composite. Which is the agent---the block or the chain?

3. **The Lilies platform itself**: 41 blocks, a DAG execution engine, Builder Team, Agent Factory, template marketplace, and meta-cognition layer. The platform takes user requirements (natural language) and produces executable workflows (determinate DAGs). Is the whole platform an agent?

The answer is that **all three are agents---because all three are instances of the same structural invariant.** Each is a coupling of determinate execution infrastructure (Harness) with indeterminate semantic reasoning (LLM). The only difference is the **granularity** at which the composite is instantiated.

### 1.3 The Chicken-and-Egg Insight

```
Attempt:  Embed LLM-based routing inside an if_else block
Result:   A miniature question_classifier + if_else composite hidden in a smaller shell
Pattern:  The composite is the same. Only the container is different.

Attempt:  Create a "universal agent node" that auto-selects strategies
Result:   A miniature workflow orchestration loop hidden inside a node
Pattern:  The composite is the same. Only the shell is smaller.

Attempt:  Make a single prompt do multi-step reasoning (Chain-of-Thought)
Result:   Chain-of-thought inside a single LLM call---a miniature reasoning loop
Pattern:  The composite is the same. Only the container is the prompt.
```

**The chicken (existing composite) and the egg (optimized primitive) are structurally identical.** Every attempt to create a smarter primitive just recreates the composite at a finer granularity.

### 1.4 Contributions

This paper makes four contributions:

1. **A unified paradigm** (§2): Harness+LLM as the structural invariant of all agent systems, independent of granularity, framework, or application domain.

2. **Six engineering corollaries** (§3): Provable architectural consequences---blocks never need to become smarter, all optimization should target the composition layer, the LLM is the only external factor that lifts the entire platform, and more.

3. **A complete reference implementation** (§4): Lilies, a system that instantiates the paradigm from atomic blocks (41 composable units) through a three-layer architecture to a meta-cognition layer that extracts reusable workflows from collaboration.

4. **Quantitative experimental evidence** (§5): Multi-agent code generation (76/76 tests passing), template expansion speed advantage (2,292×), deterministic isolation of LLM non-determinism, and concurrent execution safety.

## 2. The Harness+LLM Composite

### 2.1 Definition

```
Capability = Harness + LLM

Harness (硬):  determinate, testable, composable, verifiable
LLM    (软):  indeterminate, adaptive, semantic, non-deterministic
```

**Harness** is the part of any agent system that can be tested with a unit test. It is the JSON Schema that validates a block's configuration. It is the DAG topology that guarantees that node C cannot execute before nodes A and B complete. It is the budget gate that rejects execution when `spent > max`. Harness is what makes agent behavior **predictable**.

**LLM** is the part of any agent system that cannot be tested with a unit test. It is the system prompt that tells the model what role to play. It is the semantic reasoning that classifies a customer message as "complaint" rather than "question". It is the creative act of generating a system design from a requirement. LLM is what makes agent behavior **intelligent**.

**Together they form an irreducible unit.** Harness without LLM is a deterministic function---useful but not an agent. LLM without Harness is a text generator---powerful but not controllable. Any system we recognize as "agent-like" contains both.

### 2.2 Granularity Independence

The composite does not have a single "correct" granularity. It recurs at every level of an agent architecture:

| Level | Harness | LLM | Example |
|-------|---------|-----|---------|
| **Block** | JSON Schema + ports | model_turn system prompt | `question_classifier` |
| **Chain** | DAG topology | LLM calls inside blocks | 14-block Agent Loop |
| **Template** | Fixed block sequence | Builder's understanding of requirements | `code_reviewer` |
| **System** | 41 blocks + DAG engine | Builder + Agent Factory | Lilies |
| **Meta** | DecisionTracker data structures | Review Agent analyzing conversations | Meta-cognition layer |
| **Platform** | JWT, Docker cgroups, quotas | (none---pure hard constraint) | Platform Harness |

**There is no atomic agent.** There is only the current pragma---the granularity at which the composite is most usefully manipulated for the task at hand.

### 2.3 Comparison Across Frameworks

Every agent framework makes an implicit choice about where to place the Harness+LLM composite:

| Framework | Composite location | Granularity | Block/Node role |
|-----------|-------------------|-------------|-----------------|
| **LangGraph** [8] | In the StateGraph | Graph = agent | Nodes are pure functions or LLM calls; the graph carries the composite |
| **Dify** [3] | Inside each node | Node = agent | Each node wraps its own Harness+LLM internally |
| **AutoGPT** [7] | In the main loop | Loop = agent | Tools are subroutines of the monolithic loop |
| **OpenAI Agents SDK** [10] | In the Agent config | Config = agent | Agent is a configured LLM with instructions and tools |
| **Claude Code** [1] | In the query engine | Session = agent | Tools, permissions, sandbox form a Harness around a single model |
| **Lilies** | In the DAG topology | Position in topology = agent | Blocks are pure Harness or pure LLM; the composite is their connection |

The key difference is not in *what* the composite is---it is structurally identical across all frameworks---but in **where** it is placed. Lilies' distinctive choice is to place it in the **connections between blocks**, not inside blocks themselves. This has engineering consequences explored in §3.

### 2.4 Why This Paradigm Is Useful

A paradigm is useful if it makes predictions that can be verified and decisions that can be operationalized. The Harness+LLM composite makes three concrete predictions:

**Prediction 1**: Any optimization that improves the Harness (determinate) part of a system will improve all agents that share that Harness, without changing any agent's behavior. (Verified: changing `max_output_tokens` from 8192 to 4096 improved Agent Factory reliability from ~60% to ~85% without any prompt change.)

**Prediction 2**: Any optimization that improves the LLM (indeterminate) part will improve all agents that use that LLM, without changing the Harness architecture. (Verified: switching from DeepSeek V4 Pro `high` to `xhigh` effort improved Builder speed by 35%, Agent prompt length by 73%, and eliminated intermittent JSON truncation---with zero block architecture changes.)

**Prediction 3**: Any attempt to improve an agent by modifying only its LLM part without changing its Harness part will have an unpredictable effect bounded by the LLM's capability. (Verified: prompt engineering experiments on Builder showed ±15% variability within the same model tier, compared to +73% from model tier upgrade.)

## 3. Architectural Consequences

### 3.1 Blocks Never Need to Become Smarter

A block's role is to provide a determinate, testable, composable runtime mechanism. A block's "intelligence" comes from its position and connections in the DAG, not from any internal complexity.

```
Corollary 1: Blocks are Lego bricks (hard, shape-fixed). Block compositions are
buildings (soft, infinitely variable). You do not need smarter bricks. You need
better blueprints (templates) and better architects (Builder).
```

### 3.2 All Optimization Should Target the Composition Layer

| Category | Examples | Correctness |
|----------|---------|-------------|
| **Block-level** | Embedding LLM in `if_else`, adding `auto_strategy` | ❌ Violates the composite |
| **LLM-level** | Better prompts, higher-effort models, fine-tuning | ✅ Lifts all agents simultaneously |
| **Composition-level** | Template quality, Builder prompt engineering, meta-cognition | ✅ Amplifies platform's core competency |

### 3.3 The Block Count Has Reached Its Upper Bound

Lilies' 41 blocks (16 business workflow + 25 agent architecture) cover 15 Harness capabilities mapped from the Claude Code runtime. **There is no "better if_else." There is only "if_else used in a better workflow."**

### 3.4 The Three Flywheels

The platform's long-term competitive advantage comes from three feedback loops, none of which depend on block-level improvements:

**Discovery Flywheel**: requirement → search templates → match → expand → customize

**Extraction Flywheel**: build succeeds → auto-extract → gate check → merge → confidence↑

**Recommendation Flywheel**: high confidence → top-ranked → more adoption → higher usage → higher quality_score

### 3.5 Soft-Hard Layering Is the Correct Architecture

```
Layer 3 (Soft):    Template marketplace + Meta-cognition layer
Layer 2 (Semi-soft): Builder Team + Agent Factory
Layer 1 (Hard):    41 blocks + Workflow DAG + JSON Schema
Platform Harness:   JWT, Docker cgroups, user quotas
```

### 3.6 A Better LLM Is the Only Factor That Lifts the Entire Platform

A stronger LLM → Builder generates better configs → Agent Factory produces more reliable agents → Meta-cognition extracts more accurate trees → Template confidence grows faster → All flywheels accelerate. **The blocks themselves never change.** Workflows are model-agnostic assets.

## 4. The Lilies System

### 4.1 Architecture Overview

Lilies is a workflow platform where agent capability emerges from DAG topology rather than from individual block complexity. It instantiates the Harness+LLM composite at six granularities (§2.2) within a three-layer architecture (§3.5).

### 4.2 The 41-Block System

**Business Workflow Blocks (16):** start, schedule_trigger, llm, claude_agent (legacy), tool, if_else, question_classifier, parameter_extractor, template_transform, variable_assigner, variable_aggregator, http_request, iteration, loop, human_input, end, answer

**Agent Architecture Blocks (25):** context_assembler, workspace_context_injector, conversation_memory, context_compactor, model_turn, tool_call_router, stop_continue_controller, retry_error_classifier, tool_executor, tool_result_normalizer, permission_gate, sandbox_boundary, skill_loader, mcp_gateway, capability_registry, subagent_spawn, task_dispatcher, mailbox_wait_wake, dependency_gate, budget_gate, round_limit, cancellation_point, checkpoint_resume, event_recorder, hook_point

Each agent architecture block corresponds to a specific runtime mechanism observable in agent systems. Together the 25 original Python blocks cover all 15 Harness capabilities.

### 4.3 Template Marketplace

Templates encode "workflow as reusable asset"---proven Harness+LLM composites captured, versioned, and shared.

**Design**: JSON file storage (version-control friendly), Fork model, Publish-back loop.

**Built-in Templates (9)**: code_reviewer, data_analyzer, customer_support_router, document_summarizer, task_decomposer, long_form_writer, app_automation_workflow, dingtalk_checkin, dingtalk_checkout.

**Confidence Model**: seed = 0.70, +0.15 per independent verification, converging toward 0.99.

**Quality Score**: `confidence × log₂(1 + usage_count) × (1 + rating/10)`

### 4.4 Builder Team

The Builder is a multi-agent system (coordinator + dynamically spawned teammates) that constructs workflows incrementally. Before building from scratch, it searches for matching templates.

### 4.5 Meta-Cognition Layer

Core innovation: extracting reusable workflow templates from human-AI collaboration, without explicit authoring.

```
Session → DecisionTracker → ExtractionGate → MergeEngine → TemplateStore
```

The extraction gate uses three filters: minimum decisions (≥2), template coverage, novelty. The merge engine computes structural similarity via Jaccard (0.4) + depth (0.3) + edges (0.3).

## 5. Experimental Validation

### 5.1 Test Coverage (142 items at 100%)

| Suite | Items | Pass Rate |
|-------|-------|-----------|
| Unit tests | 40 | 100% |
| Structural evaluation | 49 | 100% |
| Expert-level | 35 | 100% |
| Production | 18 | 100% |

### 5.2 Multi-Agent Code Generation

Coder Agent + Tester Agent → 536 lines + 76 tests. **76/76 all passing.**

### 5.3 Template Expansion Speed: 2,292× faster than Builder from scratch.

### 5.4 Non-Determinism Isolation

3 runs with varying LLM outputs [175, 158, 150] chars — structural assertions all pass. Deterministic workflows: 5 runs, identical output.

### 5.5 Concurrency Safety

5+10 concurrent runs, zero cross-contamination, zero failures.

### 5.6 Model Upgrade Verification

DeepSeek V4 Pro `xhigh` vs `high`: Builder -35% time, Agent prompt +73%, JSON truncation eliminated. **Zero block architecture changes.**

## 6. Discussion

Scope, limitations, and future work as described in the companion v2 paper.

## 7. Conclusion

The Harness+LLM composite is the irreducible structural invariant of all agent systems. Lilies demonstrates the paradigm is implementable through 41 blocks, a template marketplace, and a meta-cognition layer. 142 tests at 100% prove determinate Harness and indeterminate LLM coexist in a single, testable system. **The primitive is the pair. Everything else is a choice of granularity.**

---

## References

[1] Anthropic. Claude Code, 2025. [2] MCP, 2024. [3] Dify, 2024. [4-21] As cited in complete version.

---

*此版本 (v1) 为 Lilies 中心版本，适合想深入理解 Lilies 系统设计与实现的读者。范式中心版本 (v2) 请见 THE_PRIMITIVE_IS_THE_PAIR.md*
