from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from agent_platform.forbidden_assistance_scanner import (
    _input_binding,
    derive_source_semantic_input,
    evaluate_source_semantic_input,
)
from agent_platform.formal_source_provenance import (
    ApprovedDeveloperResponseBinding,
    FormalSourceProvenanceArchive,
    FormalSourceProvenanceCoordinator,
)


NOW = datetime(2026, 7, 24, 2, 0, tzinfo=timezone.utc)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
PUBLIC_SUMMARY_DIGEST = "sha256:" + "d" * 64
TASK_PACKAGE = {
    "task": {
        "task_id": "EXP-LILIES-001",
        "revision": 1,
        "source_projects": [
            {
                "name": "Paperless-ngx",
                "repository_url": "https://github.com/paperless-ngx/paperless-ngx.git",
            },
            {
                "name": "InvenTree",
                "repository_url": "git@github.com:inventree/InvenTree.git",
            },
        ],
    },
    "fixtures": {
        "files": [
            {"path": "public-inputs/invoice-001.pdf"},
        ],
    },
    "fixture_identifiers": [
        "PO-0042",
        "po",
        "purchase_order",
        "supplier",
        "vendor",
    ],
    "public_summary_digest": PUBLIC_SUMMARY_DIGEST,
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _binding(
    *,
    channel_id: UUID,
    commit_sha: str,
) -> ApprovedDeveloperResponseBinding:
    return ApprovedDeveloperResponseBinding(
        channel_id=channel_id,
        report_id=uuid4(),
        approval_id=uuid4(),
        approval_message_id=uuid4(),
        approval_message_seq=10,
        approval_authority="user",
        approval_payload_digest=DIGEST_A,
        approved_report_revision=4,
        response_id=uuid4(),
        response_message_id=uuid4(),
        response_message_seq=12,
        response_report_revision=5,
        response_payload_digest=DIGEST_B,
        commit_sha=commit_sha,
    )


def _source_archive(
    tmp_path: Path,
    *,
    changed_source: str,
) -> FormalSourceProvenanceArchive:
    repository = tmp_path / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "scanner@example.invalid")
    _git(repository, "config", "user.name", "Scanner Test")
    source_path = repository / "platform/backend/src/agent_platform/generic.py"
    _write(source_path, "VALUE = 1\n")
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "baseline")

    assignment_id = uuid4()
    channel_id = uuid4()
    coordinator = FormalSourceProvenanceCoordinator(
        repository_root=repository,
        state_root=tmp_path / "source-provenance-state",
    )
    coordinator.freeze_baseline(
        task_id="EXP-LILIES-001",
        task_revision=1,
        run_id="formal-run:source-semantic",
        assignment_id=assignment_id,
        channel_id=channel_id,
        captured_at=NOW,
    )
    _write(source_path, changed_source)
    _git(repository, "add", "--all")
    _git(repository, "commit", "-m", "approved developer change")
    binding = _binding(
        channel_id=channel_id,
        commit_sha=_git(repository, "rev-parse", "HEAD"),
    )
    coordinator.record_approved_response(
        assignment_id=assignment_id,
        binding=binding,
    )
    return coordinator.finalize_archive(
        assignment_id=assignment_id,
        expected_bindings=[binding],
        finalized_at=NOW,
    )


