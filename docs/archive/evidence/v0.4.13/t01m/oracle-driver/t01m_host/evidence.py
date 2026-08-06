import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Iterable

from .constants import (
    ALL_CASE_IDS,
    BRIEF_ID,
    CONTROL_FILES,
    ORACLE_ID,
    REQUIRED_ARTIFACT_DIRECTORIES,
    REQUIRED_ARTIFACT_FILES,
    REQUIRED_SCREENSHOTS,
    TASK_ID,
)
from .util import (
    OracleError,
    canonical_json_bytes,
    normalized_relative_path,
    require_hex_digest,
    sha256_file,
    write_new_or_replace,
)


def _load(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise OracleError(f"JSON control file must contain an object: {path}")
    return value


def _media_type(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".png":
        return "image/png"
    if path.suffix == ".txt":
        return "text/plain; charset=utf-8"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def build_evidence_leaves(
    artifacts: Path,
    destination: Path,
    artifact_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if destination.resolve().parent == artifacts.resolve():
        raise OracleError("evidence-leaves.json must be outside artifacts/")
    entries = []
    found = []
    for path in sorted(artifacts.rglob("*")):
        if path.is_symlink():
            raise OracleError(f"symlinks are forbidden below artifacts/: {path}")
        if not path.is_file():
            continue
        relative = normalized_relative_path(artifacts, path)
        if Path(relative).name in CONTROL_FILES:
            raise OracleError(f"control file is forbidden below artifacts/: {relative}")
        found.append(relative)
        annotation = artifact_index.get(relative)
        if not isinstance(annotation, dict):
            raise OracleError(f"missing artifact-index annotation: {relative}")
        receipt = annotation.get("producer_receipt_id")
        cases = annotation.get("case_ids")
        if not isinstance(receipt, str) or not receipt:
            raise OracleError(f"invalid producer receipt id: {relative}")
        if (
            not isinstance(cases, list)
            or not cases
            or any(case not in ALL_CASE_IDS for case in cases)
            or len(cases) != len(set(cases))
        ):
            raise OracleError(f"invalid case coverage: {relative}")
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "media_type": _media_type(path),
                "producer_receipt_id": receipt,
                "case_ids": sorted(cases),
            }
        )
    extras = sorted(set(artifact_index) - set(found))
    if extras:
        raise OracleError(f"artifact-index contains absent paths: {extras}")
    document = {
        "schema_version": 1,
        "artifact_root": "artifacts/",
        "entries": entries,
    }
    write_new_or_replace(destination, canonical_json_bytes(document))
    return document


def verify_evidence_leaves(
    root: Path, artifacts: Path, leaves_path: Path
) -> dict[str, Any]:
    leaves = _load(leaves_path)
    entries = leaves.get("entries")
    if leaves.get("schema_version") != 1 or not isinstance(entries, list):
        raise OracleError("invalid evidence-leaves schema")
    expected_paths = []
    seen = set()
    for entry in entries:
        path_text = entry.get("path")
        if not isinstance(path_text, str) or path_text in seen:
            raise OracleError("duplicate or invalid leaf path")
        seen.add(path_text)
        path = artifacts / path_text
        normalized_relative_path(artifacts, path)
        if not path.is_file() or path.is_symlink():
            raise OracleError(f"missing or non-regular leaf: {path_text}")
        if entry.get("size") != path.stat().st_size:
            raise OracleError(f"leaf size mismatch: {path_text}")
        if require_hex_digest(entry.get("sha256"), path_text) != sha256_file(path):
            raise OracleError(f"leaf digest mismatch: {path_text}")
        if not entry.get("producer_receipt_id") or not entry.get("case_ids"):
            raise OracleError(f"leaf lacks producer/case coverage: {path_text}")
        expected_paths.append(path_text)
    actual_paths = [
        normalized_relative_path(artifacts, path)
        for path in sorted(artifacts.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]
    if expected_paths != sorted(expected_paths) or expected_paths != actual_paths:
        raise OracleError("evidence leaves do not cover artifacts exactly once")
    return {
        "entry_count": len(entries),
        "evidence_leaves_sha256": sha256_file(leaves_path),
        "result": "pass",
    }


def build_evidence_manifest(
    root: Path,
    metadata: dict[str, Any],
    destination: Path,
) -> dict[str, Any]:
    leaves_path = root / "evidence-leaves.json"
    artifacts = root / "artifacts"
    verification = verify_evidence_leaves(root, artifacts, leaves_path)
    absent_files = [
        relative
        for relative in REQUIRED_ARTIFACT_FILES
        if not (artifacts / relative).is_file()
    ]
    absent_directories = [
        relative
        for relative in REQUIRED_ARTIFACT_DIRECTORIES
        if not (artifacts / relative).is_dir()
        or not any(path.is_file() for path in (artifacts / relative).rglob("*"))
    ]
    absent_screenshots = [
        name
        for name in REQUIRED_SCREENSHOTS
        if not (artifacts / "screenshots" / name).is_file()
    ]
    if absent_files or absent_directories or absent_screenshots:
        raise OracleError(
            "required artifact coverage incomplete: "
            f"files={absent_files}, dirs={absent_directories}, "
            f"screenshots={absent_screenshots}"
        )
    required = {
        "baseline_id",
        "baseline_digest",
        "toolchain_id",
        "toolchain_digest",
        "assignment_id",
        "assignment_digest",
        "accepted_commit",
        "accepted_tree",
        "final_apk_path",
        "final_apk_sha256",
        "case_results",
        "a10_pre_review",
    }
    missing = sorted(required - set(metadata))
    if missing:
        raise OracleError(f"manifest metadata missing fields: {missing}")
    case_results = metadata["case_results"]
    if not isinstance(case_results, dict) or set(case_results) != set(ALL_CASE_IDS[:-1]):
        raise OracleError("manifest must bind exact A01-A09 result set")
    references = []
    leaf_by_path = {
        entry["path"]: entry for entry in _load(leaves_path)["entries"]
    }
    for case, item in sorted(case_results.items()):
        path_text = item.get("path")
        digest = item.get("sha256")
        if path_text not in leaf_by_path or leaf_by_path[path_text]["sha256"] != digest:
            raise OracleError(f"{case} result is not bound to an exact evidence leaf")
        result_document = _load(artifacts / path_text)
        if result_document.get("result") != "pass":
            raise OracleError(f"{case} result leaf does not report pass")
        references.append({"case_id": case, "path": f"artifacts/{path_text}", "sha256": digest})
    pre_review = metadata["a10_pre_review"]
    normalized_pre_review = {}
    for name, item in sorted(pre_review.items()):
        path_text = item.get("path")
        digest = item.get("sha256")
        if path_text not in leaf_by_path or leaf_by_path[path_text]["sha256"] != digest:
            raise OracleError(f"A10 pre-review input is not an exact evidence leaf: {name}")
        normalized_pre_review[name] = {
            "path": f"artifacts/{path_text}",
            "sha256": digest,
        }
    final_apk_text = metadata["final_apk_path"]
    if (
        not isinstance(final_apk_text, str)
        or Path(final_apk_text).is_absolute()
        or any(part in ("", ".", "..") for part in final_apk_text.split("/"))
        or "\\" in final_apk_text
    ):
        raise OracleError("final APK path must be normalized below evidence root")
    apk_path = root / final_apk_text
    if (
        not apk_path.is_file()
        or apk_path.is_symlink()
        or normalized_relative_path(root, apk_path) != final_apk_text
        or require_hex_digest(
            metadata["final_apk_sha256"], "final_apk_sha256"
        )
        != sha256_file(apk_path)
    ):
        raise OracleError("final APK binding does not verify")
    for label in ("accepted_commit", "accepted_tree"):
        if not isinstance(metadata[label], str) or not re.fullmatch(
            r"[0-9a-f]{40}", metadata[label]
        ):
            raise OracleError(f"{label} must be a full lowercase Git SHA")
    document = {
        "schema_version": 1,
        "task_id": TASK_ID,
        "oracle_id": ORACLE_ID,
        "brief_id": BRIEF_ID,
        "status": "ready_for_independent_review",
        "baseline": {
            "id": metadata["baseline_id"],
            "sha256": require_hex_digest(metadata["baseline_digest"], "baseline_digest"),
        },
        "toolchain": {
            "id": metadata["toolchain_id"],
            "sha256": require_hex_digest(metadata["toolchain_digest"], "toolchain_digest"),
        },
        "assignment": {
            "id": metadata["assignment_id"],
            "sha256": require_hex_digest(metadata["assignment_digest"], "assignment_digest"),
        },
        "accepted_commit": metadata["accepted_commit"],
        "accepted_tree": metadata["accepted_tree"],
        "final_apk": {
            "path": metadata["final_apk_path"],
            "size": apk_path.stat().st_size,
            "sha256": metadata["final_apk_sha256"],
        },
        "evidence_leaves": {
            "path": "evidence-leaves.json",
            "sha256": verification["evidence_leaves_sha256"],
        },
        "a01_a09_results": references,
        "a10_pre_review": normalized_pre_review,
        "required_artifact_coverage": {
            "entry_count": verification["entry_count"],
            "result": "pass",
        },
    }
    serialized = canonical_json_bytes(document)
    forbidden = (
        b"evidence-manifest.json",
        b"independent-review.json",
        b"closure-envelope.json",
    )
    if any(name in serialized for name in forbidden):
        raise OracleError("evidence manifest contains a self or forward reference")
    write_new_or_replace(destination, serialized)
    return document


def _json_digest_edges(root: Path, source_name: str, value: Any) -> list[tuple[str, str]]:
    edges = []
    if isinstance(value, dict):
        if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
            target = value["path"]
            candidate = root / target
            if candidate.is_file() and sha256_file(candidate) == value["sha256"]:
                edges.append((source_name, target))
            elif candidate.is_file():
                raise OracleError(f"digest edge mismatch: {source_name} -> {target}")
            else:
                raise OracleError(f"missing digest edge target: {source_name} -> {target}")
        for child in value.values():
            edges.extend(_json_digest_edges(root, source_name, child))
    elif isinstance(value, list):
        for child in value:
            edges.extend(_json_digest_edges(root, source_name, child))
    return edges


def validate_control_graph(root: Path) -> dict[str, Any]:
    leaves = _load(root / "evidence-leaves.json")
    manifest = _load(root / "evidence-manifest.json")
    review_path = root / "independent-review.json"
    closure_path = root / "closure-envelope.json"
    documents = {
        "evidence-leaves.json": leaves,
        "evidence-manifest.json": manifest,
    }
    if review_path.is_file():
        documents["independent-review.json"] = _load(review_path)
    if closure_path.is_file():
        documents["closure-envelope.json"] = _load(closure_path)
    edges = [
        (
            "evidence-leaves.json",
            "artifacts/" + entry["path"],
        )
        for entry in leaves.get("entries", [])
    ]
    for entry in leaves.get("entries", []):
        target = root / "artifacts" / entry["path"]
        if not target.is_file() or sha256_file(target) != entry["sha256"]:
            raise OracleError(f"invalid leaf graph target: {entry.get('path')}")
    for source, document in documents.items():
        if source != "evidence-leaves.json":
            edges.extend(_json_digest_edges(root, source, document))
    permitted = {
        "evidence-leaves.json": lambda target: target.startswith("artifacts/"),
        "evidence-manifest.json": lambda target: target == "evidence-leaves.json"
        or target.startswith("artifacts/"),
        "independent-review.json": lambda target: target == "evidence-manifest.json",
        "closure-envelope.json": lambda target: target
        in {"evidence-manifest.json", "independent-review.json"},
    }
    seen = set()
    graph: dict[str, list[str]] = {}
    for edge in edges:
        if edge in seen:
            raise OracleError(f"duplicate digest edge: {edge}")
        seen.add(edge)
        source, target = edge
        if source == target or not permitted[source](target):
            raise OracleError(f"forbidden graph level edge: {source} -> {target}")
        if target in CONTROL_FILES and target not in documents:
            raise OracleError(f"missing graph target: {target}")
        graph.setdefault(source, []).append(target)
    required_control_edges = {
        "evidence-manifest.json": {"evidence-leaves.json"},
    }
    if "independent-review.json" in documents:
        required_control_edges["independent-review.json"] = {"evidence-manifest.json"}
    if "closure-envelope.json" in documents:
        required_control_edges["closure-envelope.json"] = {
            "evidence-manifest.json",
            "independent-review.json",
        }
    for source, required_targets in required_control_edges.items():
        if not required_targets.issubset(set(graph.get(source, []))):
            raise OracleError(f"{source} lacks required control-file digest edges")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise OracleError(f"evidence graph cycle at {node}")
        if node in visited:
            return
        visiting.add(node)
        for child in graph.get(node, []):
            if child in CONTROL_FILES:
                visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in documents:
        visit(node)
    return {"edge_count": len(edges), "cycle_count": 0, "result": "pass"}


def build_and_validate_closure(root: Path, destination: Path) -> dict[str, Any]:
    manifest_path = root / "evidence-manifest.json"
    review_path = root / "independent-review.json"
    manifest = _load(manifest_path)
    review = _load(review_path)
    manifest_digest = sha256_file(manifest_path)
    review_digest = sha256_file(review_path)
    if manifest.get("status") != "ready_for_independent_review":
        raise OracleError("manifest status is not ready_for_independent_review")
    review_manifest = review.get("evidence_manifest", {})
    reviewed_digest = review.get(
        "reviewed_evidence_manifest_sha256", review_manifest.get("sha256")
    )
    if reviewed_digest != manifest_digest:
        raise OracleError("independent review is not bound to exact manifest")
    if review_manifest != {
        "path": "evidence-manifest.json",
        "sha256": manifest_digest,
    }:
        raise OracleError("independent review needs the permitted manifest digest edge")
    if review.get("final_verdict") != "PASS" or review.get("blockers") != []:
        raise OracleError("independent review is not zero-blocker PASS")
    if (
        review.get("independence_attestation") is not True
        or review.get("fresh_context") is not True
        or review.get("read_only_review") is not True
    ):
        raise OracleError("independent review lacks fresh read-only independence")
    if review.get("fresh_context") is not True:
        raise OracleError("independent review was not performed in a fresh context")
    if review.get("read_only_review") is not True:
        raise OracleError("independent review was not read-only")
    if (
        not isinstance(review.get("reviewer_identity"), str)
        or not review["reviewer_identity"]
        or not isinstance(review.get("review_context"), str)
        or not review["review_context"]
    ):
        raise OracleError("independent review lacks fresh reviewer identity/context")
    if review.get("remaining_predicates_confirmed") != [
        "independent_review",
        "closure_envelope",
    ]:
        raise OracleError("independent review did not confirm the exact remaining steps")
    verdicts = review.get("reconstructed_case_verdicts")
    if not isinstance(verdicts, dict) or verdicts != {
        case: "PASS" for case in ALL_CASE_IDS[:-1]
    }:
        raise OracleError("independent review did not reconstruct exact A01-A09 PASS")
    if review.get("a10_pre_review_verdict") != "PASS":
        raise OracleError("independent review did not pass A10 pre-review")
    # Validate the complete pre-closure graph before a terminal PASS file exists.
    preclosure_graph = validate_control_graph(root)
    if preclosure_graph["result"] != "pass":
        raise OracleError("pre-closure evidence graph validation failed")
    document = {
        "schema_version": 1,
        "task_id": TASK_ID,
        "oracle_id": ORACLE_ID,
        "brief_id": BRIEF_ID,
        "accepted_commit": manifest["accepted_commit"],
        "accepted_tree": manifest["accepted_tree"],
        "final_apk_sha256": manifest["final_apk"]["sha256"],
        "evidence_manifest": {
            "path": "evidence-manifest.json",
            "sha256": manifest_digest,
        },
        "independent_review": {
            "path": "independent-review.json",
            "sha256": review_digest,
        },
        "a01_a09_pass_ids": ALL_CASE_IDS[:-1],
        "a10_predicates": {
            "pre_review": "PASS",
            "independent_review": "PASS",
            "closure_envelope": "PASS",
        },
        "a10_verdict": "PASS",
        "pass_case_ids": ALL_CASE_IDS,
        "zero_blockers": True,
        "terminal_verdict": "PASS",
    }
    serialized = canonical_json_bytes(document)
    if b"closure-envelope.json" in serialized:
        raise OracleError("closure envelope must not hash itself")
    expected_edges = _json_digest_edges(root, "closure-envelope.json", document)
    if set(expected_edges) != {
        ("closure-envelope.json", "evidence-manifest.json"),
        ("closure-envelope.json", "independent-review.json"),
    }:
        raise OracleError("closure envelope has an incomplete or extra digest edge")
    write_new_or_replace(destination, serialized)
    graph = validate_control_graph(root)
    if graph["result"] != "pass":
        raise OracleError("closure graph validation failed")
    return document
