from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "summarize_experiment_evidence.py"
    spec = importlib.util.spec_from_file_location("summarize_experiment_evidence_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_summary_includes_compact_policy_default_reuse_metadata(tmp_path: Path) -> None:
    module = load_module()
    evidence_path = tmp_path / "experiment_policy_default.json"
    evidence = {
        "experiment": "E05 template reuse-depth live comparison",
        "status": "completed",
        "started_at": "2026-07-10T00:00:00Z",
        "finished_at": "2026-07-10T00:05:00Z",
        "arms": [
            {
                "depth": "policy_default",
                "status": "completed",
                "build_status": "published",
                "elapsed_seconds": 123.4,
                "usage_counts": {"model_call": 12, "tool_call": 18},
                "event_summary": {"template_suggestion_count": 1, "template_expand_count": 1},
                "resolved_template_strategy": {
                    "reuse_depth": "adaptive",
                    "reuse_depth_source": "policy_default",
                    "effective_reuse_depth": "deep",
                    "recommended_action": "compose_modules",
                },
                "benchmark_outcome": {"case_passed": True, "case_score": 1.0},
            }
        ],
    }
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_path = module.summarize_file(evidence_path)

    text = summary_path.read_text(encoding="utf-8")
    assert "policy_default" in text
    assert "policy_default, adaptive->deep, compose_modules" in text
    assert "case=true, score=1" in text
