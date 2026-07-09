from __future__ import annotations

import re
from typing import Any, Iterable

from .template_models import TemplateMeta


ALLOWED_REUSE_DEPTHS = {"none", "shallow", "deep", "adaptive"}
DEFAULT_TEMPLATE_SUGGESTION_REUSE_DEPTH = "adaptive"
DEFAULT_TEMPLATE_SUGGESTION_POLICY_VERSION = "v0.2.52_adaptive_default_productization"
ADAPTIVE_DEEP_BLOCK_HINTS = {
    "iteration",
    "loop",
    "parameter_extractor",
    "variable_aggregator",
}
ADAPTIVE_CONFIDENCE_FLOOR = 0.70


def _query_terms(requirement: str) -> list[str]:
    terms = [term for term in re.split(r"[^0-9A-Za-z_]+", requirement.casefold()) if len(term) > 2]
    if terms:
        return terms
    return [term for term in requirement.casefold().split() if len(term) > 1]


def score_template_matches(
    requirement: str,
    templates: Iterable[TemplateMeta],
) -> list[tuple[float, TemplateMeta]]:
    query = requirement.casefold().strip()
    if not query:
        return []
    terms = _query_terms(requirement)
    scored: list[tuple[float, TemplateMeta]] = []
    for meta in templates:
        name_title = f"{meta.name} {meta.title}".casefold()
        searchable = " ".join([
            meta.name,
            meta.title,
            meta.description,
            meta.category,
            *meta.tags,
        ]).casefold()
        tag_matches = sum(
            1
            for tag in meta.tags
            if tag.casefold() in query or any(term in tag.casefold() for term in terms)
        )
        name_matches = sum(1 for term in terms if term in name_title)
        text_matches = sum(1 for term in terms if term in searchable)
        full_query_match = 1.0 if query in searchable else 0.0
        raw_score = (
            0.45 * min(tag_matches, 3)
            + 0.35 * min(name_matches, 3)
            + 0.15 * min(text_matches, 5) / max(len(terms), 1)
            + 0.05 * full_query_match
        )
        score = round(meta.confidence * raw_score, 3)
        if score > 0.1:
            scored.append((score, meta))
    scored.sort(key=lambda item: (item[0], item[1].confidence, item[1].name), reverse=True)
    return scored


def resolve_effective_reuse_depth(
    reuse_depth: str,
    meta: TemplateMeta | None,
) -> tuple[str, str]:
    if reuse_depth not in ALLOWED_REUSE_DEPTHS:
        allowed = ", ".join(sorted(ALLOWED_REUSE_DEPTHS))
        raise ValueError(f"reuse_depth must be one of: {allowed}")
    if reuse_depth != "adaptive":
        return reuse_depth, f"explicit:{reuse_depth}"
    if meta is None:
        return "none", "adaptive:no_template_match"
    if meta.confidence < ADAPTIVE_CONFIDENCE_FLOOR:
        return "none", f"adaptive:low_confidence:{meta.confidence:.2f}"
    matched_hints = sorted(set(meta.min_blocks_required) & ADAPTIVE_DEEP_BLOCK_HINTS)
    if matched_hints:
        return "deep", f"adaptive:complex_blocks:{','.join(matched_hints)}"
    return "shallow", f"adaptive:template_match:{meta.name}"


def recommended_action_for_depth(depth: str) -> str:
    if depth == "none":
        return "build_from_scratch"
    if depth == "deep":
        return "compose_modules"
    return "expand_template"


def suggestion_default_metadata(
    requested_reuse_depth: str | None,
    *,
    build_plan_reuse_depth: str | None = None,
) -> tuple[str, dict[str, Any]]:
    requested = str(requested_reuse_depth or "").strip()
    if requested:
        return requested, {
            "reuse_depth_source": "explicit",
            "defaulted_by_policy": False,
            "default_policy_version": None,
            "available_overrides": sorted(ALLOWED_REUSE_DEPTHS),
        }
    if build_plan_reuse_depth:
        return build_plan_reuse_depth, {
            "reuse_depth_source": "build_plan",
            "defaulted_by_policy": False,
            "default_policy_version": None,
            "available_overrides": sorted(ALLOWED_REUSE_DEPTHS),
        }
    return DEFAULT_TEMPLATE_SUGGESTION_REUSE_DEPTH, {
        "reuse_depth_source": "policy_default",
        "defaulted_by_policy": True,
        "default_policy_version": DEFAULT_TEMPLATE_SUGGESTION_POLICY_VERSION,
        "available_overrides": sorted(ALLOWED_REUSE_DEPTHS),
    }


def build_suggestion_payload(
    meta: TemplateMeta,
    score: float,
    reuse_depth: str,
    *,
    effective_reuse_depth: str | None = None,
    policy_reason: str | None = None,
    default_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_depth = effective_reuse_depth
    resolved_reason = policy_reason
    if resolved_depth is None or resolved_reason is None:
        resolved_depth, resolved_reason = resolve_effective_reuse_depth(reuse_depth, meta)
    payload = {
        **meta.model_dump(mode="json"),
        "relevance_score": round(score, 3),
        "reuse_depth": reuse_depth,
        "effective_reuse_depth": resolved_depth,
        "recommended_action": recommended_action_for_depth(resolved_depth),
        "policy_reason": resolved_reason,
    }
    if default_metadata:
        payload.update(default_metadata)
    return payload
