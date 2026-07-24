from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .lilies_platform_contract import (
    DEFAULT_ARTIFACT_CHUNK_BYTES,
    MAX_ARTIFACT_CHUNK_BYTES,
    MAX_REGISTERED_ARTIFACT_BYTES,
)


SCHEMA_VERSION = 1
DEFAULT_MAX_ARTIFACT_BYTES = MAX_REGISTERED_ARTIFACT_BYTES

CorrelationId = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
RelativeArtifactPath = Annotated[str, StringConstraints(min_length=1, max_length=1_024)]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    if value.utcoffset() != timedelta(0):
        raise ValueError("datetime must use UTC (offset +00:00)")
    return value.astimezone(timezone.utc)


def _parse_utc(value: str) -> datetime:
    return _require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _validate_relative_path(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise ValueError("artifact relative_path must use safe POSIX path segments")
    if PureWindowsPath(value).drive:
        raise ValueError("artifact relative_path may not contain a drive or UNC root")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise ValueError("artifact relative_path must be relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("artifact relative_path may not traverse or contain empty segments")
    if path.as_posix() != value:
        raise ValueError("artifact relative_path must be canonical")
    return value


def _validate_media_type(value: str) -> str:
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise ValueError("media_type may not contain whitespace or control characters")
    if value.count("/") != 1:
        raise ValueError("media_type must contain one type/subtype separator")
    major, minor = value.split("/", 1)
    if not major or not minor:
        raise ValueError("media_type requires a non-empty type and subtype")
    return value


class StrictArtifactModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ArtifactBinding(StrictArtifactModel):
    assignment_id: UUID
    session_id: UUID
    application_id: UUID
    run_id: CorrelationId


class ArtifactRegistrationRequest(StrictArtifactModel):
    binding: ArtifactBinding
    relative_path: RelativeArtifactPath
    media_type: str = Field(min_length=3, max_length=200)
    expected_sha256: Sha256Digest | None = None

    @field_validator("relative_path")
    @classmethod
    def relative_path_is_safe(cls, value: str) -> str:
        return _validate_relative_path(value)

    @field_validator("media_type")
    @classmethod
    def media_type_is_valid(cls, value: str) -> str:
        return _validate_media_type(value)


class HostReceiptRegistrationRequest(ArtifactRegistrationRequest):
    """Trusted platform registration for one real host-write receipt.

    This model is deliberately not exposed by the Lilies black-box API.  The
    public run-artifact scanner always calls ``register_artifact`` and therefore
    cannot promote a workspace file into host-receipt evidence.
    """

    receipt_id: CorrelationId
    operation: CorrelationId


class ArtifactProvenance(StrictArtifactModel):
    schema_version: Literal["1.0"] = "1.0"
    evidence_kind: Literal["artifact", "host_receipt"]
    source: Literal["platform_artifact_scan", "platform_host_write"]
    assignment_id: UUID
    session_id: UUID
    application_id: UUID
    run_id: CorrelationId
    receipt_id: CorrelationId | None = None
    operation: CorrelationId | None = None

    @model_validator(mode="after")
    def kind_matches_platform_source(self) -> "ArtifactProvenance":
        if self.evidence_kind == "artifact":
            if (
                self.source != "platform_artifact_scan"
                or self.receipt_id is not None
                or self.operation is not None
            ):
                raise ValueError("artifact provenance must come from the platform scanner")
        elif (
            self.source != "platform_host_write"
            or self.receipt_id is None
            or self.operation is None
        ):
            raise ValueError("host-receipt provenance requires a platform host write")
        return self


class ArtifactReadRequest(StrictArtifactModel):
    artifact_id: UUID
    binding: ArtifactBinding
    offset_bytes: int = Field(default=0, ge=0)
    max_bytes: int = Field(
        default=DEFAULT_ARTIFACT_CHUNK_BYTES,
        ge=1,
        le=MAX_ARTIFACT_CHUNK_BYTES,
    )


class ArtifactRecord(StrictArtifactModel):
    artifact_id: UUID
    assignment_id: UUID
    session_id: UUID
    application_id: UUID
    run_id: CorrelationId
    root_path: Path
    relative_path: RelativeArtifactPath
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: Sha256Digest
    evidence_kind: Literal["artifact", "host_receipt"]
    provenance: ArtifactProvenance
    created_at: datetime

    @field_validator("root_path")
    @classmethod
    def root_is_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("artifact root_path must be absolute")
        return value

    @field_validator("relative_path")
    @classmethod
    def record_relative_path_is_safe(cls, value: str) -> str:
        return _validate_relative_path(value)

    @field_validator("media_type")
    @classmethod
    def record_media_type_is_valid(cls, value: str) -> str:
        return _validate_media_type(value)

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class ArtifactRegistrationResult(StrictArtifactModel):
    artifact: ArtifactRecord
    replayed: bool


class ArtifactReadResult(StrictArtifactModel):
    # Artifact chunks are exact byte projections; trimming would corrupt reconstruction.
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_assignment=True,
    )

    artifact_id: UUID
    assignment_id: UUID
    session_id: UUID
    application_id: UUID
    run_id: CorrelationId
    relative_path: RelativeArtifactPath
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: Sha256Digest
    offset_bytes: int = Field(ge=0)
    chunk_size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_CHUNK_BYTES)
    next_offset_bytes: int | None = Field(default=None, ge=0)
    complete: bool
    encoding: Literal["utf8", "base64"]
    content: str


