from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "e05_adaptive_reuse_policy_backtest.py"
    spec = importlib.util.spec_from_file_location("e05_adaptive_reuse_policy_backtest_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_backtest_matches_expected_adaptive_policy(tmp_path: Path) -> None:
    module = load_module()
    output_path = tmp_path / "adaptive_backtest.json"
    payload = module.run_backtest(
        templates_dir=Path(__file__).resolve().parents[1] / "templates",
        output_path=output_path,
    )

    assert payload["status"] == "completed"
    assert payload["summary"] == {
        "family_count": 3,
        "exact_matches": 2,
        "bounded_matches": 1,
        "mismatches": 0,
    }

    families = {item["family"]: item for item in payload["families"]}
    assert families["code_reviewer"]["recommended_depth"] == "shallow"
    assert families["code_reviewer"]["alignment"] == "exact_match"

    assert families["customer_support_router"]["recommended_depth"] == "shallow"
    assert families["customer_support_router"]["alignment"] == "within_success_envelope"

    assert families["data_analyzer"]["recommended_depth"] == "deep"
    assert families["data_analyzer"]["alignment"] == "exact_match"
    assert "parameter_extractor" in families["data_analyzer"]["policy_reason"]

    assert output_path.exists()
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["summary"]["mismatches"] == 0

    summary_path = output_path.with_name(f"{output_path.stem}_summary.md")
    assert summary_path.exists()
    summary_text = summary_path.read_text(encoding="utf-8")
    assert "code_reviewer" in summary_text
    assert "customer_support_router" in summary_text
    assert "data_analyzer" in summary_text
