from __future__ import annotations

from scripts.frontend_verification_runner import probe_node_environment, run_frontend_verification


def test_v02_86_probe_discovers_usable_node_environment() -> None:
    probe = probe_node_environment()

    assert probe["package_json_present"] is True
    assert probe["node_modules_present"] is True
    assert probe["node_available"] is True
    assert probe["npm_available"] is True
    assert probe["selected_node_bin"]


def test_v02_86_frontend_verification_runner_passes() -> None:
    result = run_frontend_verification()

    assert result["status"] == "completed"
    assert result["passed"] is True
    checks = {check["command"]: check for check in result["checks"]}
    assert checks["npm run lint"]["passed"] is True
    assert checks["node_modules/.bin/tsc --noEmit"]["passed"] is True
