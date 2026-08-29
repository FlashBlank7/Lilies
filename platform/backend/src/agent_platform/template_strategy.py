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


# 字符范围和 knowledge_rag._tokens 对齐（那个模块早就把中文切对了，
# 这里却没有——同一个判据没铺满所有地方，今天第 N 次）。
# 含扩展 A 区、日文假名、谚文：只写 一-鿿 的话，
# 冷僻字和日韩文本会整段被当成分隔符，退回"整句一个词"那个老毛病。
# 不同的是那边还加了单字（检索要召回），这里只要二字词——
# 单字在模板名/描述里命中一切，是噪声不是信号。
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿぀-ヿ가-힯]+")


def _query_terms(requirement: str) -> list[str]:
    """把需求切成用来比对模板的词。

    中文要单独切。原来只按 `[^0-9A-Za-z_]+` 切——**中文字符全是分隔符**，
    于是一句纯中文切完什么都不剩，掉进"按空格切"那条兜底，
    整句变成**一个词**。量出来的后果（本仓真机需求全是中文）：

      · 无 tags 的中文模板，只有当需求**一字不差等于模板名**时才配得上；
        名字叫「词频统计」、描述写着"统计文本里每个词出现的次数"的模板，
        遇到需求"统计文本中每个词出现次数"——**一个都配不上**。
      · 有 tags 的中文模板能配上，但走的只有 tags 这一条路，
        name/description 两条完全不出力，分数系统性偏低
        （同等贴切时中文 0.63、英文 1.557）。

    中文没有空格，正经分词要带词典。这里用**二字滑窗**：中文里
    绝大多数有信息量的词是两个字，滑窗必然覆盖到它们，代价是多一些噪声词，
    而噪声词配不上模板文本、不会加分。够用且没有新依赖。
    """
    latin = [term for term in re.split(r"[^0-9A-Za-z_]+", requirement.casefold())
             if len(term) > 2]
    cjk: list[str] = []
    for run in _CJK_RUN.findall(requirement):
        # 单字（「把」「的」这种夹在中间的）不会进来：滑窗本身就出不了单字，
        # 长度 1 的串走到下面那句是 range(0)，什么都不产生。
        # 这里**不加**一句 `if len(run) < 2: continue`——写过，变异验证显示
        # 它是等价变异（删掉一条测试都不红）。看着像在把关、实际什么也没做
        # 的代码比没有更糟。
        if len(run) == 2:
            cjk.append(run)
            continue
        cjk.extend(run[i:i + 2] for i in range(len(run) - 1))
    terms = latin + cjk
    if terms:
        # 去重但保序：滑窗会重复出词，重复的词会把 text_matches 的分母抬高
        return list(dict.fromkeys(terms))
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


def policy_default_execution_contract(
    effective_reuse_depth: str,
    *,
    reuse_depth_source: str = "policy_default",
) -> dict[str, str]:
    return {
        "next_step": "set_build_plan_reuse_depth",
        "reuse_depth_to_record": effective_reuse_depth,
        "then": recommended_action_for_depth(effective_reuse_depth),
        "preserve_reuse_depth_source": reuse_depth_source,
    }


def suggestion_default_metadata(
    requested_reuse_depth: str | None,
    *,
    build_plan_reuse_depth: str | None = None,
    runtime_policy_reuse_depth: str | None = None,
    runtime_policy_version: str | None = None,
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
    runtime_policy_reuse_depth = str(runtime_policy_reuse_depth or "").strip()
    if runtime_policy_reuse_depth:
        return runtime_policy_reuse_depth, {
            "reuse_depth_source": "complexity_router",
            "defaulted_by_policy": True,
            "default_policy_version": runtime_policy_version,
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
        if default_metadata.get("defaulted_by_policy"):
            payload["execution_contract"] = policy_default_execution_contract(
                resolved_depth,
                reuse_depth_source=str(default_metadata.get("reuse_depth_source") or "policy_default"),
            )
    return payload
