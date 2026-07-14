from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
  module_path = ROOT / "scripts" / "v03_53_markdown_result_renderer.py"
  spec = importlib.util.spec_from_file_location("v03_53_markdown_result_renderer_under_test", module_path)
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


def test_v03_53_markdown_module_is_reusable_and_click_openable() -> None:
  module = load_audit_module()
  check = module.markdown_module_checks()
  assert check["passed"] is True
  assert check["cases"]["exports_reusable_document"] is True
  assert check["cases"]["exports_reusable_result_card"] is True
  assert check["cases"]["has_click_open_dialog"] is True


def test_v03_53_markdown_renderer_avoids_raw_html_injection() -> None:
  module = load_audit_module()
  check = module.markdown_safety_checks()
  assert check["passed"] is True
  assert check["cases"]["does_not_use_dangerously_set_inner_html"] is True
  assert check["cases"]["links_are_sanitized"] is True


def test_v03_53_try_run_result_uses_markdown_rendering() -> None:
  module = load_audit_module()
  check = module.workflow_run_integration_checks()
  assert check["passed"] is True
  assert check["cases"]["customer_result_uses_markdown_card"] is True
  assert check["cases"]["try_run_result_uses_markdown_card"] is True
  assert check["cases"]["old_primary_run_output_pre_removed"] is True


def test_v03_53_frontend_copy_and_styles_are_present() -> None:
  module = load_audit_module()
  assert module.frontend_copy_checks()["passed"] is True
  style_check = module.frontend_style_checks()
  assert style_check["passed"] is True
  assert style_check["cases"]["dialog_is_fixed_width_independent"] is True


def test_v03_53_regression_manifest_contains_current_test() -> None:
  module = load_audit_module()
  check = module.regression_manifest_check()
  assert check["passed"] is True
  assert check["cases"]["v0353_test_in_command"] is True
  assert check["cases"]["pass_count_not_less_than_v0353_floor"] is True


def test_v03_53_static_evidence_passes_and_writes_json(tmp_path: Path) -> None:
  module = load_audit_module()
  evidence = module.build_evidence()
  assert evidence["status"] == "passed"
  output = tmp_path / "evidence.json"
  module.write_evidence(output, evidence)
  loaded = json.loads(output.read_text(encoding="utf-8"))
  assert loaded["version"] == "v0.3.53"
  assert loaded["summary"]["open_p0_p1_bug_count"] == 0
