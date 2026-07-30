import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path

from t01m_host.observability import (
    DaemonObservabilityClient,
    validate_observability_pair,
)
from t01m_host.util import OracleError, canonical_json_bytes


class ObservabilityTests(unittest.TestCase):
    def test_controller_actively_invokes_paired_read_only_client(self):
        source = """#!/usr/bin/env python3
import hashlib
import json
import pathlib
import sys
import time

args = dict(zip(sys.argv[2::2], sys.argv[3::2]))
phase = args["--phase"]
producer = hashlib.sha256(pathlib.Path(sys.argv[0]).read_bytes()).hexdigest()
snapshot = {
    "schema_version": 1,
    "kind": "t01m-daemon-observability-snapshot",
    "phase": phase,
    "run_nonce": args["--run-nonce"],
    "capture_nonce": args["--capture-nonce"],
    "capture_id": "capture-" + phase,
    "captured_at_unix_ns": time.time_ns(),
    "coverage_epoch": {"id": "epoch-1", "started_at_unix_ns": 1},
    "daemon": {"fingerprint_sha256": "b" * 64, "instance_id": "daemon-1"},
    "ledger": {
        "model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "unknown_token_events": 0,
        "tool_calls": 0,
        "cost_microunits": 0,
    },
    "attestation": {
        "complete": True,
        "producer_sha256": producer,
        "read_only": True,
    },
}
sys.stdout.write(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\\n")
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "observer"
            executable.write_text(source, encoding="utf-8")
            os.chmod(executable, 0o700)
            client = DaemonObservabilityClient(executable.resolve(), root)
            before = client.capture("before")
            workload_started = time.time_ns()
            workload_completed = time.time_ns()
            after = client.capture("after")
            report = validate_observability_pair(
                before,
                after,
                workload_started_ns=workload_started,
                workload_completed_ns=workload_completed,
            )
            self.assertEqual(report["result"], "pass")
            self.assertTrue((root / "daemon-observability-before.json").is_file())
            self.assertTrue((root / "daemon-observability-after.json").is_file())

    def _document(
        self,
        phase: str,
        captured_at: int,
        *,
        capture_id: str | None = None,
        capture_nonce: str | None = None,
    ):
        client_sha = "a" * 64
        snapshot = {
            "schema_version": 1,
            "kind": "t01m-daemon-observability-snapshot",
            "phase": phase,
            "run_nonce": "run-1",
            "capture_nonce": capture_nonce or f"nonce-{phase}",
            "capture_id": capture_id or f"capture-{phase}",
            "captured_at_unix_ns": captured_at,
            "coverage_epoch": {"id": "epoch-1", "started_at_unix_ns": 1},
            "daemon": {
                "fingerprint_sha256": "b" * 64,
                "instance_id": "daemon-1",
            },
            "ledger": {
                "model_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "unknown_token_events": 0,
                "tool_calls": 0,
                "cost_microunits": 0,
            },
            "attestation": {
                "complete": True,
                "producer_sha256": client_sha,
                "read_only": True,
            },
        }
        return {
            "schema_version": 1,
            "artifact_path_identity": (
                f"artifacts/daemon-observability-{phase}.json"
            ),
            "client": {
                "path_identity": "explicit --observability-client",
                "sha256": client_sha,
            },
            "host_invocation_started_unix_ns": captured_at - 1,
            "host_invocation_completed_unix_ns": captured_at + 1,
            "raw_snapshot_sha256": hashlib.sha256(
                canonical_json_bytes(snapshot)
            ).hexdigest(),
            "snapshot": snapshot,
        }

    def _rehash(self, document):
        document["raw_snapshot_sha256"] = hashlib.sha256(
            canonical_json_bytes(document["snapshot"])
        ).hexdigest()

    def test_pair_requires_distinct_active_captures_with_unchanged_ledger(self):
        report = validate_observability_pair(
            self._document("before", 10),
            self._document("after", 40),
            workload_started_ns=20,
            workload_completed_ns=30,
        )
        self.assertEqual(report["result"], "pass")

    def test_arbitrary_json_is_rejected(self):
        with self.assertRaises(OracleError):
            validate_observability_pair(
                {"model_calls": 0},
                {"model_calls": 0},
                workload_started_ns=20,
                workload_completed_ns=30,
            )

    def test_same_static_capture_is_rejected(self):
        before = self._document(
            "before", 10, capture_id="same", capture_nonce="same"
        )
        after = self._document(
            "after", 40, capture_id="same", capture_nonce="same"
        )
        with self.assertRaises(OracleError):
            validate_observability_pair(
                before,
                after,
                workload_started_ns=20,
                workload_completed_ns=30,
            )

    def test_changed_ledger_counter_is_rejected(self):
        before = self._document("before", 10)
        after = self._document("after", 40)
        after["snapshot"]["ledger"]["model_calls"] = 1
        self._rehash(after)
        with self.assertRaises(OracleError):
            validate_observability_pair(
                before,
                after,
                workload_started_ns=20,
                workload_completed_ns=30,
            )

    def test_every_ledger_boolean_is_rejected_in_both_captures(self):
        fields = (
            "model_calls",
            "input_tokens",
            "output_tokens",
            "unknown_token_events",
            "tool_calls",
            "cost_microunits",
        )
        for phase in ("before", "after"):
            for field in fields:
                for value in (False, True):
                    with self.subTest(phase=phase, field=field, value=value):
                        before = self._document("before", 10)
                        after = self._document("after", 40)
                        document = before if phase == "before" else after
                        document["snapshot"]["ledger"][field] = value
                        self._rehash(document)
                        with self.assertRaises(OracleError):
                            validate_observability_pair(
                                before,
                                after,
                                workload_started_ns=20,
                                workload_completed_ns=30,
                            )

    def test_capture_identity_booleans_are_rejected(self):
        for phase in ("before", "after"):
            for field in ("run_nonce", "capture_id", "capture_nonce"):
                for value in (False, True):
                    with self.subTest(phase=phase, field=field, value=value):
                        before = self._document("before", 10)
                        after = self._document("after", 40)
                        document = before if phase == "before" else after
                        document["snapshot"][field] = value
                        self._rehash(document)
                        with self.assertRaises(OracleError):
                            validate_observability_pair(
                                before,
                                after,
                                workload_started_ns=20,
                                workload_completed_ns=30,
                            )

    def test_schema_version_booleans_are_rejected(self):
        for phase in ("before", "after"):
            for location in ("snapshot", "wrapper"):
                for value in (False, True):
                    with self.subTest(
                        phase=phase, location=location, value=value
                    ):
                        before = self._document("before", 10)
                        after = self._document("after", 40)
                        document = before if phase == "before" else after
                        if location == "snapshot":
                            document["snapshot"]["schema_version"] = value
                            self._rehash(document)
                        else:
                            document["schema_version"] = value
                        with self.assertRaises(OracleError):
                            validate_observability_pair(
                                before,
                                after,
                                workload_started_ns=20,
                                workload_completed_ns=30,
                            )

    def test_snapshot_and_wrapper_timestamp_booleans_are_rejected(self):
        fields = (
            ("snapshot", "captured_at_unix_ns"),
            ("snapshot", "coverage_epoch.started_at_unix_ns"),
            ("wrapper", "host_invocation_started_unix_ns"),
            ("wrapper", "host_invocation_completed_unix_ns"),
        )
        for phase in ("before", "after"):
            for location, field in fields:
                for value in (False, True):
                    with self.subTest(
                        phase=phase, location=location, field=field, value=value
                    ):
                        before = self._document("before", 10)
                        after = self._document("after", 40)
                        document = before if phase == "before" else after
                        if field == "coverage_epoch.started_at_unix_ns":
                            document["snapshot"]["coverage_epoch"][
                                "started_at_unix_ns"
                            ] = value
                            self._rehash(document)
                        elif location == "snapshot":
                            document["snapshot"][field] = value
                            self._rehash(document)
                        else:
                            document[field] = value
                        with self.assertRaises(OracleError):
                            validate_observability_pair(
                                before,
                                after,
                                workload_started_ns=20,
                                workload_completed_ns=30,
                            )

    def test_workload_timestamp_booleans_are_rejected(self):
        for field in ("workload_started_ns", "workload_completed_ns"):
            for value in (False, True):
                with self.subTest(field=field, value=value):
                    arguments = {
                        "workload_started_ns": 20,
                        "workload_completed_ns": 30,
                    }
                    arguments[field] = value
                    with self.assertRaises(OracleError):
                        validate_observability_pair(
                            self._document("before", 10),
                            self._document("after", 40),
                            **arguments,
                        )

    def test_snapshot_must_strictly_bracket_workload(self):
        with self.assertRaises(OracleError):
            validate_observability_pair(
                self._document("before", 20),
                self._document("after", 40),
                workload_started_ns=20,
                workload_completed_ns=30,
            )


if __name__ == "__main__":
    unittest.main()
