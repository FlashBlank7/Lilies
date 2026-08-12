"""Requirement complexity classification for bounded emergence.

Deterministic, signal-based phase-trigger: decides whether a build may use a
collective (team) of agents or should stay a single unit.

This is layer-1 of the bounded-emergence design
(docs/current-design/design_bounded_emergence_v1.md): the "edge of chaos"
phase transition. It is lean-core aligned — it ONLY flips the
single-vs-team availability gate (`allow_team`) and records the decision for
observability. It never removes capabilities; it decides whether the team
capability is *offered* for a given requirement. The default is conservative
(no team) so simple tasks stay simple; collaboration is offered only when the
requirement signals complexity.

Theoretical basis: R11 (completeness is relative — collaboration mode is
requirement-driven, not a default) and workflow-is-server (collaboration is a
service-composition decision made before execution, not an ambient property).
"""

from __future__ import annotations

# Signals that a task is likely single-unit sufficient (stay lean).
SINGLE_SIGNALS = frozenset({
    "翻译", "总结", "格式化", "提取", "清洗", "重写", "转换", "生成",
    "greeting", "summarize", "translate", "format", "extract", "generate",
})

# Signals that a task likely needs collective collaboration (team allowed).
TEAM_SIGNALS = frozenset({
    "多智能体", "协作", "团队", "并行", "分头", "审核", "评审", "复核",
    "多角色", "分工", "流水线", "多步骤", "跨领域", "分工合作",
    "multi-agent", "collaborat", "team", "parallel", "review", "cross-check",
    "orchestrat", "delegat",
})

# Long requirements are likely multi-step and benefit from a team.
COMPLEX_MIN_CHARS = 200
LONG_MIN_CHARS = 80


def classify_requirement(requirement: str) -> dict[str, object]:
    """Classify a build requirement into a single-vs-team phase.

    Deterministic: same input, same output. This is the "phase transition"
    trigger — the router flips the builder's team availability at the
    ordered/chaotic boundary of task complexity.

    Returns:
        level: "simple" | "medium" | "complex"
        allow_team: whether the builder may offer team collaboration
        signals: {"single": [...], "team": [...]} matched signals
        confidence: float 0..1
        requirement_chars: int
    """
    text = (requirement or "").strip()
    lowered = text.casefold()
    hits_single = sorted(s for s in SINGLE_SIGNALS if s.casefold() in lowered)
    hits_team = sorted(s for s in TEAM_SIGNALS if s.casefold() in lowered)

    if hits_team:
        # Explicit collaboration signals dominate: the requirement asks for a team.
        level = "complex"
        allow_team = True
        confidence = min(0.95, 0.65 + 0.05 * len(hits_team))
    elif len(text) >= COMPLEX_MIN_CHARS and len([c for c in text if c in "。；;."]) >= 2:
        # Long, multi-sentence requirement spanning multiple concerns.
        level = "complex"
        allow_team = True
        confidence = 0.7
    elif len(text) >= LONG_MIN_CHARS:
        level = "medium"
        allow_team = True
        confidence = 0.6
    else:
        # Short, single-deliverable requirement: stay a single unit.
        level = "simple"
        allow_team = False
        confidence = 0.85 if hits_single else 0.6

    return {
        "level": level,
        "allow_team": allow_team,
        "signals": {"single": hits_single, "team": hits_team},
        "confidence": round(confidence, 2),
        "requirement_chars": len(text),
    }
