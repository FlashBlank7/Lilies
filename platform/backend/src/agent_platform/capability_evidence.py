from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionEnvelope(str, Enum):
    E0 = "E0"
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"
    E5 = "E5"


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class EvidenceLevel(str, Enum):
    H0 = "H0"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"
    H4 = "H4"
    H5 = "H5"


class EvidenceEnvironment(str, Enum):
    mock = "mock"
    contract = "contract"
    sandbox = "sandbox"
    live = "live"
    production_observation = "production_observation"


class VerificationStatus(str, Enum):
    design_only = "design_only"
    static_verified = "static_verified"
    component_verified = "component_verified"
    integration_verified = "integration_verified"
    live_verified = "live_verified"
    production_observed = "production_observed"
    blocked_by_environment = "blocked_by_environment"
    unsupported = "unsupported"



ArtifactCategory = Literal[
    "implementation",
    "default",
    "api",
    "test",
    "integration",
    "live",
    "telemetry",
]


VERIFIED_STATUS_ORDER: dict[VerificationStatus, int] = {
    VerificationStatus.design_only: 0,
    VerificationStatus.static_verified: 1,
    VerificationStatus.component_verified: 2,
    VerificationStatus.integration_verified: 3,
    VerificationStatus.live_verified: 4,
    VerificationStatus.production_observed: 5,
}

STATUS_EVIDENCE_LEVEL: dict[VerificationStatus, EvidenceLevel] = {
    VerificationStatus.design_only: EvidenceLevel.H0,
    VerificationStatus.static_verified: EvidenceLevel.H1,
    VerificationStatus.component_verified: EvidenceLevel.H2,
    VerificationStatus.integration_verified: EvidenceLevel.H3,
    VerificationStatus.live_verified: EvidenceLevel.H4,
    VerificationStatus.production_observed: EvidenceLevel.H5,
    VerificationStatus.blocked_by_environment: EvidenceLevel.H0,
    VerificationStatus.unsupported: EvidenceLevel.H0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ModulePort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    value_type: Literal[
        "string", "number", "boolean", "object", "array", "file", "file_list", "any"
    ] = "any"
    required: bool = True
    description: str = Field(min_length=1, max_length=1000)


class ModuleDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    version: int = Field(ge=1)
    capability_ids: list[str] = Field(default_factory=list, max_length=40)
    reason: str = Field(min_length=1, max_length=1000)


class ModuleKnownBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    effect: Literal["unsupported", "blocked_by_environment", "degraded", "requires_approval"]
    capability_ids: list[str] = Field(default_factory=list, max_length=40)


class ModuleCapabilityClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    statement: str = Field(min_length=1, max_length=2000)
    requested_status: VerificationStatus
    claim_scope: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def verified_claim_status_only(self) -> ModuleCapabilityClaim:
        if self.requested_status in {
            VerificationStatus.blocked_by_environment,
            VerificationStatus.unsupported,
        }:
            raise ValueError("module capability claims must request a verifiable status")
        return self


class ReusableModuleContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    capability_ids: list[str] = Field(min_length=1, max_length=80)
    inputs: list[ModulePort] = Field(min_length=1, max_length=40)
    outputs: list[ModulePort] = Field(min_length=1, max_length=40)
    dependencies: list[ModuleDependency] = Field(default_factory=list, max_length=40)
    required_envelope: ExecutionEnvelope
    risk_level: RiskLevel
    known_boundaries: list[ModuleKnownBoundary] = Field(min_length=1, max_length=40)
    claims: list[ModuleCapabilityClaim] = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_contract(self) -> ReusableModuleContract:
        if len(self.capability_ids) != len(set(self.capability_ids)):
            raise ValueError("module contract has duplicate capability_ids")
        input_names = [item.name for item in self.inputs]
        output_names = [item.name for item in self.outputs]
        if len(input_names) != len(set(input_names)):
            raise ValueError("module contract has duplicate input ports")
        if len(output_names) != len(set(output_names)):
            raise ValueError("module contract has duplicate output ports")
        claim_ids = [item.capability_id for item in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("module contract has duplicate capability claims")
        unknown_claims = sorted(set(claim_ids) - set(self.capability_ids))
        if unknown_claims:
            raise ValueError(f"module claims reference undeclared capabilities: {unknown_claims}")
        missing_claims = sorted(set(self.capability_ids) - set(claim_ids))
        if missing_claims:
            raise ValueError(f"module capabilities have no claim declaration: {missing_claims}")
        boundary_unknown = sorted({
            capability_id
            for boundary in self.known_boundaries
            for capability_id in boundary.capability_ids
            if capability_id not in self.capability_ids
        })
        if boundary_unknown:
            raise ValueError(
                f"module boundaries reference undeclared capabilities: {boundary_unknown}"
            )
        return self


class EvidenceArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ArtifactCategory
    path: str = Field(min_length=1, max_length=1000)
    locator: str = Field(default="", max_length=500)
    method: Literal["direct", "derived", "approximation"] = "direct"
    description: str = Field(default="", max_length=1000)
    sha256: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    size_bytes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def safe_relative_path(self) -> EvidenceArtifact:
        candidate = Path(self.path)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("evidence artifact path must stay inside the evidence root")
        return self


class EvidenceGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    impact: str = Field(min_length=1, max_length=2000)
    recheck_trigger: str = Field(default="", max_length=1000)


class CapabilityEvidenceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,159}$",
    )
    capability_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$")
    claim: str = Field(min_length=1, max_length=2000)
    claim_scope: str = Field(min_length=1, max_length=2000)
    requested_status: VerificationStatus
    environment: EvidenceEnvironment
    artifacts: list[EvidenceArtifact] = Field(default_factory=list, max_length=80)
    gaps: list[EvidenceGap] = Field(default_factory=list, max_length=40)
    module_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,119}$",
    )
    module_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def module_coordinates_are_paired(self) -> CapabilityEvidenceCreateRequest:
        if (self.module_id is None) != (self.module_version is None):
            raise ValueError("module_id and module_version must be provided together")
        if self.requested_status in {
            VerificationStatus.blocked_by_environment,
            VerificationStatus.unsupported,
        } and not self.gaps:
            raise ValueError("blocked or unsupported evidence requires an explicit gap")
        return self


class CapabilityEvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    capability_id: str
    claim: str
    claim_scope: str
    verification_status: VerificationStatus
    claim_ceiling: VerificationStatus
    evidence_level: EvidenceLevel
    ceiling_level: EvidenceLevel
    environment: EvidenceEnvironment
    artifacts: list[EvidenceArtifact]
    gaps: list[EvidenceGap]
    module_id: str | None = None
    module_version: int | None = None
    created_at: str = Field(default_factory=utc_now)

    @property
    def artifact_categories(self) -> list[str]:
        return sorted({item.category for item in self.artifacts})


def derive_claim_ceiling(
    claim: str,
    artifacts: list[EvidenceArtifact],
) -> VerificationStatus:
    if not artifacts:
        return VerificationStatus.design_only

    normalized_claim = claim.casefold()
    metric_claim = any(
        marker in normalized_claim
        for marker in ("token", "cost", "budget", "令牌", "成本", "预算")
    )
    if metric_claim and all(item.method == "approximation" for item in artifacts):
        return VerificationStatus.unsupported

    categories = {item.category for item in artifacts}
    if not categories.intersection({"implementation", "default", "api"}):
        return VerificationStatus.design_only
    ceiling = VerificationStatus.static_verified
    if "test" not in categories:
        return ceiling
    ceiling = VerificationStatus.component_verified
    if "integration" not in categories:
        return ceiling
    ceiling = VerificationStatus.integration_verified
    if "live" not in categories:
        return ceiling
    ceiling = VerificationStatus.live_verified
    if "telemetry" in categories:
        ceiling = VerificationStatus.production_observed
    return ceiling


