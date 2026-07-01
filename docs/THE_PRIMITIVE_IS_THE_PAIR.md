# The Primitive Is The Pair: Harness+LLM as the Atomic Unit of Agent Architectures

**Jiang Zhijun** · June 2026

---

## Abstract

The AI agent landscape lacks a coherent intensional definition. "Agent" means different things across frameworks---a single LLM call, a multi-turn autonomous loop, a node with embedded reasoning. These debates are circular because they rely on extensional definitions (listing properties) rather than intensional ones (identifying invariant structure). This paper proposes a unifying paradigm: **the atomic unit of any agent system is the Harness+LLM composite.** Harness provides determinate, testable, composable execution infrastructure. LLM provides indeterminate, adaptive semantic reasoning. We prove that every "smarter block" optimization in every major agent framework is structurally identical to the composite it claims to improve upon---the chicken and the egg are the same thing at different granularities. We demonstrate this by mapping the composite across LangGraph, Dify, AutoGPT, Claude Code, OpenAI Agents SDK, and Lilies, showing that each framework's design choices reduce to a single decision: where to place the composite. The composite generates testable predictions, prescribes optimization directions, and clarifies why a better LLM lifts the entire platform without architectural change. A reference implementation in Lilies provides quantitative validation across a 142-item test suite.

---

## 1. Introduction

### 1.1 The Definition Problem

What is an agent?

Ask ten practitioners and you will get eleven answers [7, 8, 3, 1, 10]. Each definition is extensional---it lists properties that a particular system happens to exhibit. Each is correct for its system. Each fails to generalize.

The root cause is not that agent systems are too diverse. It is that **the question "what is an agent?" presumes a privileged granularity that does not exist.** Agent systems are not atomic. They are composite structures that appear simultaneously at multiple levels.

### 1.2 The Chicken-and-Egg Problem

Every attempt to make an agent framework "smarter" at the atomic level reproduces the same pattern:

```
Attempt:  Embed LLM-based routing inside an if_else block
Result:   A miniature question_classifier + if_else composite hidden in a smaller shell
Pattern:  The composite is the same. Only the container is different.

Attempt:  Create a "universal agent node" that auto-selects strategies
Result:   A miniature workflow orchestration loop hidden inside a node
Pattern:  The composite is the same. Only the shell is smaller.

Attempt:  Make a single prompt do multi-step reasoning
Result:   Chain-of-thought inside a single LLM call---a miniature reasoning loop
Pattern:  The composite is the same. Only the container is the prompt.
```

**The chicken (the existing composite) and the egg (the optimized atomic unit) are structurally identical.** Every attempt to create a smarter primitive just recreates the composite at a finer granularity. This is the central insight: the Harness+LLM composite is irreducible---it cannot be simplified away, only moved around.

### 1.3 Contributions

1. **A unified paradigm** (§2): Harness+LLM as the structural invariant of all agent systems.
2. **A chicken-and-egg proof** (§3): Any "smarter block" optimization recreates the composite at a different granularity.
3. **A cross-framework mapping** (§4): Six major frameworks analyzed through the composite lens.
4. **Engineering consequences** (§5): Predictions, prescriptions, and prohibitions derived from the paradigm.
5. **Quantitative validation** (§6): Reference implementation with 142-item test suite at 100%.

## 2. The Harness+LLM Composite

### 2.1 Definition

```
Capability = Harness (硬) + LLM (软)

Harness: determinate, testable, composable, verifiable
LLM:     indeterminate, adaptive, semantic, non-deterministic
```

**Harness** is the part that can be verified with a unit test. It is the JSON Schema validating a block's configuration. The DAG topology enforcing execution order. The budget gate rejecting execution when `spent > max`. Harness provides predictability.

**LLM** is the part that cannot be verified with a unit test. It is the system prompt defining a role. The semantic reasoning classifying intent. The creative act of generating design from requirements. LLM provides intelligence.

**Together they form an irreducible unit.** Harness without LLM is a deterministic function. LLM without Harness is a text generator. Any system we recognize as agent-like contains both. Neither alone is an agent. Neither can be reduced to the other.

### 2.2 Granularity Independence

The composite has no privileged granularity. It recurs at every level:

| Level | Harness | LLM | Example |
|-------|---------|-----|---------|
| Single block | JSON Schema + ports | model_turn system prompt | question_classifier |
| Block chain | DAG topology | LLM calls inside blocks | 14-block Agent Loop |
| Workflow template | Fixed block sequence | Builder's semantic understanding | code_reviewer |
| Entire platform | 41 blocks + DAG engine | Builder + Agent Factory | Lilies |
| Meta-cognition | DecisionTracker structures | Review Agent analysis | extraction pipeline |
| Platform boundary | JWT, Docker cgroups, quotas | (none—pure hard constraint) | Platform Harness |

