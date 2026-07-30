import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from t01m_host.constants import ALL_CASE_IDS
from t01m_host.evidence import (
    _json_digest_edges,
    build_and_validate_closure,
    build_evidence_leaves,
    validate_control_graph,
    verify_evidence_leaves,
)
from t01m_host.util import OracleError
from t01m_host.util import canonical_json_bytes, sha256_file
from t01m_host.util import OracleError


class EvidenceTests(unittest.TestCase):
    def test_missing_digest_edge_target_is_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(OracleError, "missing digest edge target"):
                _json_digest_edges(
                    Path(temporary),
                    "fixture.json",
                    {"path": "missing.bin", "sha256": "a" * 64},
                )

    def test_leaves_cover_artifacts_exactly_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "a.txt").write_bytes(b"alpha")
            (artifacts / "nested").mkdir()
            (artifacts / "nested/b.json").write_bytes(b"{}\n")
            index = {
                "a.txt": {"producer_receipt_id": "r1", "case_ids": ["A06"]},
                "nested/b.json": {
                    "producer_receipt_id": "r2",
                    "case_ids": ["A10"],
                },
            }
            build_evidence_leaves(
                artifacts, root / "evidence-leaves.json", index
            )
            report = verify_evidence_leaves(
                root, artifacts, root / "evidence-leaves.json"
            )
            self.assertEqual(report["entry_count"], 2)

    def test_acyclic_closure_binds_immutable_manifest_and_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            artifact = artifacts / "result.json"
            artifact.write_bytes(b"{}\n")
            leaves = {
                "schema_version": 1,
                "artifact_root": "artifacts/",
                "entries": [
                    {
                        "path": "result.json",
                        "size": artifact.stat().st_size,
                        "sha256": sha256_file(artifact),
                        "media_type": "application/json",
                        "producer_receipt_id": "fixture",
                        "case_ids": ["A10"],
                    }
                ],
            }
            leaves_path = root / "evidence-leaves.json"
            leaves_path.write_bytes(canonical_json_bytes(leaves))
            manifest = {
                "schema_version": 1,
                "status": "ready_for_independent_review",
                "accepted_commit": "a" * 40,
                "accepted_tree": "b" * 40,
                "final_apk": {"sha256": "c" * 64},
                "evidence_leaves": {
                    "path": "evidence-leaves.json",
                    "sha256": sha256_file(leaves_path),
                },
                "a10_result": {
                    "path": "artifacts/result.json",
                    "sha256": sha256_file(artifact),
                },
            }
            manifest_path = root / "evidence-manifest.json"
            manifest_path.write_bytes(canonical_json_bytes(manifest))
            review = {
                "evidence_manifest": {
                    "path": "evidence-manifest.json",
                    "sha256": sha256_file(manifest_path),
                },
                "reviewed_evidence_manifest_sha256": sha256_file(manifest_path),
                "reconstructed_case_verdicts": {
                    case: "PASS" for case in ALL_CASE_IDS[:-1]
                },
                "a10_pre_review_verdict": "PASS",
                "blockers": [],
                "independence_attestation": True,
                "fresh_context": True,
                "read_only_review": True,
                "reviewer_identity": "fixture-independent-reviewer",
                "review_context": "fresh fixture context",
                "remaining_predicates_confirmed": [
                    "independent_review",
                    "closure_envelope",
                ],
                "final_verdict": "PASS",
            }
            (root / "independent-review.json").write_bytes(
                canonical_json_bytes(review)
            )
            closure = build_and_validate_closure(
                root, root / "closure-envelope.json"
            )
            self.assertEqual(closure["terminal_verdict"], "PASS")
            self.assertEqual(validate_control_graph(root)["cycle_count"], 0)

    def test_graph_rejects_a_digest_edge_whose_target_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            leaves = {"schema_version": 1, "artifact_root": "artifacts/", "entries": []}
            leaves_path = root / "evidence-leaves.json"
            leaves_path.write_bytes(canonical_json_bytes(leaves))
            manifest = {
                "schema_version": 1,
                "evidence_leaves": {
                    "path": "evidence-leaves.json",
                    "sha256": sha256_file(leaves_path),
                },
                "missing": {
                    "path": "artifacts/absent.json",
                    "sha256": "a" * 64,
                },
            }
            (root / "evidence-manifest.json").write_bytes(
                canonical_json_bytes(manifest)
            )
            with self.assertRaisesRegex(OracleError, "missing digest edge target"):
                validate_control_graph(root)


if __name__ == "__main__":
    unittest.main()