class CapabilityEvidenceRegistry:
    def __init__(
        self,
        directory: Path | str | None = None,
        *,
        evidence_root: Path | str | None = None,
    ) -> None:
        self._directory = Path(directory).resolve() if directory else None
        self._evidence_root = Path(evidence_root or Path.cwd()).resolve()
        self._records: dict[str, CapabilityEvidenceRecord] = {}
        if self._directory:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._load()

    def _record_path(self, record_id: str) -> Path:
        assert self._directory is not None
        key = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
        return self._directory / f"{key}.json"

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)

    def _load(self) -> None:
        assert self._directory is not None
        for path in sorted(self._directory.glob("*.json")):
            try:
                record = CapabilityEvidenceRecord.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                self._records[record.record_id] = record
            except Exception as exc:
                print(f"[capability_evidence] skip {path.name}: {exc}")

    def _resolve_artifact(self, artifact: EvidenceArtifact) -> EvidenceArtifact:
        resolved = (self._evidence_root / artifact.path).resolve()
        if resolved != self._evidence_root and self._evidence_root not in resolved.parents:
            raise ValueError(f"evidence artifact escapes evidence root: {artifact.path}")
        if not resolved.is_file():
            raise ValueError(f"evidence artifact does not exist: {artifact.path}")
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        return artifact.model_copy(update={
            "sha256": digest,
            "size_bytes": resolved.stat().st_size,
        })

    def register(
        self,
        request: CapabilityEvidenceCreateRequest,
        *,
        module_id: str | None = None,
        module_version: int | None = None,
    ) -> CapabilityEvidenceRecord:
        resolved_module_id = module_id if module_id is not None else request.module_id
        resolved_module_version = (
            module_version if module_version is not None else request.module_version
        )
        if (resolved_module_id is None) != (resolved_module_version is None):
            raise ValueError("module_id and module_version must be provided together")
        if request.module_id is not None and request.module_id != resolved_module_id:
            raise ValueError("evidence module_id does not match route module")
        if request.module_version is not None and request.module_version != resolved_module_version:
            raise ValueError("evidence module_version does not match route version")

        artifacts = [self._resolve_artifact(item) for item in request.artifacts]
        ceiling = derive_claim_ceiling(request.claim, artifacts)
        requested = request.requested_status
        if requested in {
            VerificationStatus.blocked_by_environment,
            VerificationStatus.unsupported,
        }:
            ceiling = requested
        elif ceiling in {
            VerificationStatus.blocked_by_environment,
            VerificationStatus.unsupported,
        }:
            raise ValueError(
                f"claim cannot be verified: evidence ceiling is {ceiling.value}"
            )
        elif VERIFIED_STATUS_ORDER[requested] > VERIFIED_STATUS_ORDER[ceiling]:
            raise ValueError(
                f"requested status {requested.value} exceeds evidence ceiling {ceiling.value}"
            )

        identity_payload = request.model_dump(
            mode="json",
            exclude={"record_id", "module_id", "module_version", "artifacts"},
        )
        identity_payload.update({
            "module_id": resolved_module_id,
            "module_version": resolved_module_version,
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
        })
        generated_record_id = "evidence:" + hashlib.sha256(
            json.dumps(
                identity_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        record = CapabilityEvidenceRecord(
            record_id=request.record_id or generated_record_id,
            capability_id=request.capability_id,
            claim=request.claim,
            claim_scope=request.claim_scope,
            verification_status=requested,
            claim_ceiling=ceiling,
            evidence_level=STATUS_EVIDENCE_LEVEL[requested],
            ceiling_level=STATUS_EVIDENCE_LEVEL[ceiling],
            environment=request.environment,
            artifacts=artifacts,
            gaps=request.gaps,
            module_id=resolved_module_id,
            module_version=resolved_module_version,
        )
        existing = self._records.get(record.record_id)
        if existing is not None:
            comparable_existing = existing.model_dump(mode="json", exclude={"created_at"})
            comparable_new = record.model_dump(mode="json", exclude={"created_at"})
            if comparable_existing != comparable_new:
                raise ValueError(f"evidence record id is immutable: {record.record_id}")
            return existing
        self._records[record.record_id] = record
        if self._directory:
            self._atomic_write(
                self._record_path(record.record_id),
                json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
            )
        return record

    def get(self, record_id: str) -> CapabilityEvidenceRecord:
        try:
            return self._records[record_id]
        except KeyError:
            raise KeyError(f"capability evidence not found: {record_id}") from None

    def list(
        self,
        *,
        capability_id: str | None = None,
        module_id: str | None = None,
        module_version: int | None = None,
        verification_status: VerificationStatus | None = None,
        category: ArtifactCategory | None = None,
    ) -> list[CapabilityEvidenceRecord]:
        records = list(self._records.values())
        if capability_id:
            records = [item for item in records if item.capability_id == capability_id]
        if module_id:
            records = [item for item in records if item.module_id == module_id]
        if module_version is not None:
            records = [item for item in records if item.module_version == module_version]
        if verification_status:
            records = [
                item for item in records
                if item.verification_status == verification_status
            ]
        if category:
            records = [
                item for item in records
                if any(artifact.category == category for artifact in item.artifacts)
            ]
        return sorted(records, key=lambda item: (item.capability_id, item.created_at, item.record_id))

    def integrity_errors(self, record: CapabilityEvidenceRecord) -> list[str]:
        errors: list[str] = []
        for artifact in record.artifacts:
            try:
                current = self._resolve_artifact(artifact)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            if current.sha256 != artifact.sha256:
                errors.append(
                    f"evidence artifact changed after registration: {artifact.path}"
                )
        return errors

    def __len__(self) -> int:
        return len(self._records)
