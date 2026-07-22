from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_platform.platform_contract_version import (
    PlatformContractSchemaDrift,
    PlatformContractVersionRollback,
    PlatformContractVersionStore,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


@pytest.mark.asyncio
async def test_contract_version_is_monotonic_and_durable_across_store_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent_platform.db"
    store = PlatformContractVersionStore(db_path)
    assert await store.initialize() == {"schema_version": 1}

    first = await store.observe(contract_version=1, schema_digest=DIGEST_A)
    assert first.highest_contract_version == 1
    assert first.schema_digest == DIGEST_A

    restarted = PlatformContractVersionStore(db_path)
    assert await restarted.initialize() == {"schema_version": 1}
    replay = await restarted.observe(contract_version=1, schema_digest=DIGEST_A)
    assert replay == first

    advanced = await restarted.observe(contract_version=2, schema_digest=DIGEST_B)
    assert advanced.highest_contract_version == 2
    assert advanced.schema_digest == DIGEST_B
    assert advanced.first_observed_at == first.first_observed_at
    assert advanced.updated_at >= first.updated_at

    after_upgrade = PlatformContractVersionStore(db_path)
    await after_upgrade.initialize()
    with pytest.raises(PlatformContractVersionRollback) as rollback:
        await after_upgrade.observe(contract_version=1, schema_digest=DIGEST_A)
    assert rollback.value.highest_version == 2
    assert rollback.value.attempted_version == 1

    persisted = await after_upgrade.observe(contract_version=2, schema_digest=DIGEST_B)
    assert persisted.highest_contract_version == 2
    assert persisted.schema_digest == DIGEST_B

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT contract_version,schema_digest "
            "FROM platform_contract_version_history ORDER BY contract_version"
        ).fetchall() == [(1, DIGEST_A), (2, DIGEST_B)]


@pytest.mark.asyncio
async def test_same_contract_version_rejects_schema_digest_drift_without_mutation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent_platform.db"
    store = PlatformContractVersionStore(db_path)
    await store.initialize()
    original = await store.observe(contract_version=1, schema_digest=DIGEST_A)

    with pytest.raises(PlatformContractSchemaDrift) as drift:
        await store.observe(contract_version=1, schema_digest=DIGEST_B)
    assert drift.value.contract_version == 1
    assert drift.value.expected_digest == DIGEST_A
    assert drift.value.actual_digest == DIGEST_B

    restarted = PlatformContractVersionStore(db_path)
    await restarted.initialize()
    persisted = await restarted.observe(contract_version=1, schema_digest=DIGEST_A)
    assert persisted == original


@pytest.mark.asyncio
async def test_first_observation_preserves_contract_version_one_in_existing_platform_db(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "agent_platform.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE existing_platform_data(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO existing_platform_data(id) VALUES (1)")

    store = PlatformContractVersionStore(db_path)
    await store.initialize()
    state = await store.observe(contract_version=1, schema_digest=DIGEST_A)

    assert state.highest_contract_version == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT id FROM existing_platform_data").fetchall() == [(1,)]
        assert connection.execute(
            "SELECT highest_contract_version,schema_digest "
            "FROM platform_contract_version_state WHERE singleton=1"
        ).fetchone() == (1, DIGEST_A)


def test_contract_version_gate_is_shared_by_independent_processes(tmp_path: Path) -> None:
    db_path = tmp_path / "agent_platform.db"
    script = """
import asyncio
import sys
from pathlib import Path
from agent_platform.platform_contract_version import (
    PlatformContractVersionRollback,
    PlatformContractVersionStore,
)

async def main():
    store = PlatformContractVersionStore(Path(sys.argv[1]))
    await store.initialize()
    try:
        state = await store.observe(
            contract_version=int(sys.argv[2]),
            schema_digest=sys.argv[3],
        )
    except PlatformContractVersionRollback:
        print("rollback")
        raise SystemExit(23)
    print(f"{state.highest_contract_version}:{state.schema_digest}")

asyncio.run(main())
"""

    first = subprocess.run(
        [sys.executable, "-c", script, str(db_path), "1", DIGEST_A],
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == f"1:{DIGEST_A}"

    upgraded = subprocess.run(
        [sys.executable, "-c", script, str(db_path), "2", DIGEST_B],
        capture_output=True,
        text=True,
        check=False,
    )
    assert upgraded.returncode == 0, upgraded.stderr
    assert upgraded.stdout.strip() == f"2:{DIGEST_B}"

    rollback = subprocess.run(
        [sys.executable, "-c", script, str(db_path), "1", DIGEST_A],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rollback.returncode == 23, rollback.stderr
    assert rollback.stdout.strip() == "rollback"
