import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from t01m_host.static_cases import verify_static_cases
from t01m_host.util import OracleError


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["/usr/bin/git", "-C", root, *args],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


class StaticCaseTests(unittest.TestCase):
    def test_exact_git_ledger_and_rebuild_chain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            _git(root, "init", "-q")
            _git(root, "config", "user.email", "fixture@example.invalid")
            _git(root, "config", "user.name", "Fixture")
            _git(root, "commit", "--allow-empty", "-q", "-m", "empty baseline")
            baseline = _git(root, "rev-parse", "HEAD").decode().strip()
            files = {
                "README.md": "构建、安装、离线、私有存储与验证。\n",
                "app/src/main/java/F.java": "class F {}\n",
                "app/src/main/res/values/strings.xml": "<resources/>\n",
                "app/src/test/java/FTest.java": "class FTest {}\n",
            }
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            _git(root, "add", ".")
            _git(root, "commit", "-q", "-m", "implementation")
            accepted = _git(root, "rev-parse", "HEAD").decode().strip()
            diff = _git(root, "diff", "--binary", "--no-ext-diff", baseline, accepted)
            fixture_root = Path(temporary) / "evidence"
            fixture_root.mkdir()

            def write(name: str, value: dict) -> Path:
                path = fixture_root / name
                path.write_text(json.dumps(value), encoding="utf-8")
                return path

            ledger = write(
                "ledger.json",
                {
                    "iterations": [
                        {
                            "parent_sha": baseline,
                            "commit_sha": accepted,
                            "diff_sha256": hashlib.sha256(diff).hexdigest(),
                            "work_item_id": "W1",
                            "assignment_id": "L1",
                            "model_receipts": ["model"],
                            "tool_receipts": [],
                            "author_agent": "Lilies",
                        }
                    ],
                    "first_non_empty_commit": accepted,
                    "isolated_lilies_assignment_id": "L1",
                    "codex_authored_application_implementation": False,
                    "token_usage": [
                        {
                            "assignment_id": "L1",
                            "stage": "implementation",
                            "model": "fixture",
                            "status": "unknown",
                            "tokens": None,
                        }
                    ],
                },
            )
            build = write(
                "build.json",
                {
                    "accepted_commit": accepted,
                    "source_mutation_commit": accepted,
                    "network": False,
                    "build_exit_code": 0,
                    "test_exit_code": 0,
                    "tests": [{"name": "fixture", "result": "pass"}],
                },
            )
            common = {
                "accepted_commit": accepted,
                "clean_source_snapshot": True,
                "build_exit_code": 0,
                "test_exit_code": 0,
                "source_sha256": "1" * 64,
                "toolchain_sha256": "2" * 64,
                "config_sha256": "3" * 64,
                "signing_key_sha256": "4" * 64,
                "signing_certificate_sha256": "5" * 64,
                "apk_sha256": "6" * 64,
            }
            first = write("rebuild-a.json", common)
            second = write(
                "rebuild-b.json",
                {**common, "promoted_artifacts": ["6" * 64 + ".apk"]},
            )
            result = verify_static_cases(
                repository=root,
                accepted_commit=accepted,
                assignment_ledger=ledger,
                build_receipt=build,
                rebuild_receipt_a=first,
                rebuild_receipt_b=second,
                output=fixture_root / "result.json",
            )
            self.assertEqual(result["result"], "pass")
            bad = json.loads(ledger.read_text())
            bad["iterations"][0]["author_agent"] = "Codex"
            ledger.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(OracleError, "non-Lilies"):
                verify_static_cases(
                    repository=root,
                    accepted_commit=accepted,
                    assignment_ledger=ledger,
                    build_receipt=build,
                    rebuild_receipt_a=first,
                    rebuild_receipt_b=second,
                    output=fixture_root / "bad.json",
                )


if __name__ == "__main__":
    unittest.main()
