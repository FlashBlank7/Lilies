# Lilies 元认知层 — 设计规划与实现方案

> 目标读者: 接手此项目的 AI/开发者。本文档描述了一个完整的功能模块的设计、接口和数据流，可以直接按此方案实现。

---

## 目录

1. [问题定义](#1-问题定义)
2. [核心设计决策](#2-核心设计决策)
3. [架构概览](#3-架构概览)
4. [数据模型](#4-数据模型)
5. [API 设计](#5-api-设计)
6. [实现步骤](#6-实现步骤)
7. [与现有系统的集成点](#7-与现有系统的集成点)
8. [测试计划](#8-测试计划)

---

## 1. 问题定义

### 背景

Lilies 的核心价值主张是"固化工作流"。当前，工作流的创建方式有两种：
- **手工搭建**：用户在画布上拖拽积木
- **Builder Team**：AI 根据自然语言需求自动搭建

这两种方式都缺少一个关键环节：**从人+AI 的实际协作过程中自动学习**。

在我们的实际协作中（以"钉钉自动打卡"为例），整个探索过程形成了一个决策树：

```
"自动打卡" → API 可用? → NO → 急速模式? → YES → 只需启动App
```

这个决策树的价值在于：**它适用于任何"自动操作外部 App"的需求**，不仅限于钉钉。

但目前，这个决策树的编码方式是**事后手工整理**——先经历探索（~4h），再人工写成模板或文档。理想状态是**协作过程中实时捕获**。

### 目标

构建一个**元认知层 (Meta-Cognition Layer)**，使 Lilies 能够：

1. **观察**：在 Builder Team 或人+AI 协作过程中，自动识别关键决策点
2. **提取**：将决策树转换为结构有效的 WorkflowSpec
3. **入库**：作为模板存入模板市场
4. **去重**：检测与已有模板的相似性，合并而非重复
5. **推荐**：在未来的类似任务中，主动建议已有模板

---

## 2. 核心设计决策

### 决策 A：触发时机 — 混合模式（自动 + 质量门控）

| 方案 | 优点 | 缺点 |
|------|------|------|
| 纯自动 (每次会话结束) | 覆盖面全，零遗漏 | 产生大量噪声（简单对话不需要提取） |
| 纯手动 (用户显式触发) | 精准，无噪声 | 遗漏率高（~60% 用户不会主动触发） |
| **混合模式 (推荐)** | 兼顾覆盖和质量 | 需要设计门控逻辑 |

**选定方案**：自动触发 + 质量门控。

```python
class ExtractionGate:
    """只提案有意义的提取，静默跳过平庸会话。"""

    def should_propose(
        self,
        session: dict,
        existing_templates: list[TemplateMeta],
    ) -> tuple[bool, str]:
        """
        Returns (should_propose, reason).

        Gates (all must pass):
          1. 至少 2 个决策点（单步简单任务不值得提取）
          2. 未被已有模板覆盖（避免重复提案）
          3. 包含新分支（相对于已有模板的增量价值）
        """
        decision_count = len(session.get("decision_points", []))
        if decision_count < 2:
            return False, f"insufficient_decision_points ({decision_count})"

        for template in existing_templates:
            if self._is_covered_by(session, template):
                return False, f"covered_by_template:{template.name}"

        if not self._has_novel_branches(session, existing_templates):
            return False, "no_novel_branches"

        return True, "proposed"

    def _is_covered_by(self, session, template) -> bool:
        """Check if the session's decision tree is already handled by a template."""
        session_tags = self._extract_tags(session)
        template_tags = set(template.tags or [])
        overlap = session_tags & template_tags
        return len(overlap) >= 2  # at least 2 matching tags

    def _has_novel_branches(self, session, templates) -> bool:
        """Check if any branch answer in the session is novel vs existing templates."""
        # Simplified: compare answer texts against template descriptions
        ...
```

**用户体验**：

```
会话结束
    ↓
ExtractionGate.should_propose()
    ↓
    ├─ False → 静默跳过
    └─ True  → 在会话摘要中显示:
               "🤖 本次协作涉及 3 个关键决策点，
                可提取为工作流模板。是否保存？[是] [忽略]"
```

### 决策 B：去重策略 — 语义合并 + 溯源追踪

| 方案 | 优点 | 缺点 |
|------|------|------|
| 每个会话一个模板 | 实现简单 | 大量重复，降低信噪比 |
| 严格去重（相同=合并） | 干净 | 丢失有价值的变体 |
| **语义合并+溯源 (推荐)** | 保留变体，累积置信度 | 需要相似度判断逻辑 |

**选定方案**：语义合并。两个独立用户各自发现了相似的决策树 → 合并为一个模板，但保留来源信息。

```python
@dataclass
class TemplateProvenance:
    """Track where a template came from and how confident we are."""

    sources: list[ProvenanceSource] = field(default_factory=list)
    confidence: float = 0.5  # starts low, increases with independent confirmations

@dataclass
class ProvenanceSource:
    source_type: Literal["expert_manual", "session_extract"]
    identifier: str       # session_id or "platform"
    created_at: str
    user_id: str | None = None

def merge_or_create(candidate: WorkflowSpec, existing: list[Template]) -> Template:
    """
    If candidate is semantically similar to an existing template → merge (bump confidence).
    Otherwise → create new template.

    "Semantically similar" = same category + >= 2 overlapping tags + similar decision structure.
    """
    for template in existing:
        if is_semantically_similar(candidate, template):
            template.meta.confidence = min(0.99, template.meta.confidence + 0.15)
            template.provenance.sources.append(new_source)
            template.meta.version += 1
            return template

    # New template
    return Template(
        workflow=candidate,
        provenance=ProvenanceSource(source_type="session_extract", ...),
        confidence=0.5
    )
```

**置信度模型**：

```
手工创建:     confidence = 0.70  (种子模板)
1次会话验证:  confidence = 0.85  (+0.15)
2次会话验证:  confidence = 0.95  (+0.10)
3次独立验证:  confidence = 0.98  (+0.03, diminishing)
```

### 决策 C：种子模板 (app_automation_workflow) 的定位

`app_automation_workflow.json` 是手工创建的元认知结果。它的定位：

| 角色 | 说明 |
|------|------|
| **校准器** | 验证元认知提取系统正确性的基准——如果能从真实会话中提取出与种子结构等价的工作流 → 系统正确 |
| **基线** | 种子 confidence=0.70，后续会话验证逐步提升 |
| **bootstrap** | 不需要等元认知系统完美后才能用——种子立即可用 |

**种子与自动提取的关系**：

```
种子模板 (v1, confidence=0.70)
    │
    ├─ 会话 ABC 提取出相似结构 → 匹配! → confidence=0.85
    │
    ├─ 会话 DEF 提取出相似结构 → 匹配! → confidence=0.95
    │
    └─ 会话 GHI 提取出不同分支 → 标记 review → 人工判断
        是否合并?
         ├─ Yes → 种子升级 v2 (含新分支)
         └─ No  → 创建独立模板 GHI
```

---

## 3. 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                    会话层 (Session Layer)                    │
│                                                             │
│  Builder Team 会话  │  Agent 会话  │  手工编辑会话            │
│  每次操作=事件       │  每次turn    │  每次mutate             │
└──────────┬────────────────────┬──────────────────┬──────────┘
           │                    │                  │
           ▼                    ▼                  ▼
┌─────────────────────────────────────────────────────────────┐
│               决策追踪器 (DecisionTracker)                    │
│                                                             │
│  .ask(question, context)   → 记录决策点                     │
│  .answer(answer, outcome)  → 记录分支                       │
│  .extract_workflow()       → 输出 WorkflowSpec              │
│  .summary()                → 人类可读摘要                    │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│               提取门控 (ExtractionGate)                       │
│                                                             │
│  should_propose(session, templates) → (bool, reason)        │
│  过滤条件: 决策点数量 / 模板覆盖 / 新颖性                     │
└──────────────────────────┬──────────────────────────────────┘
                           │ (if True)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              合并引擎 (MergeEngine)                           │
│                                                             │
│  is_semantically_similar(wf_a, wf_b) → bool                 │
│  merge_or_create(candidate, existing) → Template             │
│  相似度: category + tags + decision_structure               │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              模板市场 (TemplateStore)                         │
│                                                             │
│  register() / list() / get() / search()                     │
│  confidence / provenance / usage_count                      │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. 数据模型

### DecisionPoint & DecisionBranch

```python
from dataclasses import dataclass, field
from uuid import uuid4

@dataclass
class DecisionBranch:
    """One possible answer to a decision point."""
    answer: str                    # "YES", "NO", "LIMITED"
    description: str = ""          # human-readable explanation
    outcome: str = ""              # "Use HTTP Request block"
    sub_decisions: list["DecisionPoint"] = field(default_factory=list)


@dataclass
class DecisionPoint:
    """A single branching decision in a collaboration."""
    id: str = field(default_factory=lambda: str(uuid4()))
    question: str = ""             # "Does this app have a public API?"
    context: str = ""              # "Trying to automate DingTalk check-in"
    branches: list[DecisionBranch] = field(default_factory=list)
    parent_id: str | None = None
    depth: int = 0                 # nesting level in the decision tree
```

### TemplateMeta (扩展)

```python
class TemplateMeta(BaseModel):
    # ... existing fields ...

    # ── New: provenance tracking ──
    provenance: list[ProvenanceSource] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.1, le=1.0)
    seed_template: bool = False  # True if hand-crafted by platform
```

---

## 5. API 设计

### 新增端点

```bash
# ── Session extraction ──

POST /api/v1/sessions/{session_id}/extract-template
  → 分析会话历史，尝试提取决策树 → 工作流
  → 如果 ExtractionGate 通过 → 返回候选 WorkflowSpec
  → 如果未通过 → 返回 {proposed: false, reason: "..."}

Response 200:
{
  "proposed": true,
  "workflow": { ... WorkflowSpec ... },
  "summary": "# Decision Tree: ...",
  "similar_templates": [  # 已有相似模板
    {"name": "app_automation_workflow", "similarity": 0.92}
  ]
}

# ── Template merge ──

POST /api/v1/templates/{name}/merge-check
  Body: { "candidate": WorkflowSpec }
  → 检查候选工作流与已有模板的相似度

Response 200:
{
  "should_merge": true,
  "target": "app_automation_workflow",
  "similarity": 0.92,
  "confidence_after": 0.95,
  "diff_summary": "Identical decision structure. 1 new branch detected."
}

POST /api/v1/templates/{name}/merge
  Body: { "candidate": WorkflowSpec, "confirm": true }
  → 执行合并，更新模板版本和置信度

# ── Builder integration ──

GET /api/v1/templates/suggestions?requirement=...
  → 根据需求文本推荐匹配的模板
  → 按 confidence × tag_match 排序
```

### 与现有 `POST /api/v1/templates/{name}/expand` 的关系

```
expand:  模板 → 工作流草稿 (已有)
extract: 会话 → 工作流候选 (新增)
merge:   候选 → 模板 (新增，含去重)
```

---

## 6. 实现步骤

### Step 1: DecisionTracker (✅ 已完成)

**文件**: `meta_cognition.py`

已实现功能：
- `DecisionTracker.ask()` / `.answer()` — 记录决策
- `DecisionTracker.extract_workflow()` — 自动生成 WorkflowSpec
  - 每个决策点: LLM → If/Else
  - 每个分支: Template (方案描述)
  - 自动处理嵌套子决策
- `DecisionTracker.summary()` — Markdown 摘要

**已验证**:
- `demo_dingtalk_workflow()` 生成 15 节点/14 边工作流
- `validate_workflow()` 返回 0 结构错误

### Step 2: ExtractionGate (🔜 待实现)

**新文件**: `extraction_gate.py`

```python
class ExtractionGate:
    def __init__(self, template_store: TemplateStore):
        self.store = template_store

    def should_propose(
        self,
        decision_points: list[DecisionPoint],
    ) -> tuple[bool, str]:
        """
        Gate checks:
          1. len(decision_points) >= 2
          2. Not covered by existing template
          3. Contains novel branches
        """
        # Gate 1: minimum decision count
        if len(decision_points) < 2:
            return False, "insufficient_decisions"

        # Gate 2: template coverage check
        existing = self.store.list()
        for template in existing:
            if self._is_covered(decision_points, template):
                return False, f"covered_by:{template.meta.name}"

        # Gate 3: novelty check
        if not self._is_novel(decision_points, existing):
            return False, "no_novel_branches"

        return True, "proposed"

    def _is_covered(self, dps, template) -> bool:
        """Check via tag overlap."""
        session_tags = self._extract_tags_from_decisions(dps)
        template_tags = set(template.meta.tags or [])
        return len(session_tags & template_tags) >= 2

    def _extract_tags_from_decisions(self, dps) -> set[str]:
        """Extract tags from decision questions/answers."""
        tags = set()
        for dp in dps:
            text = (dp.question + dp.context).lower()
            if "api" in text: tags.add("api")
            if "automation" in text or "自动" in text: tags.add("automation")
            if "app" in text or "应用" in text: tags.add("app")
            if "schedule" in text or "定时" in text: tags.add("scheduled")
        return tags

    def _is_novel(self, dps, templates) -> bool:
        """At least one branch answer is not covered by any existing template."""
        if not templates:
            return True
        # Simple heuristic: check if the number of branches exceeds
        # typical existing templates in the same category
        branch_count = sum(len(dp.branches) for dp in dps)
        max_existing = max(
            (len(t.meta.tags) for t in templates if t.meta.tags),
            default=0
        )
        return branch_count > max_existing or branch_count >= 3
```

### Step 3: MergeEngine (🔜 待实现)

**新文件**: `merge_engine.py`

```python
@dataclass
class SimilarityResult:
    should_merge: bool
    target_template: str | None  # template name if should_merge
    similarity_score: float      # 0.0 - 1.0
    confidence_after: float
    diff_summary: str

class MergeEngine:
    def __init__(self, template_store: TemplateStore):
        self.store = template_store

    def check_similarity(
        self,
        candidate: WorkflowSpec,
    ) -> SimilarityResult:
        """Check if candidate is similar to any existing template."""
        best_score = 0.0
        best_template = None

        for template in self.store.list():
            score = self._compute_similarity(candidate, template.workflow)
            if score > best_score:
                best_score = score
                best_template = template

        if best_score >= 0.7 and best_template:
            return SimilarityResult(
                should_merge=True,
                target_template=best_template.meta.name,
                similarity_score=best_score,
                confidence_after=min(0.99, best_template.meta.confidence + 0.15),
                diff_summary=self._compute_diff(candidate, best_template.workflow),
            )

        return SimilarityResult(
            should_merge=False,
            target_template=None,
            similarity_score=best_score,
            confidence_after=0.0,
            diff_summary="No similar template found."
        )

    def _compute_similarity(self, a: WorkflowSpec, b: WorkflowSpec) -> float:
        """
        Compute structural similarity between two workflows.

        Factors:
          - Node type sequence match (0.4 weight)
          - Decision depth match (0.3 weight)
          - Tag overlap from template meta (0.3 weight)
        """
        a_types = [n.type for n in a.nodes]
        b_types = [n.type for n in b.nodes]

        # Jaccard similarity of node types
        a_set, b_set = set(a_types), set(b_types)
        type_sim = len(a_set & b_set) / max(len(a_set | b_set), 1)

        # Depth similarity (count decision LLM nodes)
        a_depth = sum(1 for n in a.nodes if n.type == "llm")
        b_depth = sum(1 for n in b.nodes if n.type == "llm")
        depth_sim = 1.0 - abs(a_depth - b_depth) / max(a_depth, b_depth, 1)

        return 0.4 * type_sim + 0.3 * depth_sim  # + 0.3 from tag overlap (external)

    def _compute_diff(self, candidate: WorkflowSpec, existing: WorkflowSpec) -> str:
        c_types = [n.type for n in candidate.nodes]
        e_types = [n.type for n in existing.nodes]
        c_only = set(c_types) - set(e_types)
        e_only = set(e_types) - set(c_types)
        parts = []
        if c_only:
            parts.append(f"+{len(c_only)} new node types: {sorted(c_only)}")
        if e_only:
            parts.append(f"-{len(e_only)} removed node types: {sorted(e_only)}")
        return "; ".join(parts) if parts else "Identical structure"

    def merge(
        self,
        candidate: WorkflowSpec,
        target_name: str,
        source: ProvenanceSource,
    ) -> Template:
        """Merge candidate into existing template. Bump confidence and version."""
        template = self.store.get(target_name)
        template.meta.provenance.append(source)
        template.meta.confidence = min(0.99, template.meta.confidence + 0.15)
        template.meta.version += 1
        # In v1, keep the existing workflow (candidate adds confidence, not structure)
        # v2 can merge novel branches into the template workflow
        return template
```

### Step 4: Builder Integration (🔜 待实现)

在 Builder 的系统 Prompt 中增加模板推荐逻辑：

```python
# builder.py — BUILDER_SYSTEM_PROMPT 新增规则

"""
- Before building a new workflow from scratch, call catalog_search with the
  requirement text to check if a matching template already exists.
- If a template with confidence >= 0.7 matches, suggest expanding it
  instead of building from scratch.
- After completing a build session, call the extract-template API to
  check if a new decision pattern was discovered.
"""
```

### Step 5: API 端点 (🔜 待实现)

在 `api.py` 中添加：

```python
@app.post("/api/v1/sessions/{session_id}/extract-template")
async def extract_template_from_session(session_id: str):
    """Try to extract a workflow template from a session's decision history."""
    # 1. Get session decision points
    # 2. ExtractionGate.should_propose()
    # 3. If true → DecisionTracker.extract_workflow() → return WorkflowSpec
    # 4. If false → return {proposed: false, reason: "..."}

@app.post("/api/v1/templates/{name}/merge-check")
async def check_template_merge(name: str, body: MergeCheckRequest):
    """Check if a candidate workflow should be merged into an existing template."""

@app.post("/api/v1/templates/{name}/merge")
async def merge_template(name: str, body: MergeRequest):
    """Merge a candidate workflow into an existing template."""

@app.get("/api/v1/templates/suggestions")
async def suggest_templates(requirement: str = ""):
    """Suggest matching templates for a requirement. Sorted by confidence × relevance."""
```

---

## 7. 与现有系统的集成点

| 现有组件 | 集成方式 | 改动量 |
|---------|---------|--------|
| `TemplateStore` | 增加 `confidence`, `provenance` 字段 | 小 (3-5 行) |
| `TemplateMeta` | 增加 `confidence`, `provenance`, `seed_template` | 小 (3 字段) |
| Builder system prompt | 增加模板推荐规则 | 中 (5-8 行) |
| `api.py` | 新增 4 个端点 | 中 (~80 行) |
| `catalog_search` | 返回结果增加模板 match_score | 小 (5 行) |

无需改动：
- `blocks.py`
- `workflow_runtime.py`
- `factory.py`
- `runtime.py`

---

## 8. 测试计划

### 单元测试

```python
# test_meta_cognition.py

def test_extraction_gate_rejects_single_decision():
    """Single decision point → not proposed."""
    gate = ExtractionGate(empty_store)
    assert gate.should_propose([single_dp]) == (False, "insufficient_decisions")

def test_extraction_gate_proposes_novel_pattern():
    """Novel 3-decision pattern → proposed."""
    gate = ExtractionGate(empty_store)
    assert gate.should_propose([dp1, dp2, dp3]) == (True, "proposed")

def test_extraction_gate_skips_covered_pattern():
    """Pattern already covered by template → skipped."""
    store = store_with_app_automation_template()
    gate = ExtractionGate(store)
    assert gate.should_propose([similar_dp1, similar_dp2])[0] == False

def test_merge_engine_detects_similar():
    """Identical structure → should_merge=True."""
    engine = MergeEngine(store_with_app_automation_template())
    result = engine.check_similarity(candidate_workflow)
    assert result.should_merge
    assert result.similarity_score >= 0.7

def test_merge_engine_rejects_dissimilar():
    """Completely different structure → should_merge=False."""
    engine = MergeEngine(store_with_app_automation_template())
    result = engine.check_similarity(unrelated_workflow)
    assert not result.should_merge

def test_extract_workflow_is_valid():
    """Extracted workflow passes structural validation."""
    tracker = demo_dingtalk_workflow_tracker()
    wf = tracker.extract_workflow()
    errors = registry.validate_workflow(wf)
    assert len([e for e in errors if 'test' not in e.lower()]) == 0

def test_confidence_bumps_on_merge():
    """Merging bumps confidence correctly."""
    template = Template(meta=TemplateMeta(confidence=0.70))
    engine = MergeEngine(store)
    result = engine.merge(candidate, template.name, new_source)
    assert result.meta.confidence == 0.85  # 0.70 + 0.15
    assert result.meta.version == 2  # bumped
```

### 集成测试

```python
def test_full_extraction_pipeline():
    """End-to-end: session → extract → gate → merge → template."""
    # 1. Simulate a collaboration session with decision points
    tracker = DecisionTracker("Test automation task")
    tracker._current = tracker.ask("API available?")
    tracker.answer("NO", "Use quick mode")
    tracker._current = tracker.ask("Quick mode available?")
    tracker.answer("YES", "Just launch app")

    # 2. Gate check
    gate = ExtractionGate(template_store)
    should, reason = gate.should_propose(tracker.roots)
    assert should

    # 3. Extract workflow
    wf = tracker.extract_workflow()
    assert len(wf.nodes) >= 5

    # 4. Merge check
    engine = MergeEngine(template_store)
    sim_result = engine.check_similarity(wf)
    # Should match app_automation_workflow (same pattern)

    # 5. Merge
    if sim_result.should_merge:
        merged = engine.merge(wf, sim_result.target_template, source)
        assert merged.meta.confidence > 0.70
```

---

## 附录：关键文件清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `meta_cognition.py` | ✅ 已完成 | DecisionTracker + extract_workflow + 演示 |
| `template_models.py` | 🔜 需更新 | TemplateMeta 增加 provenance/confidence/seed_template |
| `template_store.py` | ✅ 已完成 | 模板加载/搜索/展开/注册 |
| `extraction_gate.py` | 🔜 待创建 | 按本文档 §6 Step 2 实现 |
| `merge_engine.py` | 🔜 待创建 | 按本文档 §6 Step 3 实现 |
| `api.py` | 🔜 需更新 | 4 个新端点 + DecisionTracker 集成 |
| `builder.py` | 🔜 需更新 | system prompt 增加模板推荐规则 |
| `templates/app_automation_workflow.json` | ✅ 已完成 | 种子模板 (手工创建) |
| `tests/test_meta_cognition.py` | 🔜 待创建 | 按本文档 §8 实现 |

## 附录：种子模板清单

| 模板名 | 来源 | confidence | 验证次数 |
|--------|------|-----------|---------|
| `app_automation_workflow` | 手工 (platform) | 0.70 | 0 (种子) |
| `code_reviewer` | 手工 (platform) | 0.70 | 0 |
| `data_analyzer` | 手工 (platform) | 0.70 | 0 |
| `customer_support_router` | 手工 (platform) | 0.70 | 0 |
| `document_summarizer` | 手工 (platform) | 0.70 | 0 |
| `task_decomposer` | 手工 (platform) | 0.70 | 0 |
| `long_form_writer` | 手工 (platform) | 0.70 | 0 |
| `dingtalk_checkin` | 手工 (platform) | 0.70 | 0 |
| `dingtalk_checkout` | 手工 (platform) | 0.70 | 0 |