**There is no atomic agent. There is only the current pragma---the granularity at which the composite is most usefully manipulated.**

## 3. The Chicken-and-Egg Proof

### 3.1 The Proof Structure

Consider any agent framework F with a set of atomic building blocks B. Suppose we attempt to improve F by creating a "smarter" block b' that internally uses LLM reasoning to make decisions previously made by composing simpler blocks.

**Claim**: b' is structurally identical to a sub-workflow of existing blocks from B, wrapped in a smaller container.

**Proof**:

1. Any agent decision that uses LLM reasoning requires: (a) a Harness to structure the input and validate the output, and (b) an LLM to perform the semantic reasoning.
2. These two components---the Harness+LLM composite---are necessary and sufficient for the decision.
3. Therefore b' must internally contain a Harness+LLM composite.
4. This internal composite is a miniature workflow at a finer granularity.
5. The existing blocks in B already express this composite at the framework's native granularity.
6. Therefore b' = a sub-workflow of B, hidden inside a single block.

**Conclusion**: Any attempt to make a block smarter by embedding LLM reasoning simply recreates the composite at a finer granularity. The composite is irreducible---it cannot be eliminated, only moved to a different level.

### 3.2 Concrete Instances

| Framework | "Smarter Block" Proposal | What It Actually Is |
|-----------|------------------------|---------------------|
| Lilies | `auto_strategy` field in if_else | question_classifier + if_else as a miniature workflow |
| Dify | "Smart Node" with internal routing | A Dify workflow hidden inside a single Dify node |
| LangGraph | Single node with chain-of-thought + tool calls | A LangGraph subgraph inside a node |
| AutoGPT | "Universal tool" that auto-selects sub-actions | The AutoGPT loop, recursively, inside a tool |
| OpenAI Agents SDK | Agent with "autonomous mode" | A handoff-based multi-agent system collapsed into one config |

**In every case: the egg becomes the chicken. The smarter primitive is just the composite at a finer grain.**

## 4. Cross-Framework Mapping

### 4.1 The Single Decision That Defines a Framework

Every agent framework makes exactly one architectural decision: **where to place the Harness+LLM composite.** All other design choices follow from this.

| Framework | Composite Placement | Granularity | Visibility of Composite |
|-----------|-------------------|-------------|------------------------|
| **LangGraph** [8] | In the StateGraph structure | Graph = agent | Visible in code, invisible at runtime |
| **Dify** [3] | Inside each node | Node = agent | Hidden within node boundaries |
| **AutoGPT** [7] | In the main execution loop | Loop = agent | Monolithic, cannot be decomposed |
| **Claude Code** [1] | In the query engine session | Session = agent | Visible in tool calls, invisible as structure |
| **OpenAI Agents SDK** [10] | In the Agent configuration | Config = agent | Visible in handoffs, not in topology |
| **Lilies** | In the connections between blocks | Position in topology = agent | Fully visible in DAG |

### 4.2 Framework Analysis Through the Composite Lens

**LangGraph**: The StateGraph is the Harness. LLM calls within nodes provide semantic reasoning. The composite is at the graph level---you see nodes and edges, but the Harness+LLM pairing is implicit in which nodes call LLMs.

**Dify**: Each node carries its own internal Harness (configuration form) + LLM (reasoning chain). The composite is at the node level---you can't see inside a node to understand its reasoning. Composability is limited to connecting pre-built nodes.

**AutoGPT**: The entire execution loop is one monolithic Harness+LLM composite. There is no decomposition, no versioning, no partial reuse. The loop is the agent.

**Claude Code**: The query engine wraps tools, permissions, and sandbox around a single model session. The composite is at the session level. A session cannot be a sub-component of another session.

**Lilies**: Blocks are either pure Harness (if_else, budget_gate) or pure LLM (model_turn, question_classifier). The composite emerges from their connection in the DAG. This makes the composite fully visible, composable, versionable, and testable at every granularity.

### 4.3 What the Mapping Reveals

1. **No framework eliminates the composite.** Every framework instantiates it. The only question is where.

2. **Framework limitations map directly to composite placement errors.** Dify's opacity comes from hiding the composite inside nodes. AutoGPT's rigidity comes from binding the composite to a single loop. Lilies' composability comes from placing the composite in connections, making it explicit.

3. **Framework comparisons reduce to granularity preferences.** Debating "LangGraph vs Dify" is debating "graph-level composite vs node-level composite." Neither is wrong. Each optimizes for different use cases.

