"""Orchestration Advisor — recommend block combinations from natural language requirements.

Reduces the cognitive load of choosing among 42 blocks by matching requirement
patterns to known block sequences. Can be used by:
  - Builder Team (as a first-pass suggestion before detailed building)
  - Template suggestions API (ranking by block-level relevance)
  - Human editor hints (frontend: "You might also need ...")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .blocks import BlockRegistry
    from .template_store import TemplateStore


@dataclass
class BlockRecommendation:
    block_type: str
    title: str
    reason: str
    priority: int = 5  # 1=essential, 10=optional


@dataclass
class SequenceRecommendation:
    """A recommended order of blocks for a given requirement pattern."""

    pattern_name: str
    description: str
    sequence: list[str]  # ordered block types
    templates: list[str] = field(default_factory=list)  # matching template names
    match_score: float = 0.0


# ── Known composition patterns ──────────────────────────────────

_KNOWN_PATTERNS: list[dict] = [
    {
        "keywords": ["classify", "route", "intent", "category", "support", "ticket", "客服", "分类", "路由"],
        "pattern_name": "Intent-based Router",
        "description": "Classify input text and route to different handlers",
        "sequence": ["start", "question_classifier", "if_else", "template_transform", "variable_aggregator", "end"],
        "templates": ["customer_support_router"],
        "essential": ["question_classifier", "if_else"],
    },
    {
        "keywords": ["code", "review", "bug", "test", "fix", "debug", "repair", "代码", "Bug", "测试"],
        "pattern_name": "Code Review & Repair",
        "description": "Read code → run tests → find bugs → fix → re-test → report",
        "sequence": ["start", "llm", "tool", "end"],
        "templates": ["code_reviewer"],
        "essential": ["llm", "tool"],
    },
    {
        "keywords": ["data", "analyze", "statistics", "report", "csv", "json", "数据", "分析", "统计"],
        "pattern_name": "Data Analysis Pipeline",
        "description": "Load data → compute statistics → extract insights → format report",
        "sequence": ["start", "llm", "parameter_extractor", "template_transform", "end"],
        "templates": ["data_analyzer"],
        "essential": ["llm", "parameter_extractor"],
    },
    {
        "keywords": ["summarize", "summary", "document", "article", "long", "摘要", "文档", "总结"],
        "pattern_name": "Document Summarizer",
        "description": "Read document → multi-level summary → format output",
        "sequence": ["start", "llm", "template_transform", "end"],
        "templates": ["document_summarizer"],
        "essential": ["llm", "template_transform"],
    },
    {
        "keywords": ["decompose", "task", "plan", "breakdown", "subtask", "dependency", "拆解", "分解", "任务", "规划"],
        "pattern_name": "Task Decomposer",
        "description": "Break complex task into ordered subtasks with dependencies",
        "sequence": ["start", "llm", "template_transform", "end"],
        "templates": ["task_decomposer"],
        "essential": ["llm", "template_transform"],
    },
    {
        "keywords": ["agent", "loop", "multi-turn", "tool call", "permission", "context", "智能体", "多轮"],
        "pattern_name": "Agent Architecture Loop",
        "description": "Full Claude-like agent: context → model turn → tool → result → control",
        "sequence": [
            "context_assembler", "model_turn", "tool_call_router",
            "tool_executor", "tool_result_normalizer", "stop_continue_controller",
            "permission_gate", "budget_gate", "event_recorder",
        ],
        "templates": [],
        "essential": ["model_turn", "tool_executor"],
    },
    {
        "keywords": ["automate", "app", "automation", "schedule", "定时", "自动", "app", "自动化"],
        "pattern_name": "App Automation Decision",
        "description": "Check API availability → quick mode → simulated clicks → manual fallback",
        "sequence": ["start", "llm", "if_else", "template_transform", "end"],
        "templates": ["app_automation_workflow"],
        "essential": ["llm", "if_else", "template_transform"],
    },
    {
        "keywords": ["search", "web", "news", "digest", "search engine", "搜索", "新闻", "聚合"],
        "pattern_name": "Search & Aggregate",
        "description": "Search web → aggregate results → format summary",
        "sequence": ["start", "tool", "llm", "template_transform", "end"],
        "templates": [],
        "essential": ["tool", "llm"],
    },
]


class OrchestrationAdvisor:
    """Recommend block sequences and templates for a given requirement."""

    def __init__(
        self,
        blocks: "BlockRegistry | None" = None,
        templates: "TemplateStore | None" = None,
    ) -> None:
        self._blocks = blocks
        self._templates = templates

    # ── public API ─────────────────────────────────────────────

    def recommend_sequence(self, requirement: str) -> list[SequenceRecommendation]:
        """Return matching block sequences ranked by keyword match."""
        query = requirement.casefold()
        results: list[SequenceRecommendation] = []

        for pat in _KNOWN_PATTERNS:
            keyword_hits = sum(
                1 for kw in pat["keywords"] if kw.casefold() in query
            )
            if keyword_hits == 0:
                continue
            # Score: keyword density × template availability bonus
            score = keyword_hits / max(len(pat["keywords"]), 1)
            if pat["templates"]:
                score += 0.2

            results.append(SequenceRecommendation(
                pattern_name=pat["pattern_name"],
                description=pat["description"],
                sequence=pat["sequence"],
                templates=pat["templates"],
                match_score=round(min(score, 1.0), 3),
            ))

        results.sort(key=lambda r: r.match_score, reverse=True)
        return results

    def recommend_blocks(
        self, requirement: str
    ) -> list[BlockRecommendation]:
        """Recommend individual blocks based on requirement keywords."""
        query = requirement.casefold()
        seen: set[str] = set()
        recommendations: list[BlockRecommendation] = []

        for pat in _KNOWN_PATTERNS:
            keyword_hits = sum(
                1 for kw in pat["keywords"] if kw.casefold() in query
            )
            if keyword_hits == 0:
                continue
            for block_type in pat.get("essential", []):
                if block_type not in seen:
                    seen.add(block_type)
                    recommendations.append(BlockRecommendation(
                        block_type=block_type,
                        title=_block_title(block_type),
                        reason=f"Essential for: {pat['pattern_name']}",
                        priority=1,
                    ))
            for block_type in pat["sequence"]:
                if block_type not in seen and block_type not in ("start", "end"):
                    seen.add(block_type)
                    recommendations.append(BlockRecommendation(
                        block_type=block_type,
                        title=_block_title(block_type),
                        reason=f"Part of: {pat['pattern_name']}",
                        priority=5,
                    ))

        return sorted(recommendations, key=lambda r: r.priority)

    def recommend_all(
        self, requirement: str
    ) -> dict[str, Any]:
        """Return sequences, blocks, and templates in one response."""
        sequences = self.recommend_sequence(requirement)
        blocks = self.recommend_blocks(requirement)

        template_names: list[str] = []
        for s in sequences:
            for tn in s.templates:
                if tn not in template_names:
                    template_names.append(tn)

        return {
            "requirement": requirement,
            "sequences": [
                {
                    "pattern_name": s.pattern_name,
                    "description": s.description,
                    "sequence": s.sequence,
                    "templates": s.templates,
                    "match_score": s.match_score,
                }
                for s in sequences[:3]
            ],
            "recommended_blocks": [
                {
                    "block_type": b.block_type,
                    "title": b.title,
                    "reason": b.reason,
                    "priority": b.priority,
                }
                for b in blocks[:8]
            ],
            "suggested_templates": template_names[:5],
        }


# ── helpers ────────────────────────────────────────────────────

def _block_title(block_type: str) -> str:
    """Human-readable title for a block type."""
    titles = {
        "llm": "LLM Call",
        "tool": "Tool Executor",
        "if_else": "If / Else Branch",
        "question_classifier": "Question Classifier",
        "template_transform": "Template Transform",
        "variable_aggregator": "Variable Aggregator",
        "variable_assigner": "Variable Assigner",
        "parameter_extractor": "Parameter Extractor",
        "iteration": "Iteration",
        "loop": "Loop",
        "http_request": "HTTP Request",
        "human_input": "Human Input",
        "context_assembler": "Context Assembler",
        "model_turn": "Model Turn",
        "tool_call_router": "Tool Call Router",
        "tool_executor": "Tool Executor",
        "tool_result_normalizer": "Tool Result Normalizer",
        "stop_continue_controller": "Stop/Continue",
        "permission_gate": "Permission Gate",
        "budget_gate": "Budget Gate",
        "round_limit": "Round Limit",
        "retry_error_classifier": "Error Classifier",
        "event_recorder": "Event Recorder",
        "subagent_spawn": "Subagent Spawn",
        "task_dispatcher": "Task Dispatcher",
        "sandbox_boundary": "Sandbox Boundary",
        "checkpoint_resume": "Checkpoint/Resume",
        "hook_point": "Hook Point",
    }
    return titles.get(block_type, block_type.replace("_", " ").title())
