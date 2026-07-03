"""Template system — codify expert workflows as reusable, composable assets.

Provenance tracking (v2): templates carry a confidence score and a history of
their sources — whether hand-crafted by an expert or extracted from a session.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .workflow_models import WorkflowSpec


class ProvenanceSource(BaseModel):
    """One source that contributed to a template's creation or validation."""

    source_type: Literal["expert_manual", "session_extract"] = "expert_manual"
    identifier: str = ""
    created_at: str = ""
    user_id: str | None = None


class TemplateMeta(BaseModel):
    """Searchable metadata for the template marketplace."""

    name: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    category: Literal[
        "code_engineering", "data_analysis", "customer_service",
        "content_creation", "task_management", "agent_architecture",
    ] = "task_management"
    icon: str = "workflow"
    tags: list[str] = Field(default_factory=list)
    expected_inputs: dict[str, str] = Field(default_factory=dict)
    expected_outputs: dict[str, str] = Field(default_factory=dict)
    author: str = "platform"
    version: int = 1
    min_blocks_required: list[str] = Field(default_factory=list)
    # Provenance tracking
    provenance: list[ProvenanceSource] = Field(default_factory=list)
    confidence: float = Field(default=0.70, ge=0.10, le=1.0)
    seed_template: bool = False
    usage_count: int = Field(default=0, ge=0)
    pending_branches_count: int = Field(default=0, ge=0)
    rating_sum: float = Field(default=0.0, ge=0.0)
    rating_count: int = Field(default=0, ge=0)

    @property
    def rating(self) -> float:
        """Average rating, 0.0 if no ratings yet."""
        if self.rating_count == 0:
            return 0.0
        return round(self.rating_sum / self.rating_count, 1)

    @property
    def quality_score(self) -> float:
        """Composite quality score for ranking.

        Formula: confidence (0.1-1.0) × log2(1+usage_count) × (1 + rating/10)
        - confidence: 直接来自溯源追踪
        - usage_count: 对数增长（1次=0, 3次≈1, 7次≈2, 15次≈3）
        - rating: 0-5分，最多贡献+0.5倍
        """
        import math
        usage_bonus = math.log2(1 + self.usage_count)
        rating_bonus = 1.0 + (self.rating / 10.0) if self.rating > 0 else 1.0
        return round(self.confidence * max(usage_bonus, 1.0) * rating_bonus, 3)


class Template(BaseModel):
    """A complete template: metadata + workflow spec."""

    meta: TemplateMeta
    workflow: WorkflowSpec

    model_config = {"extra": "forbid"}


class TemplateCreateRequest(BaseModel):
    """Publish the current draft as a new template."""

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    category: TemplateMeta.model_fields["category"].annotation = "task_management"
    tags: list[str] = Field(default_factory=list)
    icon: str = "workflow"
