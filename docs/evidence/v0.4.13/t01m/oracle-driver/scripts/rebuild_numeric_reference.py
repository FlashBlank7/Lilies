#!/usr/bin/env python3
"""Deterministically rebuild the locked JBR NumericReference classes twice."""

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "numeric-reference/NumericReference.java"
EXPECTED_CLASSES = {
    "NumericReference.class",
    "NumericReference$Box.class",
    "NumericReference$CharacterResult.class",
    "NumericReference$PixelScore.class",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compile_once(javac: Path, destination: Path) -> dict[str, str]:
    subprocess.run(
        [
            str(javac),
            "-encoding",
            "UTF-8",
            "--release",
            "17",
            "-d",
            str(destination),
            str(SOURCE),
        ],
        check=True,
    )
    names = {path.name for path in destination.glob("*.class")}
    if names != EXPECTED_CLASSES:
        raise RuntimeError(f"unexpected NumericReference class set: {sorted(names)}")
    return {
        name: digest(destination / name) for name in sorted(EXPECTED_CLASSES)
    }


def atomic_copy(source: Path, destination: Path) -> None:
    descriptor, temporary_text = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_text)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(source.read_bytes())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    jbr = Path(os.environ.get("T01M_JBR_HOME", "/__t01m_missing_jbr__"))
    javac = jbr / "bin/javac"
    release = jbr / "release"
    lock = json.loads((ROOT / "oracle-lock.json").read_text(encoding="utf-8"))
    if (
        not javac.is_file()
        or digest(javac) != lock["toolchain"]["jbr_javac_sha256"]
        or not release.is_file()
        or digest(release) != lock["toolchain"]["jbr_release_sha256"]
    ):
        raise RuntimeError("locked JBR javac/release binding mismatch")
    with tempfile.TemporaryDirectory(prefix="t01m-numeric-a-") as first_text:
        with tempfile.TemporaryDirectory(prefix="t01m-numeric-b-") as second_text:
            first = Path(first_text)
            second = Path(second_text)
            first_hashes = compile_once(javac, first)
            second_hashes = compile_once(javac, second)
            if first_hashes != second_hashes:
                raise RuntimeError("two NumericReference rebuilds differ")
            destination = ROOT / "numeric-reference"
            for name in sorted(EXPECTED_CLASSES):
                atomic_copy(first / name, destination / name)
    print(
        json.dumps(
            {
                "schema_version": 1,
                "source_sha256": digest(SOURCE),
                "class_sha256": first_hashes,
                "independent_rebuild_count": 2,
                "byte_identical": True,
                "result": "pass",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
