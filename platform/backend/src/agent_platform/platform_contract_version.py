from __future__ import annotations

import asyncio
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .lilies_platform_contract import public_contract_schema_digest


SCHEMA_VERSION = 1
MAX_CONTRACT_VERSION = 2**63 - 1
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def platform_contract_schema_digest() -> str:
    """Return the non-contextual public schema generation fingerprint."""

    return public_contract_schema_digest()


@dataclass(frozen=True, slots=True)
class PlatformContractVersionState:
    highest_contract_version: int
    schema_digest: str
    first_observed_at: datetime
    updated_at: datetime


class PlatformContractVersionError(RuntimeError):
    code = "platform_contract_version_error"


class PlatformContractVersionStoreError(PlatformContractVersionError):
    code = "platform_contract_version_store_error"


class PlatformContractVersionRollback(PlatformContractVersionError):
    code = "contract_version_rollback"

    def __init__(self, *, highest_version: int, attempted_version: int) -> None:
        super().__init__(
            f"platform contract version {attempted_version} is below persisted highest "
            f"version {highest_version}"
        )
        self.highest_version = highest_version
        self.attempted_version = attempted_version


class PlatformContractSchemaDrift(PlatformContractVersionError):
    code = "contract_schema_drift"

    def __init__(
        self,
        *,
        contract_version: int,
        expected_digest: str,
        actual_digest: str,
    ) -> None:
        super().__init__(
            f"platform contract version {contract_version} is already bound to a different "
            "public schema digest"
        )
        self.contract_version = contract_version
        self.expected_digest = expected_digest
        self.actual_digest = actual_digest


class PlatformContractVersionStore:
    """Persist and atomically enforce the platform contract's monotonic identity."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        if self.db_path.exists():
            os.chmod(self.db_path, 0o600)
        return connection

    async def initialize(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> dict[str, int]:
        observed_at = _utc_now().isoformat()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS platform_contract_version_schema (
                      version INTEGER PRIMARY KEY,
                      applied_at TEXT NOT NULL
                    )
                    """
                )
                current = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(version),0) AS version "
                        "FROM platform_contract_version_schema"
                    ).fetchone()["version"]
                )
                if current > SCHEMA_VERSION:
                    raise PlatformContractVersionStoreError(
                        f"platform contract version schema {current} is newer than supported "
                        f"{SCHEMA_VERSION}"
                    )
                if current < 1:
                    connection.execute(
                        """
                        CREATE TABLE platform_contract_version_state (
                          singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                          highest_contract_version INTEGER NOT NULL
                            CHECK(highest_contract_version >= 1),
                          schema_digest TEXT NOT NULL,
                          first_observed_at TEXT NOT NULL,
                          updated_at TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE platform_contract_version_history (
                          contract_version INTEGER PRIMARY KEY CHECK(contract_version >= 1),
                          schema_digest TEXT NOT NULL,
                          observed_at TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TRIGGER platform_contract_version_history_no_update
                          BEFORE UPDATE ON platform_contract_version_history
                          BEGIN SELECT RAISE(ABORT, 'platform contract history is immutable'); END
                        """
                    )
                    connection.execute(
                        """
                        CREATE TRIGGER platform_contract_version_history_no_delete
                          BEFORE DELETE ON platform_contract_version_history
                          BEGIN SELECT RAISE(ABORT, 'platform contract history is immutable'); END
                        """
                    )
                    connection.execute(
                        "INSERT INTO platform_contract_version_schema(version,applied_at) "
                        "VALUES (?,?)",
                        (1, observed_at),
                    )
        except PlatformContractVersionError:
            raise
        except sqlite3.Error as error:
            raise PlatformContractVersionStoreError(
                "failed to initialize platform contract version state"
            ) from error
        self._secure_database_files()
        return {"schema_version": SCHEMA_VERSION}

    async def observe(
        self,
        *,
        contract_version: int,
        schema_digest: str,
    ) -> PlatformContractVersionState:
        if (
            not isinstance(contract_version, int)
            or isinstance(contract_version, bool)
            or contract_version < 1
            or contract_version > MAX_CONTRACT_VERSION
        ):
            raise ValueError(
                f"contract_version must be an integer between 1 and {MAX_CONTRACT_VERSION}"
            )
        if not _DIGEST_RE.fullmatch(schema_digest):
            raise ValueError("schema_digest must be a lowercase sha256 digest")
        async with self._lock:
            return await asyncio.to_thread(
                self._observe_sync,
                contract_version,
                schema_digest,
            )

    def _observe_sync(
        self,
        contract_version: int,
        schema_digest: str,
    ) -> PlatformContractVersionState:
        now = _utc_now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM platform_contract_version_state WHERE singleton=1"
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO platform_contract_version_state(
                          singleton,highest_contract_version,schema_digest,
                          first_observed_at,updated_at
                        ) VALUES (1,?,?,?,?)
                        """,
                        (
                            contract_version,
                            schema_digest,
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO platform_contract_version_history(
                          contract_version,schema_digest,observed_at
                        ) VALUES (?,?,?)
                        """,
                        (contract_version, schema_digest, now.isoformat()),
                    )
                else:
                    highest = int(row["highest_contract_version"])
                    expected_digest = str(row["schema_digest"])
                    if contract_version < highest:
                        raise PlatformContractVersionRollback(
                            highest_version=highest,
                            attempted_version=contract_version,
                        )
                    if contract_version == highest and schema_digest != expected_digest:
                        raise PlatformContractSchemaDrift(
                            contract_version=contract_version,
                            expected_digest=expected_digest,
                            actual_digest=schema_digest,
                        )
                    if contract_version > highest:
                        connection.execute(
                            """
                            UPDATE platform_contract_version_state
                            SET highest_contract_version=?,schema_digest=?,updated_at=?
                            WHERE singleton=1
                            """,
                            (contract_version, schema_digest, now.isoformat()),
                        )
                        connection.execute(
                            """
                            INSERT INTO platform_contract_version_history(
                              contract_version,schema_digest,observed_at
                            ) VALUES (?,?,?)
                            """,
                            (contract_version, schema_digest, now.isoformat()),
                        )
                persisted = connection.execute(
                    "SELECT * FROM platform_contract_version_state WHERE singleton=1"
                ).fetchone()
        except PlatformContractVersionError:
            raise
        except sqlite3.Error as error:
            raise PlatformContractVersionStoreError(
                "failed to persist platform contract version state"
            ) from error
        self._secure_database_files()
        if persisted is None:  # pragma: no cover - protected by the transaction above
            raise PlatformContractVersionStoreError("platform contract version state is missing")
        return PlatformContractVersionState(
            highest_contract_version=int(persisted["highest_contract_version"]),
            schema_digest=str(persisted["schema_digest"]),
            first_observed_at=datetime.fromisoformat(str(persisted["first_observed_at"])),
            updated_at=datetime.fromisoformat(str(persisted["updated_at"])),
        )

    def _secure_database_files(self) -> None:
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)
