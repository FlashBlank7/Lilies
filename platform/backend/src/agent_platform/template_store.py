"""Versioned reusable-module and compatibility template storage."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .capability_contracts import CapabilityBuildContract, ExecutionEnvelope, VerificationStatus
from .capability_evidence import (
    CapabilityEvidenceCreateRequest,
    CapabilityEvidenceRecord,
    CapabilityEvidenceRegistry,
    ReusableModuleContract,
    VERIFIED_STATUS_ORDER,
)
from .template_models import Template, TemplateMeta
from .workflow_models import WorkflowSpec


ModuleVersionStatus = Literal[
    "legacy_unverified",
    "draft",
    "verified",
    "deprecated",
    "quarantined",
]
ModuleSource = Literal["builtin", "system", "user", "session_extract"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ModuleVersionState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_id: str
    version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: ModuleSource
    status: ModuleVersionStatus
    created_at: str = Field(default_factory=utc_now)
    verified_at: str | None = None
    verification_errors: list[str] = Field(default_factory=list)
    evidence_record_ids: list[str] = Field(default_factory=list)


class ModuleVersionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: ModuleVersionState
    template: Template

    @property
    def module_ref(self) -> str:
        return f"module:{self.state.module_id}@{self.state.version}"


class ModuleCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_ref: str
    verified: bool
    envelope_compatible: bool
    required_capability_ids: list[str]
    covered_capability_ids: list[str]
    missing_capability_ids: list[str]
    extra_capability_ids: list[str]
    known_boundaries: list[dict[str, Any]]
    eligible_for_reuse: bool
    reason: str


class TemplateStore:
    """Immutable module versions plus latest-version template compatibility APIs."""

    def __init__(
        self,
        registry_dir: Path | str | None = None,
        *,
        evidence_root: Path | str | None = None,
        workflow_validator: Callable[[WorkflowSpec], list[str]] | None = None,
    ) -> None:
        self._templates: dict[str, dict[int, Template]] = {}
        self._states: dict[tuple[str, int], ModuleVersionState] = {}
        self._dir = Path(registry_dir).resolve() if registry_dir else None
        self._workflow_validator = workflow_validator
        evidence_dir = self._dir / "evidence" if self._dir else None
        self.evidence = CapabilityEvidenceRegistry(
            evidence_dir,
            evidence_root=evidence_root,
        )
        if self._dir:
            (self._dir / "modules").mkdir(parents=True, exist_ok=True)
            self._load_persisted()

    @staticmethod
    def _content_payload(template: Template) -> dict[str, Any]:
        meta = template.meta.model_dump(
            mode="json",
            exclude={"usage_count", "pending_branches_count", "rating_sum", "rating_count"},
        )
        return {
            "meta": meta,
            "workflow": template.workflow.model_dump(mode="json"),
            "module_contract": (
                template.module_contract.model_dump(mode="json")
                if template.module_contract
                else None
            ),
        }

    @classmethod
    def content_hash(cls, template: Template) -> str:
        payload = json.dumps(
            cls._content_payload(template),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _module_key(module_id: str) -> str:
        readable = re.sub(r"[^A-Za-z0-9_.-]", "_", module_id)[:40] or "module"
        digest = hashlib.sha256(module_id.encode("utf-8")).hexdigest()[:16]
        return f"{readable}-{digest}"

    def _paths(self, module_id: str, version: int) -> tuple[Path, Path]:
        assert self._dir is not None
        root = self._dir / "modules" / self._module_key(module_id)
        return root / f"v{version}.content.json", root / f"v{version}.state.json"

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)

    def _persist_content(self, template: Template) -> None:
        if not self._dir:
            return
        content_path, _ = self._paths(template.meta.name, template.meta.version)
        if content_path.exists():
            stored = Template.model_validate_json(content_path.read_text(encoding="utf-8"))
            if self.content_hash(stored) != self.content_hash(template):
                raise ValueError(
                    f"module version content is immutable: {template.meta.name}@{template.meta.version}"
                )
            return
        self._atomic_write(
            content_path,
            json.dumps(template.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )

    def _persist_state(self, state: ModuleVersionState) -> None:
        if not self._dir:
            return
        _, state_path = self._paths(state.module_id, state.version)
        self._atomic_write(
            state_path,
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )

    def _load_persisted(self) -> None:
        assert self._dir is not None
        for content_path in sorted((self._dir / "modules").glob("*/v*.content.json")):
            try:
                template = Template.model_validate_json(content_path.read_text(encoding="utf-8"))
                _, state_path = self._paths(template.meta.name, template.meta.version)
                if not state_path.is_file():
                    raise ValueError("module state record is missing")
                state = ModuleVersionState.model_validate_json(
                    state_path.read_text(encoding="utf-8")
                )
                if state.module_id != template.meta.name or state.version != template.meta.version:
                    raise ValueError("module content and state coordinates do not match")
                actual_hash = self.content_hash(template)
                if actual_hash != state.content_hash:
                    state = state.model_copy(update={
                        "status": "quarantined",
                        "verification_errors": [
                            "stored module content hash does not match immutable state"
                        ],
                    })
                    self._persist_state(state)
                self._templates.setdefault(template.meta.name, {})[template.meta.version] = template
                self._states[(template.meta.name, template.meta.version)] = state
            except Exception as exc:
                print(f"[template_store] skip {content_path.name}: {exc}")

    def _insert(
        self,
        template: Template,
        *,
        source: ModuleSource,
        status: ModuleVersionStatus,
        persist: bool,
    ) -> ModuleVersionRecord:
        key = (template.meta.name, template.meta.version)
        existing = self._states.get(key)
        if existing is not None:
            existing_template = self._templates[template.meta.name][template.meta.version]
            if self.content_hash(existing_template) != self.content_hash(template):
                raise ValueError(
                    f"module version already exists with different content: "
                    f"{template.meta.name}@{template.meta.version}"
                )
            return ModuleVersionRecord(state=existing, template=existing_template)

        state = ModuleVersionState(
            module_id=template.meta.name,
            version=template.meta.version,
            content_hash=self.content_hash(template),
            source=source,
            status=status,
        )
        self._templates.setdefault(template.meta.name, {})[template.meta.version] = template
        self._states[key] = state
        if persist:
            self._persist_content(template)
            self._persist_state(state)
        return ModuleVersionRecord(state=state, template=template)

    def load_builtins(self, directory: Path | str) -> int:
        """Load source-controlled templates without granting verification."""
        root = Path(directory)
        if not root.is_dir():
            return 0
        count = 0
        for path in sorted(root.glob("*.json")):
            try:
                template = Template.model_validate_json(path.read_text(encoding="utf-8"))
                status: ModuleVersionStatus = (
                    "draft" if template.module_contract else "legacy_unverified"
                )
                self._insert(template, source="builtin", status=status, persist=False)
                count += 1
            except Exception as exc:
                print(f"[template_store] skip {path.name}: {exc}")
        return count

    def register(
        self,
        name: str,
        workflow: WorkflowSpec,
        meta_overrides: dict[str, Any] | None = None,
        *,
        module_contract: ReusableModuleContract | None = None,
        source: ModuleSource = "user",
        persist: bool | None = None,
        exact_version: int | None = None,
    ) -> Template:
        """Publish a new immutable module version and return its template content."""
        overrides = meta_overrides or {}
        current_versions = self._templates.get(name, {})
        version = exact_version or (max(current_versions, default=0) + 1)
        meta = TemplateMeta(
            name=name,
            title=overrides.get("title", name),
            description=overrides.get("description", ""),
            category=overrides.get("category", "task_management"),
            tags=overrides.get("tags", []),
            icon=overrides.get("icon", "workflow"),
            expected_inputs=overrides.get("expected_inputs", {}),
            expected_outputs=overrides.get("expected_outputs", {}),
            author=overrides.get("author", "user"),
            version=version,
            min_blocks_required=overrides.get("min_blocks_required", []),
            provenance=overrides.get("provenance", []),
            confidence=overrides.get("confidence", 0.70),
            seed_template=overrides.get("seed_template", False),
        )
        template = Template(
            meta=meta,
            workflow=workflow.model_copy(deep=True),
            module_contract=module_contract,
        )
        status: ModuleVersionStatus = "draft" if module_contract else "legacy_unverified"
        should_persist = (source in {"user", "session_extract"}) if persist is None else persist
        self._insert(
            template,
            source=source,
            status=status,
            persist=should_persist,
        )
        return template

    def list(
        self,
        *,
        category: str | None = None,
        query: str = "",
    ) -> list[TemplateMeta]:
        result: list[TemplateMeta] = []
        needle = query.casefold().strip()
        for name in self.names():
            template = self.get(name)
            if category and template.meta.category != category:
                continue
            if needle:
                searchable = " ".join([
                    template.meta.name,
                    template.meta.title,
                    template.meta.description,
                    template.meta.category,
                    *template.meta.tags,
                ]).casefold()
                if needle not in searchable:
                    continue
            result.append(template.meta)
        return sorted(result, key=lambda meta: meta.title)

    def list_records(
        self,
        *,
        all_versions: bool = False,
        status: ModuleVersionStatus | None = None,
        query: str = "",
    ) -> list[ModuleVersionRecord]:
        records: list[ModuleVersionRecord] = []
        needle = query.casefold().strip()
        for name, versions in self._templates.items():
            selected_versions = sorted(versions) if all_versions else [max(versions)]
            for version in selected_versions:
                record = self.get_record(name, version)
                if status and record.state.status != status:
                    continue
                if needle:
                    searchable = " ".join([
                        name,
                        record.template.meta.title,
                        record.template.meta.description,
                        *record.template.meta.tags,
                        *(record.template.module_contract.capability_ids if record.template.module_contract else []),
                    ]).casefold()
                    if needle not in searchable:
                        continue
                records.append(record)
        return sorted(
            records,
            key=lambda item: (item.state.module_id, item.state.version),
        )

    def get(self, name: str, version: int | None = None) -> Template:
        try:
            versions = self._templates[name]
            resolved_version = version if version is not None else max(versions)
            return versions[resolved_version]
        except (KeyError, ValueError):
            suffix = f"@{version}" if version is not None else ""
            raise KeyError(f"template not found: {name}{suffix}") from None

    def get_record(self, name: str, version: int | None = None) -> ModuleVersionRecord:
        template = self.get(name, version)
        state = self._states[(name, template.meta.version)]
        return ModuleVersionRecord(state=state, template=template)

    def get_record_by_ref(self, module_ref: str) -> ModuleVersionRecord:
        match = re.fullmatch(
            r"module:([A-Za-z][A-Za-z0-9_.-]{1,119})@([1-9][0-9]*)",
            module_ref,
        )
        if match is None:
            raise KeyError(f"invalid module reference: {module_ref}")
        return self.get_record(match.group(1), int(match.group(2)))

    def versions(self, name: str) -> list[int]:
        if name not in self._templates:
            raise KeyError(f"template not found: {name}")
        return sorted(self._templates[name])

    def get_workflow(self, name: str, version: int | None = None) -> WorkflowSpec:
        return self.get(name, version).workflow

    def names(self) -> list[str]:
        return sorted(self._templates)

    def categories(self) -> list[str]:
        return sorted({self.get(name).meta.category for name in self.names()})

    def verified_module_refs(self) -> list[str]:
        return sorted(
            record.module_ref
            for record in self.list_records(all_versions=True, status="verified")
        )

    def add_evidence(
        self,
        module_id: str,
        version: int,
        request: CapabilityEvidenceCreateRequest,
    ) -> CapabilityEvidenceRecord:
        record = self.get_record(module_id, version)
        contract = record.template.module_contract
        if contract is None:
            raise ValueError("legacy templates cannot receive capability verification evidence")
        if request.capability_id not in contract.capability_ids:
            raise ValueError(
                f"evidence capability is not declared by module: {request.capability_id}"
            )
        evidence = self.evidence.register(
            request,
            module_id=module_id,
            module_version=version,
        )
        return evidence

    def verification_errors(self, module_id: str, version: int) -> tuple[list[str], list[str]]:
        record = self.get_record(module_id, version)
        template = record.template
        errors: list[str] = []
        evidence_ids: set[str] = set()
        if self.content_hash(template) != record.state.content_hash:
            errors.append("module content hash no longer matches immutable state")
        contract = template.module_contract
        if contract is None:
            errors.append("module has no reusable capability contract")
            return errors, []
        if not template.workflow.nodes:
            errors.append("module workflow has no executable nodes")
        if self._workflow_validator:
            errors.extend(
                f"workflow validation: {item}"
                for item in self._workflow_validator(template.workflow)
            )
        node_types = {node.type for node in template.workflow.nodes}
        missing_blocks = sorted(set(template.meta.min_blocks_required) - node_types)
        if missing_blocks:
            errors.append(
                f"module workflow is missing declared block types: {missing_blocks}"
            )

        for dependency in contract.dependencies:
            if dependency.module_id == module_id and dependency.version == version:
                errors.append("module cannot depend on itself")
                continue
            try:
                dependency_record = self.get_record(
                    dependency.module_id,
                    dependency.version,
                )
            except KeyError:
                errors.append(
                    f"module dependency is missing: {dependency.module_id}@{dependency.version}"
                )
                continue
            if dependency_record.state.status != "verified":
                errors.append(
                    f"module dependency is not verified: "
                    f"{dependency.module_id}@{dependency.version}"
                )

        for claim in contract.claims:
            candidates = self.evidence.list(
                capability_id=claim.capability_id,
                module_id=module_id,
                module_version=version,
            )
            eligible: list[CapabilityEvidenceRecord] = []
            stale_errors: list[str] = []
            for evidence in candidates:
                if evidence.verification_status in {
                    VerificationStatus.blocked_by_environment,
                    VerificationStatus.unsupported,
                }:
                    continue
                if (
                    VERIFIED_STATUS_ORDER[evidence.verification_status]
                    >= VERIFIED_STATUS_ORDER[claim.requested_status]
                ):
                    integrity_errors = self.evidence.integrity_errors(evidence)
                    if integrity_errors:
                        stale_errors.extend(
                            f"{claim.capability_id}: {item}" for item in integrity_errors
                        )
                    else:
                        eligible.append(evidence)
            if not eligible:
                errors.extend(stale_errors)
                errors.append(
                    f"no intact evidence reaches {claim.requested_status.value} "
                    f"for capability {claim.capability_id}"
                )
            evidence_ids.update(item.record_id for item in eligible)
        return sorted(set(errors)), sorted(evidence_ids)

    def verify(self, module_id: str, version: int) -> ModuleVersionRecord:
        record = self.get_record(module_id, version)
        errors, evidence_ids = self.verification_errors(module_id, version)
        status: ModuleVersionStatus = (
            "verified"
            if not errors
            else "quarantined" if record.state.status == "quarantined" else "draft"
        )
        state = record.state.model_copy(update={
            "status": status,
            "verified_at": utc_now() if not errors else None,
            "verification_errors": errors,
            "evidence_record_ids": evidence_ids,
        })
        self._states[(module_id, version)] = state
        self._persist_state(state)
        if errors:
            raise ValueError("; ".join(errors))
        return ModuleVersionRecord(state=state, template=record.template)

    def deprecate(self, module_id: str, version: int) -> ModuleVersionRecord:
        record = self.get_record(module_id, version)
        state = record.state.model_copy(update={"status": "deprecated"})
        self._states[(module_id, version)] = state
        self._persist_state(state)
        return ModuleVersionRecord(state=state, template=record.template)

    def compatibility(
        self,
        record: ModuleVersionRecord,
        contract: CapabilityBuildContract | None,
    ) -> ModuleCompatibility:
        module_contract = record.template.module_contract
        required = sorted(
            item.id for item in contract.capabilities if item.required
        ) if contract else []
        module_capabilities = sorted(module_contract.capability_ids) if module_contract else []
        covered = sorted(set(required).intersection(module_capabilities))
        missing = sorted(set(required) - set(module_capabilities))
        extra = sorted(set(module_capabilities) - set(required))
        envelope_compatible = True
        if contract and module_contract:
            order = {item: index for index, item in enumerate(ExecutionEnvelope)}
            envelope_compatible = (
                order[module_contract.required_envelope]
                <= order[contract.required_envelope]
            )
        verified = record.state.status == "verified"
        eligible = bool(module_contract) and verified and not missing and envelope_compatible
        reasons: list[str] = []
        if not module_contract:
            reasons.append("module contract is missing")
        if not verified:
            reasons.append(f"module status is {record.state.status}")
        if missing:
            reasons.append(f"missing capabilities: {', '.join(missing)}")
        if not envelope_compatible:
            reasons.append("module requires a stronger execution envelope")
        return ModuleCompatibility(
            module_ref=record.module_ref,
            verified=verified,
            envelope_compatible=envelope_compatible,
            required_capability_ids=required,
            covered_capability_ids=covered,
            missing_capability_ids=missing,
            extra_capability_ids=extra,
            known_boundaries=[
                item.model_dump(mode="json")
                for item in (module_contract.known_boundaries if module_contract else [])
            ],
            eligible_for_reuse=eligible,
            reason="compatible verified module" if eligible else "; ".join(reasons),
        )

    @staticmethod
    def _update_refs(value: Any, id_map: dict[str, str]) -> Any:
        if isinstance(value, list):
            return [TemplateStore._update_refs(item, id_map) for item in value]
        if isinstance(value, dict):
            updated = dict(value)
            ref = updated.get("$ref")
            if isinstance(ref, dict) and "node_id" in ref:
                updated["$ref"] = {
                    **ref,
                    "node_id": id_map.get(ref["node_id"], ref["node_id"]),
                }
            return {
                key: TemplateStore._update_refs(item, id_map)
                for key, item in updated.items()
            }
        return value

    def expand_into_workflow(
        self,
        name: str,
        *,
        version: int | None = None,
        prefix: str = "",
        x: float = 0,
        y: float = 0,
    ) -> WorkflowSpec:
        template = self.get(name, version)
        template.meta.usage_count += 1
        workflow = template.workflow.model_copy(deep=True)
        if prefix:
            id_map = {node.id: f"{prefix}_{node.id}" for node in workflow.nodes}
            for node in workflow.nodes:
                original_id = node.id
                node.id = id_map[original_id]
                node.config = self._update_refs(node.config, id_map)
            for edge in workflow.edges:
                edge.id = f"{prefix}_{edge.id}"
                edge.source = id_map.get(edge.source, edge.source)
                edge.target = id_map.get(edge.target, edge.target)
            for node in workflow.nodes:
                node.position.x += x
                node.position.y += y
        return workflow

    def __len__(self) -> int:
        return len(self._templates)