class PlatformBlackboxArtifactError(RuntimeError):
    code = "artifact_error"


class PlatformBlackboxArtifactStoreError(PlatformBlackboxArtifactError):
    code = "artifact_store_error"


class PlatformBlackboxArtifactNotFound(PlatformBlackboxArtifactError):
    code = "artifact_not_found"


class PlatformBlackboxArtifactPathUnsafe(PlatformBlackboxArtifactError):
    code = "artifact_path_unsafe"


class PlatformBlackboxArtifactScopeDenied(PlatformBlackboxArtifactError):
    code = "artifact_scope_denied"


class PlatformBlackboxArtifactTooLarge(PlatformBlackboxArtifactError):
    code = "artifact_too_large"


class PlatformBlackboxArtifactRangeInvalid(PlatformBlackboxArtifactError):
    code = "artifact_range_invalid"


class PlatformBlackboxArtifactIntegrityError(PlatformBlackboxArtifactError):
    code = "artifact_integrity_failed"


class PlatformBlackboxArtifactConflict(PlatformBlackboxArtifactError):
    code = "artifact_conflict"


class PlatformBlackboxArtifactStore:
    """Server-owned artifact registry sharing the platform SQLite database.

    The absolute artifact root is always supplied by trusted server code, never by a
    request model.  Public reads use an opaque artifact id plus the complete task/run
    binding, so a relative filesystem path is neither an authority nor an identifier.
    File content is never stored in SQLite.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        hard_max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        if hard_max_bytes < 1:
            raise ValueError("hard_max_bytes must be positive")
        self.db_path = Path(db_path)
        self.hard_max_bytes = hard_max_bytes
        self._lock = asyncio.Lock()

    async def initialize(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        if self.db_path.exists():
            os.chmod(self.db_path, 0o600)
        return conn

    def _initialize_sync(self) -> dict[str, int]:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_blackbox_artifact_schema (
                  version INTEGER PRIMARY KEY,
                  applied_at TEXT NOT NULL
                )
                """
            )
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version),0) AS version "
                    "FROM platform_blackbox_artifact_schema"
                ).fetchone()["version"]
            )
            if current > SCHEMA_VERSION:
                raise PlatformBlackboxArtifactStoreError(
                    f"platform artifact schema {current} is newer than supported "
                    f"{SCHEMA_VERSION}"
                )
            if current < 1:
                self._migrate_v1(conn)
            self._ensure_provenance_registry(conn)
        self._secure_database_files()
        return {"schema_version": SCHEMA_VERSION}

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE platform_blackbox_artifacts (
              artifact_id TEXT PRIMARY KEY,
              assignment_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              application_id TEXT NOT NULL,
              run_id TEXT NOT NULL,
              root_path TEXT NOT NULL,
              relative_path TEXT NOT NULL,
              media_type TEXT NOT NULL,
              size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
              sha256 TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(assignment_id,session_id,application_id,run_id,root_path,relative_path)
            );
            CREATE INDEX idx_platform_blackbox_artifacts_binding
              ON platform_blackbox_artifacts(
                assignment_id,session_id,application_id,run_id,created_at
              );
            CREATE TRIGGER platform_blackbox_artifacts_no_update
              BEFORE UPDATE ON platform_blackbox_artifacts
              BEGIN SELECT RAISE(ABORT, 'platform artifact registry is immutable'); END;
            CREATE TRIGGER platform_blackbox_artifacts_no_delete
              BEFORE DELETE ON platform_blackbox_artifacts
              BEGIN SELECT RAISE(ABORT, 'platform artifact registry is immutable'); END;
            """
        )
        conn.execute(
            "INSERT INTO platform_blackbox_artifact_schema(version,applied_at) VALUES (?,?)",
            (1, _utc_now().isoformat()),
        )

    @staticmethod
    def _ensure_provenance_registry(conn: sqlite3.Connection) -> None:
        """Add immutable server provenance without rewriting the v1 artifact table."""

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS platform_blackbox_artifact_provenance (
              artifact_id TEXT PRIMARY KEY,
              evidence_kind TEXT NOT NULL
                CHECK(evidence_kind IN ('artifact','host_receipt')),
              provenance_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(artifact_id) REFERENCES platform_blackbox_artifacts(artifact_id)
                ON DELETE RESTRICT
            );
            CREATE TRIGGER IF NOT EXISTS platform_blackbox_artifact_provenance_no_update
              BEFORE UPDATE ON platform_blackbox_artifact_provenance
              BEGIN SELECT RAISE(ABORT, 'platform artifact provenance is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS platform_blackbox_artifact_provenance_no_delete
              BEFORE DELETE ON platform_blackbox_artifact_provenance
              BEGIN SELECT RAISE(ABORT, 'platform artifact provenance is immutable'); END;
            """
        )

    def _secure_database_files(self) -> None:
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)

    async def register_artifact(
        self,
        request: ArtifactRegistrationRequest,
        *,
        artifact_root: Path,
        max_bytes: int | None = None,
    ) -> ArtifactRegistrationResult:
        limit = self._effective_limit(max_bytes)
        async with self._lock:
            return await asyncio.to_thread(
                self._register_artifact_sync,
                request,
                Path(artifact_root),
                limit,
                ArtifactProvenance(
                    evidence_kind="artifact",
                    source="platform_artifact_scan",
                    **request.binding.model_dump(mode="python"),
                ),
            )

    async def register_host_receipt(
        self,
        request: HostReceiptRegistrationRequest,
        *,
        artifact_root: Path,
        max_bytes: int | None = None,
    ) -> ArtifactRegistrationResult:
        """Register host-receipt evidence from trusted platform execution code."""

        limit = self._effective_limit(max_bytes)
        async with self._lock:
            return await asyncio.to_thread(
                self._register_artifact_sync,
                request,
                Path(artifact_root),
                limit,
                ArtifactProvenance(
                    evidence_kind="host_receipt",
                    source="platform_host_write",
                    receipt_id=request.receipt_id,
                    operation=request.operation,
                    **request.binding.model_dump(mode="python"),
                ),
            )

    def _register_artifact_sync(
        self,
        request: ArtifactRegistrationRequest,
        artifact_root: Path,
        max_bytes: int,
        provenance: ArtifactProvenance,
    ) -> ArtifactRegistrationResult:
        root = self._canonical_root(artifact_root)
        artifact_file = self._contained_file(root, request.relative_path)
        size = artifact_file.stat().st_size
        if size > max_bytes:
            raise PlatformBlackboxArtifactTooLarge(
                f"artifact size {size} exceeds the {max_bytes} byte limit"
            )
        raw = artifact_file.read_bytes()
        if len(raw) > max_bytes:
            raise PlatformBlackboxArtifactTooLarge(
                f"artifact size {len(raw)} exceeds the {max_bytes} byte limit"
            )
        digest = _digest(raw)
        if request.expected_sha256 is not None and not hmac.compare_digest(
            request.expected_sha256,
            digest,
        ):
            raise PlatformBlackboxArtifactIntegrityError(
                "artifact content does not match expected_sha256"
            )
        canonical_relative = artifact_file.relative_to(root).as_posix()
        _validate_relative_path(canonical_relative)
        now = _utc_now()
        artifact_id = uuid4()
        binding = request.binding
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM platform_blackbox_artifacts
                WHERE assignment_id=? AND session_id=? AND application_id=? AND run_id=?
                  AND root_path=? AND relative_path=?
                """,
                (
                    str(binding.assignment_id),
                    str(binding.session_id),
                    str(binding.application_id),
                    binding.run_id,
                    str(root),
                    canonical_relative,
                ),
            ).fetchone()
            if existing is not None:
                persisted_provenance = self._provenance_for_row(conn, existing)
                same = (
                    existing["media_type"] == request.media_type
                    and existing["size_bytes"] == len(raw)
                    and hmac.compare_digest(existing["sha256"], digest)
                    and persisted_provenance == provenance
                )
                if not same:
                    raise PlatformBlackboxArtifactConflict(
                        "artifact path is already registered with different immutable metadata"
                    )
                return ArtifactRegistrationResult(
                    artifact=self._record_from_row(
                        existing,
                        provenance=persisted_provenance,
                    ),
                    replayed=True,
                )
            conn.execute(
                """
                INSERT INTO platform_blackbox_artifacts(
                  artifact_id,assignment_id,session_id,application_id,run_id,root_path,
                  relative_path,media_type,size_bytes,sha256,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(artifact_id),
                    str(binding.assignment_id),
                    str(binding.session_id),
                    str(binding.application_id),
                    binding.run_id,
                    str(root),
                    canonical_relative,
                    request.media_type,
                    len(raw),
                    digest,
                    now.isoformat(),
                ),
            )
            conn.execute(
                """
                INSERT INTO platform_blackbox_artifact_provenance(
                  artifact_id,evidence_kind,provenance_json,created_at
                ) VALUES (?,?,?,?)
                """,
                (
                    str(artifact_id),
                    provenance.evidence_kind,
                    provenance.model_dump_json(exclude_none=True),
                    now.isoformat(),
                ),
            )
            row = self._require_record_conn(conn, str(artifact_id))
            return ArtifactRegistrationResult(
                artifact=self._record_from_row(
                    row,
                    provenance=self._provenance_for_row(conn, row),
                ),
                replayed=False,
            )

    async def get_artifact(self, artifact_id: UUID) -> ArtifactRecord:
        return await asyncio.to_thread(self._get_artifact_sync, str(artifact_id))

    async def export_assignment_inventory(
        self,
        *,
        assignment_id: UUID,
        session_id: UUID,
        application_id: UUID,
    ) -> dict[str, Any]:
        """Export every immutable artifact record for one formal assignment."""

        async with self._lock:
            return await asyncio.to_thread(
                self._export_assignment_inventory_sync,
                str(assignment_id),
                str(session_id),
                str(application_id),
            )

    def _export_assignment_inventory_sync(
        self,
        assignment_id: str,
        session_id: str,
        application_id: str,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            rows = conn.execute(
                """
                SELECT * FROM platform_blackbox_artifacts
                WHERE assignment_id=? AND session_id=? AND application_id=?
                ORDER BY created_at,artifact_id
                """,
                (assignment_id, session_id, application_id),
            ).fetchall()
            if len(rows) > 10_000:
                raise PlatformBlackboxArtifactStoreError(
                    "formal artifact inventory exceeds the complete export limit"
                )
            records = []
            for row in rows:
                payload = self._record_from_row(
                    row,
                    provenance=self._provenance_for_row(conn, row),
                ).model_dump(mode="json", exclude_none=True)
                payload.pop("root_path", None)
                records.append(payload)
        return {
            "schema_version": "1.0",
            "assignment_id": assignment_id,
            "session_id": session_id,
            "application_id": application_id,
            "complete": True,
            "count": len(records),
            "records": records,
        }

    def _get_artifact_sync(self, artifact_id: str) -> ArtifactRecord:
        with self._connect() as conn:
            row = self._require_record_conn(conn, artifact_id)
            return self._record_from_row(
                row,
                provenance=self._provenance_for_row(conn, row),
            )

    async def read_artifact(
        self,
        request: ArtifactReadRequest,
        *,
        artifact_root: Path,
    ) -> ArtifactReadResult:
        return await asyncio.to_thread(
            self._read_artifact_sync,
            request,
            Path(artifact_root),
        )

    def _read_artifact_sync(
        self,
        request: ArtifactReadRequest,
        artifact_root: Path,
    ) -> ArtifactReadResult:
        root = self._canonical_root(artifact_root)
        with self._connect() as conn:
            row = self._require_record_conn(conn, str(request.artifact_id))
            provenance = self._provenance_for_row(conn, row)
        record = self._record_from_row(row, provenance=provenance)
        binding = request.binding
        if (
            record.assignment_id != binding.assignment_id
            or record.session_id != binding.session_id
            or record.application_id != binding.application_id
            or record.run_id != binding.run_id
        ):
            raise PlatformBlackboxArtifactScopeDenied(
                "artifact does not belong to the requested assignment, application, and run"
            )
        if record.root_path != root:
            raise PlatformBlackboxArtifactScopeDenied(
                "artifact root does not belong to the requested run"
            )
        artifact_file = self._contained_file(root, record.relative_path)
        size = artifact_file.stat().st_size
        if size > self.hard_max_bytes:
            raise PlatformBlackboxArtifactTooLarge(
                f"artifact size {size} exceeds the {self.hard_max_bytes} byte limit"
            )
        raw = artifact_file.read_bytes()
        if len(raw) > self.hard_max_bytes:
            raise PlatformBlackboxArtifactTooLarge(
                f"artifact size {len(raw)} exceeds the {self.hard_max_bytes} byte limit"
            )
        digest = _digest(raw)
        if len(raw) != record.size_bytes or not hmac.compare_digest(digest, record.sha256):
            raise PlatformBlackboxArtifactIntegrityError(
                "artifact content no longer matches its immutable registry record"
            )

        # Verify the complete immutable artifact before projecting any requested
        # range.  The model-facing wire remains bounded without weakening the
        # registered artifact size or digest contract.
        if request.offset_bytes > record.size_bytes:
            raise PlatformBlackboxArtifactRangeInvalid(
                "artifact offset_bytes exceeds the immutable artifact size"
            )
        end = min(record.size_bytes, request.offset_bytes + request.max_bytes)
        chunk = raw[request.offset_bytes:end]
        try:
            content = chunk.decode("utf-8")
            encoding: Literal["utf8", "base64"] = "utf8"
        except UnicodeDecodeError:
            content = base64.b64encode(chunk).decode("ascii")
            encoding = "base64"
        complete = end == record.size_bytes
        return ArtifactReadResult(
            artifact_id=record.artifact_id,
            assignment_id=record.assignment_id,
            session_id=record.session_id,
            application_id=record.application_id,
            run_id=record.run_id,
            relative_path=record.relative_path,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            offset_bytes=request.offset_bytes,
            chunk_size_bytes=len(chunk),
            next_offset_bytes=None if complete else end,
            complete=complete,
            encoding=encoding,
            content=content,
        )

    def _effective_limit(self, requested: int | None) -> int:
        if requested is None:
            return self.hard_max_bytes
        if requested < 1:
            raise ValueError("max_bytes must be positive")
        return min(requested, self.hard_max_bytes)

    @staticmethod
    def _canonical_root(root: Path) -> Path:
        try:
            resolved = root.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise PlatformBlackboxArtifactPathUnsafe(
                "server-provided artifact root does not exist"
            ) from error
        if not resolved.is_dir():
            raise PlatformBlackboxArtifactPathUnsafe(
                "server-provided artifact root is not a directory"
            )
        return resolved

    @staticmethod
    def _contained_file(root: Path, relative_path: str) -> Path:
        try:
            safe_relative = _validate_relative_path(relative_path)
        except ValueError as error:
            raise PlatformBlackboxArtifactPathUnsafe(str(error)) from error
        candidate = root.joinpath(*PurePosixPath(safe_relative).parts)
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError) as error:
            raise PlatformBlackboxArtifactNotFound("artifact file was not found") from error
        if resolved == root or root not in resolved.parents:
            raise PlatformBlackboxArtifactPathUnsafe(
                "artifact path or symlink escapes the server-provided root"
            )
        if not resolved.is_file():
            raise PlatformBlackboxArtifactNotFound("artifact path is not a regular file")
        return resolved

    @staticmethod
    def _require_record_conn(conn: sqlite3.Connection, artifact_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM platform_blackbox_artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise PlatformBlackboxArtifactNotFound(f"artifact not found: {artifact_id}")
        return row

    @staticmethod
    def _provenance_for_row(
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ArtifactProvenance:
        persisted = conn.execute(
            """
            SELECT evidence_kind,provenance_json
            FROM platform_blackbox_artifact_provenance WHERE artifact_id=?
            """,
            (str(row["artifact_id"]),),
        ).fetchone()
        if persisted is not None:
            provenance = ArtifactProvenance.model_validate_json(
                str(persisted["provenance_json"])
            )
            if provenance.evidence_kind != str(persisted["evidence_kind"]):
                raise PlatformBlackboxArtifactIntegrityError(
                    "artifact provenance kind is inconsistent"
                )
            return provenance
        # Pre-provenance v1 rows remain ordinary artifacts.  They can never be
        # upgraded into host receipts because both registries are immutable.
        return ArtifactProvenance(
            evidence_kind="artifact",
            source="platform_artifact_scan",
            assignment_id=row["assignment_id"],
            session_id=row["session_id"],
            application_id=row["application_id"],
            run_id=row["run_id"],
        )

    @staticmethod
    def _record_from_row(
        row: sqlite3.Row,
        *,
        provenance: ArtifactProvenance,
    ) -> ArtifactRecord:
        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            assignment_id=row["assignment_id"],
            session_id=row["session_id"],
            application_id=row["application_id"],
            run_id=row["run_id"],
            root_path=Path(row["root_path"]),
            relative_path=row["relative_path"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            evidence_kind=provenance.evidence_kind,
            provenance=provenance,
            created_at=_parse_utc(row["created_at"]),
        )
