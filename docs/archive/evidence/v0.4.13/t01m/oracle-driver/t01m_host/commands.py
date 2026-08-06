import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .util import OracleError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="strict")


def run(
    argv: Iterable[object],
    *,
    timeout: float = 120.0,
    input_bytes: Optional[bytes] = None,
    env: Optional[Mapping[str, str]] = None,
    check: bool = True,
) -> CommandResult:
    normalized = tuple(str(item) for item in argv)
    if not normalized or not Path(normalized[0]).is_absolute():
        raise OracleError("oracle commands require an absolute executable path")
    completed = subprocess.run(
        normalized,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=None if env is None else dict(env),
        check=False,
    )
    result = CommandResult(
        normalized, completed.returncode, completed.stdout, completed.stderr
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")[-4000:]
        raise OracleError(
            f"command failed ({completed.returncode}): {normalized!r}\n{stderr}"
        )
    return result