def test_raw_source_rejects_task_specific_adapter_and_final_graph_despite_harmless_response(
    tmp_path: Path,
) -> None:
    harmless_developer_response = {
        "generic_capability_changes": [
            "Improved reusable registry validation.",
        ],
        "tests_run": ["pytest -q tests/test_registry.py"],
    }
    assert "workflow" not in repr(harmless_developer_response).casefold()
    archive = _source_archive(
        tmp_path,
        changed_source=(
            "VALUE = 2\n"
            'TASK_SCOPE = "EXP-LILIES-001"\n'
            "PAPERLESS_NGX_ADAPTER = {\n"
            '    "invoice-001": {"field_mapping": {"supplier": "vendor"}},\n'
            "}\n"
            'FINAL = WorkflowSpec(nodes=[{"id": "extract"}], edges=[])\n'
        ),
    )

    semantic_input = derive_source_semantic_input(
        task_package=TASK_PACKAGE,
        source_manifest=archive.manifest,
        source_files=archive.files,
    )
    online = evaluate_source_semantic_input(
        task_package=TASK_PACKAGE,
        source_manifest=archive.manifest,
        source_files=archive.files,
    )
    replay = evaluate_source_semantic_input(
        task_package=TASK_PACKAGE,
        source_manifest=archive.manifest,
        source_files=archive.files,
        archived_input=semantic_input,
    )

    assert online == replay
    assert {finding.rule_id for finding in replay} == {
        "developer_authored_final_workflow_source",
        "developer_task_specific_source_assistance",
    }
    assert {finding.outcome for finding in replay} == {"violation"}
    assert semantic_input.patch_authority == (
        "display_only_git_blob_tree_authoritative"
    )
    assert {
        item.authority for item in semantic_input.files if item.kind == "binary_patch"
    } == {"display_only_patch"}
    assert {
        item.authority for item in semantic_input.files if item.kind == "git_blob"
    } == {"authoritative_git_blob"}
    assert _input_binding(
        kind="source_semantic",
        archive_path="scanner-inputs/source-semantic.json",
        value=semantic_input.model_dump(mode="json"),
    ).complete


def test_generic_platform_source_with_adapter_and_schema_words_passes(
    tmp_path: Path,
) -> None:
    archive = _source_archive(
        tmp_path,
        changed_source=(
            "VALUE = 2\n\n"
            "class RegistrySchemaAdapter:\n"
            "    def validate(self, payload: object) -> bool:\n"
            "        return payload is not None\n"
        ),
    )
    semantic_input = derive_source_semantic_input(
        task_package=TASK_PACKAGE,
        source_manifest=archive.manifest,
        source_files=archive.files,
    )

    assert (
        evaluate_source_semantic_input(
            task_package=TASK_PACKAGE,
            source_manifest=archive.manifest,
            source_files=archive.files,
            archived_input=semantic_input,
        )
        == []
    )


def test_project_specific_function_and_implicit_field_mapping_are_rejected(
    tmp_path: Path,
) -> None:
    project_specific = _source_archive(
        tmp_path / "project",
        changed_source=(
            "VALUE = 2\n\n"
            "def paperless_to_inventree(invoice):\n"
            '    return {"supplier": invoice["vendor"], '
            '"purchase_order": invoice["po"]}\n'
        ),
    )
    project_findings = evaluate_source_semantic_input(
        task_package=TASK_PACKAGE,
        source_manifest=project_specific.manifest,
        source_files=project_specific.files,
    )
    assert [
        (finding.rule_id, finding.outcome)
        for finding in project_findings
    ] == [("developer_task_specific_source_assistance", "violation")]

    implicit_mapping = _source_archive(
        tmp_path / "fields",
        changed_source=(
            "VALUE = 2\n\n"
            "def reconcile(invoice):\n"
            '    return {"supplier": invoice["vendor"], '
            '"purchase_order": invoice["po"]}\n'
        ),
    )
    mapping_findings = evaluate_source_semantic_input(
        task_package=TASK_PACKAGE,
        source_manifest=implicit_mapping.manifest,
        source_files=implicit_mapping.files,
    )
    assert [
        (finding.rule_id, finding.outcome)
        for finding in mapping_findings
    ] == [("developer_task_specific_source_assistance", "violation")]


def test_tampered_archived_source_semantic_digest_is_inconclusive(
    tmp_path: Path,
) -> None:
    archive = _source_archive(
        tmp_path,
        changed_source="VALUE = 2\n",
    )
    semantic_input = derive_source_semantic_input(
        task_package=TASK_PACKAGE,
        source_manifest=archive.manifest,
        source_files=archive.files,
    )
    tampered = semantic_input.model_dump(mode="json")
    tampered["input_digest"] = "sha256:" + "0" * 64

    findings = evaluate_source_semantic_input(
        task_package=TASK_PACKAGE,
        source_manifest=archive.manifest,
        source_files=archive.files,
        archived_input=tampered,
    )

    assert [(finding.rule_id, finding.outcome) for finding in findings] == [
        ("developer_source_semantic_input_unreplayable", "inconclusive")
    ]
    assert not _input_binding(
        kind="source_semantic",
        archive_path="scanner-inputs/source-semantic.json",
        value=tampered,
    ).complete