## 5. Engineering Consequences

### 5.1 Predictions from the Paradigm

**Prediction 1 (Harness improvement)**: Improving the determinate part improves all agents sharing that Harness, without behavior change. Verified: `max_output_tokens` 8192→4096 improved Agent Factory reliability from ~60% to ~85%.

**Prediction 2 (LLM improvement)**: Improving the indeterminate part improves all agents using that LLM, without architectural change. Verified: DeepSeek V4 Pro `high`→`xhigh` improved Builder speed 35%, Agent prompt length 73%, eliminated JSON truncation.

**Prediction 3 (single-component optimization)**: Changing only the LLM part without changing the Harness produces unpredictable effects bounded by LLM capability. Verified: prompt engineering showed ±15% within-tier variance vs +73% from tier upgrade.

### 5.2 Prescriptions

**All optimization should target the composition layer**, not the block layer. There is no "better if_else"---only "if_else in a better workflow."

**Blocks should never be made smarter.** Their intelligence comes from their topological position, not internal complexity. Adding LLM to a block recreates the composite at a finer grain without eliminating the coarser one.

**Cross-layer optimization creates architectural debt.** Placing Layer 3 capability (semantic understanding of which block to use) inside Layer 1 (a block's internal logic) duplicates the composite unnecessarily.

### 5.3 The Three Flywheels

Platform value comes from three feedback loops, none block-dependent:

**Discovery Flywheel**: requirement → template search → match → expand → reuse

**Extraction Flywheel**: build succeeds → auto-extract → gate check → merge → confidence↑

**Recommendation Flywheel**: high confidence → top ranking → more adoption → higher usage → higher quality score

### 5.4 The LLM Lever

A better LLM lifts every composite at every granularity simultaneously, without changing any Harness. This is the only factor that improves the entire platform at once. Workflows are model-agnostic assets.

## 6. Validation

### 6.1 Reference Implementation

Lilies instantiates the composite at six granularities within a three-layer architecture. Its 41 blocks express agent capability through topology. Its template marketplace captures proven composites with provenance-tracking confidence. Its meta-cognition layer extracts new composites from human-AI collaboration.

### 6.2 Quantitative Results

| Suite | Items | Pass Rate |
|-------|-------|-----------|
| Unit tests | 40 | 100% |
| Structural evaluation | 49 | 100% |
| Expert-level tests | 35 | 100% |
| Production enhancements | 18 | 100% |
| **Total** | **142** | **100%** |

**Multi-agent code generation**: 536 lines of production code + 76 pytest tests. 76/76 passing.

**Template expansion speed**: 2,292× faster than Builder from scratch.

**Non-determinism isolation**: structural assertions pass across 3 runs with varying LLM outputs [175, 158, 150] chars. Deterministic workflows produce identical output across 5 runs.

**Concurrency**: 5+10 concurrent runs, zero cross-contamination, zero failures.

**Model upgrade**: DeepSeek V4 Pro `xhigh` vs `high`—Builder -35% time, Agent prompt +73% length, JSON truncation eliminated. Zero architecture changes.

## 7. Discussion

### 7.1 Scope

The composite describes agent systems—systems coupling determinate execution with indeterminate semantic reasoning. It does not describe pure ML inference, rule engines, or pure chatbots.

### 7.2 Limitations

Single-provider validation (DeepSeek only). Small template corpus (9 seeds). No cross-framework extraction validation.

### 7.3 Future Work

Multi-provider validation (OpenAI, Anthropic, Qwen). Template flywheel at scale (50+ sessions/day × 30 days). Cross-framework meta-cognition (apply extraction to LangGraph workflows). Automated granularity selection.

## 8. Conclusion

The agent definition debate is intractable because it asks the wrong question. Agents are not atomic things. The Harness+LLM composite is the irreducible structural invariant---it cannot be eliminated, only placed at different granularities. The chicken and the egg are the same thing.

The composite generates predictions, prescribes directions, and explains why every major framework's design reduces to a single decision. Lilies validates the paradigm with a 142-item test suite at 100%.

**The primitive is the pair. Everything else is a choice of granularity.**

---

## References

[1] Anthropic. Claude Code, 2025. [2] Anthropic. MCP, 2024. [3] LangGenius. Dify, 2024. [4] Temporal Technologies, 2024. [5] LangChain. LangGraph, 2024. [6] LangSmith, 2024. [7] AutoGPT, 2024. [8] LangGraph Concepts, 2025. [9] BabyAGI, 2023. [10] OpenAI Agents SDK, 2025. [11-21] As cited in text.
