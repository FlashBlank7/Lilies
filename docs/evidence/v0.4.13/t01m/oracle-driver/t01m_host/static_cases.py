"""Fail-closed A01-A04 baseline, ledger, build/test and reproducibility checks."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .commands import run
from .constants import GIT
from .util import OracleError, canonical_json_bytes, sha256_file, write_new_or_replace

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise OracleError(f"expected JSON object: {path}")
    return value


def _git(repository: Path, *args: str) -> bytes:
    return run([GIT, "-C", repository.resolve(), *args], timeout=300.0).stdout


def _text(repository: Path, *args: str) -> str:
    return _git(repository, *args).decode("utf-8", errors="strict").strip()


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise OracleError(f"{label} is not a full lowercase Git SHA")
    return value


def _nonempty_commits(repository: Path, baseline: str, accepted: str) -> list[dict[str, str]]:
    commits = _text(repository, "rev-list", "--reverse", f"{baseline}..{accepted}").splitlines()
    output = []
    for commit in commits:
        parent = _text(repository, "rev-parse", f"{commit}^")
        changed = _text(repository, "diff-tree", "--no-commit-id", "--name-only", "-r", parent, commit)
        if not changed:
            continue
        diff = _git(repository, "diff", "--binary", "--no-ext-diff", parent, commit)
        output.append(
            {
                "parent_sha": parent,
                "commit_sha": commit,
                "diff_sha256": hashlib.sha256(diff).hexdigest(),
            }
        )
    return output


def verify_static_cases(
    *,
    repository: Path,
    accepted_commit: str,
    assignment_ledger: Path,
    build_receipt: Path,
    rebuild_receipt_a: Path,
    rebuild_receipt_b: Path,
    output: Path,
) -> dict[str, Any]:
    """Verify declared evidence; this never mutates or builds the supplied repository."""
    repository = repository.resolve()
    accepted_commit = _require_sha(accepted_commit, "accepted_commit")
    if _text(repository, "rev-parse", "HEAD") != accepted_commit:
        raise OracleError("repository HEAD is not the accepted commit")
    if _text(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise OracleError("accepted repository is not clean")
    roots = _text(repository, "rev-list", "--max-parents=0", accepted_commit).splitlines()
    if len(roots) != 1:
        raise OracleError("history does not have exactly one reachable baseline root")
    baseline = roots[0]
    baseline_object = _text(repository, "cat-file", "-p", baseline)
    if re.search(r"^parent ", baseline_object, re.MULTILINE):
        raise OracleError("baseline commit unexpectedly has a parent")
    baseline_tree = _text(repository, "rev-parse", f"{baseline}^{{tree}}")
    if baseline_tree != EMPTY_TREE:
        raise OracleError("baseline tree is not the canonical empty tree")
    if _text(repository, "ls-tree", "-r", "--name-only", baseline):
        raise OracleError("baseline has tracked files")
    nonempty = _nonempty_commits(repository, baseline, accepted_commit)
    if not nonempty:
        raise OracleError("history has no non-empty implementation iteration")

    ledger = _load(assignment_ledger)
    iterations = ledger.get("iterations")
    if not isinstance(iterations, list) or len(iterations) != len(nonempty):
        raise OracleError("ledger does not cover every non-empty iteration exactly once")
    normalized = []
    for item in iterations:
        if not isinstance(item, dict):
            raise OracleError("ledger iteration is not an object")
        required = {
            "parent_sha", "commit_sha", "diff_sha256", "work_item_id",
            "assignment_id", "model_receipts", "tool_receipts", "author_agent",
        }
        if any(key not in item for key in required):
            raise OracleError("ledger iteration lacks mandatory attribution fields")
        if item["author_agent"] != "Lilies":
            raise OracleError("non-Lilies implementation iteration is present")
        if not item["work_item_id"] or not item["assignment_id"]:
            raise OracleError("iteration lacks work-item/assignment identity")
        if not isinstance(item["model_receipts"], list) or not item["model_receipts"]:
            raise OracleError("iteration lacks model receipt")
        if not isinstance(item["tool_receipts"], list):
            raise OracleError("iteration tool receipts are not a list")
        normalized.append(
            {key: item[key] for key in ("parent_sha", "commit_sha", "diff_sha256")}
        )
    if normalized != nonempty:
        raise OracleError("ledger parent/commit/diff chain mismatches Git history")
    if ledger.get("first_non_empty_commit") != nonempty[0]["commit_sha"]:
        raise OracleError("ledger does not bind the first non-empty commit")
    if not ledger.get("isolated_lilies_assignment_id"):
        raise OracleError("first implementation lacks isolated Lilies assignment")
    if ledger.get("codex_authored_application_implementation") is not False:
        raise OracleError("ledger does not exclude Codex-authored implementation")
    usage = ledger.get("token_usage")
    if not isinstance(usage, list) or not usage:
        raise OracleError("token usage ledger is empty")
    for entry in usage:
        if not isinstance(entry, dict) or any(
            key not in entry for key in ("assignment_id", "stage", "model", "status", "tokens")
        ):
            raise OracleError("token usage row is incomplete")
        if entry["status"] == "unknown":
            if entry["tokens"] is not None:
                raise OracleError("unknown token usage must remain null, never estimated")
        elif entry["status"] == "measured":
            if not isinstance(entry["tokens"], int) or entry["tokens"] < 0:
                raise OracleError("measured token usage is invalid")
        else:
            raise OracleError("token usage status must be measured or unknown")

    build = _load(build_receipt)
    if (
        build.get("accepted_commit") != accepted_commit
        or build.get("source_mutation_commit") != accepted_commit
        or build.get("network") is not False
        or build.get("build_exit_code") != 0
        or build.get("test_exit_code") != 0
    ):
        raise OracleError("offline build/test receipt does not bind final source mutation")
    tests = build.get("tests")
    if not isinstance(tests, list) or not tests or any(
        not isinstance(item, dict)
        or not item.get("name")
        or item.get("result") != "pass"
        for item in tests
    ):
        raise OracleError("build receipt lacks named passing deterministic tests")
    tracked = _text(repository, "ls-tree", "-r", "--name-only", accepted_commit).splitlines()
    if (
        "README.md" not in tracked
        or not any(path.endswith((".kt", ".java")) for path in tracked)
        or not any("/res/" in f"/{path}" for path in tracked)
        or not any("test" in path.casefold() and path.endswith((".kt", ".java")) for path in tracked)
    ):
        raise OracleError("tracked Android source/resource/test/README deliverables incomplete")
    readme = _git(repository, "show", f"{accepted_commit}:README.md").decode("utf-8", errors="strict")
    stripped = readme.lstrip()
    if not stripped or not re.match(r"[\u3400-\u9fff]", stripped):
        raise OracleError("README is not Chinese-first")
    for clause in ("构建", "安装", "离线", "私有", "验证"):
        if clause not in readme:
            raise OracleError(f"README lacks required Chinese clause: {clause}")

    rebuilds = [_load(rebuild_receipt_a), _load(rebuild_receipt_b)]
    common_keys = (
        "source_sha256", "toolchain_sha256", "config_sha256", "signing_key_sha256",
        "signing_certificate_sha256",
    )
    for index, receipt in enumerate(rebuilds, 1):
        if (
            receipt.get("accepted_commit") != accepted_commit
            or receipt.get("clean_source_snapshot") is not True
            or receipt.get("build_exit_code") != 0
            or receipt.get("test_exit_code") != 0
        ):
            raise OracleError(f"rebuild receipt {index} is not a clean successful build+test")
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("apk_sha256", ""))):
            raise OracleError(f"rebuild receipt {index} lacks APK SHA-256")
    if any(rebuilds[0].get(key) != rebuilds[1].get(key) for key in common_keys):
        raise OracleError("rebuild inputs differ")
    if rebuilds[0]["apk_sha256"] != rebuilds[1]["apk_sha256"]:
        raise OracleError("two clean rebuild APK digests differ")
    promoted = rebuilds[1].get("promoted_artifacts")
    expected_name = rebuilds[1]["apk_sha256"] + ".apk"
    if promoted != [expected_name]:
        raise OracleError("promotion is not exactly one content-addressed APK")

    results = {
        "A01": {
            "baseline_commit": baseline,
            "baseline_tree": baseline_tree,
            "tracked_file_count": 0,
            "first_non_empty_commit": nonempty[0]["commit_sha"],
            "result": "pass",
        },
        "A02": {
            "ledger_sha256": sha256_file(assignment_ledger),
            "iteration_count": len(nonempty),
            "codex_authored_application_implementation": False,
            "result": "pass",
        },
        "A03": {
            "build_receipt_sha256": sha256_file(build_receipt),
            "accepted_commit": accepted_commit,
            "named_test_count": len(tests),
            "repository_clean": True,
            "result": "pass",
        },
        "A04": {
            "rebuild_receipt_sha256": [
                sha256_file(rebuild_receipt_a),
                sha256_file(rebuild_receipt_b),
            ],
            "apk_sha256": rebuilds[0]["apk_sha256"],
            "promoted_artifact": expected_name,
            "result": "pass",
        },
    }
    document = {
        "schema_version": 1,
        "accepted_commit": accepted_commit,
        "results": results,
        "result": "pass",
    }
    write_new_or_replace(output, canonical_json_bytes(document))
    return document
