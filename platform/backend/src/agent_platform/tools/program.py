from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .base import Tool, ToolContext, ToolResult


class ProgramProfilePackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    integrity: str | None = None
    source: str | None = None


class ProgramProfile(BaseModel):
    """Owner-registered contract for one exact customer program surface."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    profile_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=2000)
    executable: str = Field(min_length=1, max_length=1000)
    fixed_arguments: list[str] = Field(default_factory=list, max_length=100)
    allowed_argument_prefixes: list[list[str]] = Field(min_length=1, max_length=200)
    write_argument_prefixes: list[list[str]] = Field(default_factory=list, max_length=100)
    allowed_environment: list[str] = Field(default_factory=list, max_length=100)
    allowed_network_hosts: list[str] = Field(default_factory=list, max_length=100)
    output_formats: list[Literal["json", "text"]] = Field(default_factory=lambda: ["json"])
    max_timeout_seconds: float = Field(default=120, ge=1, le=1800)
    package: ProgramProfilePackage | None = None

    @field_validator("executable")
    @classmethod
    def validate_executable(cls, value: str) -> str:
        path = PurePosixPath(value)
        if ".." in path.parts:
            raise ValueError("profile executable cannot contain '..'")
        return value

    @field_validator(
        "fixed_arguments",
        "allowed_environment",
        "allowed_network_hosts",
        mode="after",
    )
    @classmethod
    def unique_strings(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("profile list values cannot be empty")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_prefixes(self) -> "ProgramProfile":
        if any(not prefix or any(not part for part in prefix) for prefix in self.allowed_argument_prefixes):
            raise ValueError("allowed argument prefixes cannot be empty")
        allowed = {tuple(prefix) for prefix in self.allowed_argument_prefixes}
        for prefix in self.write_argument_prefixes:
            if not prefix:
                raise ValueError("write argument prefixes cannot be empty")
            if not any(tuple(prefix[: len(candidate)]) == candidate for candidate in allowed):
                raise ValueError("every write prefix must be covered by an allowed prefix")
        if len(set(self.output_formats)) != len(self.output_formats):
            raise ValueError("output formats must be unique")
        return self

    def public_contract(self) -> dict[str, Any]:
        """Return the non-secret contract builders need to configure a workflow."""

        return self.model_dump(mode="json")


class ProgramInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=120)
    arguments: list[str] = Field(default_factory=list, max_length=200)
    stdin: Any | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    output_format: Literal["json", "text"] = "json"
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=500)
    timeout: float | None = Field(default=None, ge=1, le=1800)

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: list[str]) -> list[str]:
        if any("\x00" in item for item in value):
            raise ValueError("arguments cannot contain NUL bytes")
        return value


_PROGRAM_RUNNER = r"""
import json, os, subprocess, sys
envelope = json.loads(sys.stdin.read())
environment = os.environ.copy()
environment.update({str(key): str(value) for key, value in envelope["environment"].items()})
completed = subprocess.run(
    sys.argv[1:],
    input=envelope["stdin"].encode("utf-8") if envelope["stdin"] is not None else None,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=environment,
    check=False,
)
print(json.dumps({
    "stdout": completed.stdout.decode("utf-8", errors="replace"),
    "stderr": completed.stderr.decode("utf-8", errors="replace"),
    "exit_code": completed.returncode,
}, ensure_ascii=False))
"""


class ProgramTool(Tool):
    name = "Program"
    description = (
        "Run one owner-registered, pinned customer program using exact arguments, governed "
        "environment keys, structured stdin/output, network scope, and write idempotency."
    )
    input_model = ProgramInput
    dangerous = True
    mutating = True

    def __init__(self, profiles_file: Path | None = None) -> None:
        self._profiles = self._load_profiles(profiles_file)

    @staticmethod
    def _load_profiles(path: Path | None) -> dict[str, ProgramProfile]:
        if path is None:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_profiles = payload if isinstance(payload, list) else [payload]
        profiles: dict[str, ProgramProfile] = {}
        for raw in raw_profiles:
            profile = ProgramProfile.model_validate(raw)
            if profile.profile_id in profiles:
                raise ValueError(f"duplicate program profile: {profile.profile_id}")
            profiles[profile.profile_id] = profile
        return profiles

    def public_profiles(self) -> list[dict[str, Any]]:
        return [
            self._profiles[profile_id].public_contract()
            for profile_id in sorted(self._profiles)
        ]

    def network_hosts_for(self, profile_id: str) -> list[str]:
        profile = self._profiles.get(profile_id)
        return list(profile.allowed_network_hosts) if profile else []

    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            args = ProgramInput.model_validate(data)
            profile = self._profiles.get(args.profile_id)
            if profile is None:
                raise PermissionError(f"unknown program profile: {args.profile_id}")
            self._validate_request(args, profile, context)
            stdin = self._encode_stdin(args.stdin)
            executable = self._resolve_executable(profile.executable)
            request_digest = self._request_digest(args, profile, stdin)
            runner_input = json.dumps(
                {"environment": args.environment, "stdin": stdin},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            result = await context.sandbox.run(
                [
                    "python",
                    "-c",
                    _PROGRAM_RUNNER,
                    executable,
                    *profile.fixed_arguments,
                    *args.arguments,
                ],
                stdin=runner_input,
                timeout=args.timeout or profile.max_timeout_seconds,
            )
            if result.exit_code != 0:
                raise RuntimeError(
                    self._redact(
                        result.stderr or result.stdout or "program runner failed",
                        args.environment,
                    )
                )
            envelope = json.loads(result.stdout)
            exit_code = int(envelope["exit_code"])
            stdout = self._redact(str(envelope.get("stdout", "")), args.environment)
            stderr = self._redact(str(envelope.get("stderr", "")), args.environment)
            parsed_output: Any = stdout
            parse_error: str | None = None
            if args.output_format == "json" and stdout.strip():
                try:
                    parsed_output = json.loads(stdout)
                except json.JSONDecodeError as error:
                    parse_error = f"invalid_json_output:{error.msg}"
            receipt = {
                "profile_id": profile.profile_id,
                "profile_version": profile.version,
                "request_digest": request_digest,
                "exit_code": exit_code,
                "output_format": args.output_format,
                "write": self._is_write(args.arguments, profile),
                "idempotency_key_digest": (
                    hashlib.sha256(args.idempotency_key.encode()).hexdigest()
                    if args.idempotency_key
                    else None
                ),
            }
            error_class: str | None = None
            if exit_code != 0:
                error_class = self._classify_program_failure(stderr)
            elif parse_error is not None:
                error_class = parse_error
            content = {
                "ok": error_class is None,
                "data": parsed_output,
                "stderr": stderr or None,
                "error_class": error_class,
                "receipt": receipt,
            }
            return ToolResult(
                json.dumps(content, ensure_ascii=False, separators=(",", ":")),
                error_class == "transient_program_error",
            )
        except PermissionError as error:
            return ToolResult(
                json.dumps(
                    {"ok": False, "error_class": "permission_denied", "message": str(error)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                False,
            )
        except Exception as error:
            return ToolResult(
                json.dumps(
                    {"ok": False, "error_class": "program_error", "message": str(error)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                True,
            )

    @staticmethod
    def _resolve_executable(value: str) -> str:
        return value if value.startswith("/") else f"/workspace/{value}"

    @staticmethod
    def _matches_prefix(arguments: list[str], prefix: list[str]) -> bool:
        return len(arguments) >= len(prefix) and arguments[: len(prefix)] == prefix

    @classmethod
    def _is_write(cls, arguments: list[str], profile: ProgramProfile) -> bool:
        return any(cls._matches_prefix(arguments, prefix) for prefix in profile.write_argument_prefixes)

    @classmethod
    def _validate_request(
        cls,
        args: ProgramInput,
        profile: ProgramProfile,
        context: ToolContext,
    ) -> None:
        if not any(
            cls._matches_prefix(args.arguments, prefix)
            for prefix in profile.allowed_argument_prefixes
        ):
            raise PermissionError("arguments are outside the registered program profile")
        unknown_environment = sorted(set(args.environment) - set(profile.allowed_environment))
        if unknown_environment:
            raise PermissionError(
                f"environment keys are outside the registered profile: {unknown_environment}"
            )
        if args.output_format not in profile.output_formats:
            raise PermissionError("output format is outside the registered program profile")
        timeout = args.timeout or profile.max_timeout_seconds
        if timeout > profile.max_timeout_seconds:
            raise PermissionError("timeout exceeds the registered program profile")
        if cls._is_write(args.arguments, profile) and not args.idempotency_key:
            raise PermissionError("registered write command requires an idempotency key")
        if profile.allowed_network_hosts:
            if context.agent.network_policy.value == "none":
                raise PermissionError("program profile requires network but the run denies network")
            if context.agent.network_policy.value == "allowlist":
                missing = sorted(
                    set(profile.allowed_network_hosts) - set(context.agent.network_allowlist)
                )
                if missing:
                    raise PermissionError(
                        f"program profile network hosts are outside the run allowlist: {missing}"
                    )

    @staticmethod
    def _encode_stdin(value: Any | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _redact(value: str, environment: dict[str, str]) -> str:
        redacted = value
        for secret in sorted(
            {item for item in environment.values() if item},
            key=len,
            reverse=True,
        ):
            redacted = redacted.replace(secret, "***")
        return redacted

    @staticmethod
    def _classify_program_failure(stderr: str) -> str:
        normalized = stderr.casefold()
        if any(
            marker in normalized
            for marker in (
                "authentication required",
                "unauthorized",
                "forbidden",
                "permission denied",
                "access denied",
                "http 401",
                "http 403",
            )
        ):
            return "permission_denied"
        if any(
            marker in normalized
            for marker in (
                "temporarily unavailable",
                "temporary failure",
                "connection reset",
                "connection refused",
                "timed out",
                "timeout",
                "http 429",
                "http 502",
                "http 503",
                "http 504",
                "econnreset",
                "econnrefused",
                "etimedout",
            )
        ):
            return "transient_program_error"
        return "program_exit_nonzero"

    @staticmethod
    def _request_digest(
        args: ProgramInput,
        profile: ProgramProfile,
        stdin: str | None,
    ) -> str:
        non_secret = {
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "arguments": args.arguments,
            "environment_keys": sorted(args.environment),
            "stdin_sha256": hashlib.sha256((stdin or "").encode()).hexdigest(),
            "output_format": args.output_format,
            "idempotency_key": args.idempotency_key,
        }
        encoded = json.dumps(
            non_secret,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()
