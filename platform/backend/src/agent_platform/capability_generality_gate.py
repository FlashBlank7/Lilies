from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit


DEFAULT_PORTFOLIO_PATH = PurePosixPath(
    "docs/experiments/lilies-collaboration/portfolio-v04-13-t01h.json"
)

_PLATFORM_PRODUCT_PREFIXES = (
    "platform/backend/src/",
    "platform/frontend/app/",
    "platform/frontend/components/",
    "platform/frontend/lib/",
)
_GENERIC_RUNNER_PATHS = frozenset(
    {
        "scripts/run_v04_13_codex_builder.py",
    }
)
_PROJECT_DATA_PREFIXES = (
    "docs/",
    "scripts/experiments/",
)
_TEXT_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".conf",
        ".cpp",
        ".css",
        ".go",
        ".graphql",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".md",
        ".mjs",
        ".py",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_GENERIC_HOST_WORDS = frozenset(
    {
        "app",
        "application",
        "assistant",
        "actual",
        "budget",
        "client",
        "community",
        "core",
        "edition",
        "enterprise",
        "home",
        "platform",
        "project",
        "server",
    }
)
_HOST_IMPLEMENTATION_MARKERS = frozenset(
    {
        "adapter",
        "adapters",
        "catalog",
        "connector",
        "endpoint",
        "field_map",
        "field_mapper",
        "field_mapping",
        "manifest",
        "mapper",
        "mapping",
        "mappings",
        "operation",
        "operations",
        "schema",
        "schemas",
        "wrapper",
        "wrappers",
    }
)
_EXPLICIT_FINAL_WORKFLOW_MARKERS = (
    "final_graph",
    "final_workflow",
    "prebuilt_workflow",
)
_MAX_POLICY_FILE_BYTES = 2 * 1024 * 1024


class CapabilityGeneralityConfigurationError(ValueError):
    """The portfolio-backed capability-generality policy is unavailable."""


class CapabilityGeneralityViolation(RuntimeError):
    """A CAP source delta contains project- or host-specific implementation."""

    def __init__(self, findings: tuple[CapabilityGeneralityFinding, ...]) -> None:
        if not findings:
            raise ValueError("capability generality violation requires findings")
        self.findings = findings
        super().__init__(
            "capability source is not generic: "
            + "; ".join(finding.public_detail for finding in findings)
        )


@dataclass(frozen=True)
class CapabilityHostMarker:
    project_id: str
    marker: str
    source: str


@dataclass(frozen=True)
class CapabilityGeneralityFinding:
    project_id: str
    marker: str
    marker_source: str
    path: str
    reason: str
    matched_constructs: tuple[str, ...] = ()
    line: int | None = None

    @property
    def public_detail(self) -> str:
        location = self.path if self.line is None else f"{self.path}:{self.line}"
        constructs = (
            ""
            if not self.matched_constructs
            else f", constructs={','.join(self.matched_constructs)}"
        )
        return (
            f"path={location}, project={self.project_id}, marker={self.marker}, "
            f"reason={self.reason}{constructs}"
        )


@dataclass(frozen=True)
class CapabilityGeneralityResult:
    findings: tuple[CapabilityGeneralityFinding, ...]

    @property
    def passed(self) -> bool:
        return not self.findings


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9]+", "_", folded).strip("_")


def _safe_relative_path(value: str) -> str:
    normalized = PurePosixPath(value).as_posix()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized == "."
        or not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
    ):
        raise CapabilityGeneralityConfigurationError(
            "capability generality policy contains an unsafe path"
        )
    return normalized


def _read_policy_json(repository_root: Path, relative_path: str) -> dict[str, Any]:
    root = repository_root.resolve(strict=True)
    safe_path = _safe_relative_path(relative_path)
    candidate = root.joinpath(*PurePosixPath(safe_path).parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise CapabilityGeneralityConfigurationError(
            f"capability generality policy file is unavailable: {safe_path}"
        )
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise CapabilityGeneralityConfigurationError(
            "capability generality policy escaped the repository"
        )
    payload = candidate.read_bytes()
    if len(payload) > _MAX_POLICY_FILE_BYTES:
        raise CapabilityGeneralityConfigurationError(
            f"capability generality policy file is too large: {safe_path}"
        )
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CapabilityGeneralityConfigurationError(
            f"capability generality policy is not valid JSON: {safe_path}"
        ) from error
    if not isinstance(value, dict):
        raise CapabilityGeneralityConfigurationError(
            f"capability generality policy is not an object: {safe_path}"
        )
    return value


def _repository_identity_candidates(repository_url: str) -> set[str]:
    parsed = urlsplit(repository_url)
    path = parsed.path
    if not path and ":" in repository_url:
        path = repository_url.rsplit(":", 1)[-1]
    parts = [part for part in PurePosixPath(path.strip("/")).parts if part]
    if not parts:
        return set()
    stem = parts[-1]
    if stem.casefold().endswith(".git"):
        stem = stem[:-4]
    candidates = {stem}
    if len(parts) >= 2:
        candidates.add(parts[-2])
    return candidates


def _host_marker_candidates(name: str, repository_url: str) -> set[str]:
    candidates: set[str] = set()
    normalized_name = _normalize(name)
    if normalized_name:
        candidates.add(normalized_name)
        name_tokens = normalized_name.split("_")
        trimmed = "_".join(
            token for token in name_tokens if token not in _GENERIC_HOST_WORDS
        )
        if trimmed:
            candidates.add(trimmed)
        candidates.update(
            token
            for token in name_tokens
            if len(token) >= 7 and token not in _GENERIC_HOST_WORDS
        )
    for repository_identity in _repository_identity_candidates(repository_url):
        normalized = _normalize(repository_identity)
        if normalized and normalized not in _GENERIC_HOST_WORDS:
            candidates.add(normalized)
    return {
        candidate
        for candidate in candidates
        if len(candidate.replace("_", "")) >= 6
        and candidate not in _GENERIC_HOST_WORDS
    }


def _contains_marker(normalized_surface: str, marker: str) -> bool:
    if not marker:
        return False
    bounded = f"_{normalized_surface}_"
    if f"_{marker}_" in bounded:
        return True
    compact_marker = marker.replace("_", "")
    compact_surface = normalized_surface.replace("_", "")
    return len(compact_marker) >= 8 and compact_marker in compact_surface


def _added_text(old_text: str, new_text: str) -> str:
    matcher = SequenceMatcher(
        a=old_text.splitlines(),
        b=new_text.splitlines(),
        autojunk=False,
    )
    additions: list[str] = []
    new_lines = new_text.splitlines()
    for tag, _old_start, _old_end, new_start, new_end in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            additions.extend(new_lines[new_start:new_end])
    return "\n".join(additions)


def _explicit_final_workflow(text: str) -> bool:
    folded = unicodedata.normalize("NFKC", text).casefold()
    normalized = _normalize(folded)
    if any(
        _contains_marker(normalized, marker)
        for marker in _EXPLICIT_FINAL_WORKFLOW_MARKERS
    ):
        return True
    quoted_nodes = re.search(r"""[\"']nodes[\"']\s*:""", folded)
    quoted_edges = re.search(r"""[\"']edges[\"']\s*:""", folded)
    if quoted_nodes and quoted_edges:
        return True
    workflow_spec = re.search(r"\bworkflow_?spec\s*\(", folded)
    assigned_nodes = re.search(r"\bnodes\s*=", folded)
    assigned_edges = re.search(r"\bedges\s*=", folded)
    return bool(workflow_spec and assigned_nodes and assigned_edges)


def _source_scope(path: str) -> str | None:
    if path.startswith(_PLATFORM_PRODUCT_PREFIXES):
        return "platform_product_source"
    if path in _GENERIC_RUNNER_PATHS:
        return "generic_builder_runner"
    return None


def _marker_line(
    normalized_lines: tuple[str, ...],
    marker: str,
) -> int | None:
    for number, line in enumerate(normalized_lines, start=1):
        if _contains_marker(line, marker):
            return number
    return None


class CapabilityGeneralityGate:
    """Keep project instances out of reusable platform capability source.

    Host names belong in project manifests, connector registrations, secrets,
    and experiment/environment data.  They do not belong in the platform
    product implementation or in the generic Builder runner promoted as CAP
    work.
    """

    def __init__(self, markers: tuple[CapabilityHostMarker, ...]) -> None:
        if not markers:
            raise CapabilityGeneralityConfigurationError(
                "capability generality policy contains no host markers"
            )
        self._markers = tuple(
            sorted(
                set(markers),
                key=lambda item: (
                    item.marker,
                    item.project_id,
                    item.source,
                ),
            )
        )

    @property
    def markers(self) -> tuple[CapabilityHostMarker, ...]:
        return self._markers

    @classmethod
    def from_project_manifests(
        cls,
        manifests: Mapping[str, Mapping[str, Any]],
    ) -> CapabilityGeneralityGate:
        markers: list[CapabilityHostMarker] = []
        for manifest_key, manifest in sorted(manifests.items()):
            project_id = str(manifest.get("project_id") or manifest_key).strip()
            host_projects = manifest.get("host_projects")
            if (
                not project_id
                or not isinstance(host_projects, list)
                or not host_projects
            ):
                raise CapabilityGeneralityConfigurationError(
                    f"project manifest has no host projects: {manifest_key}"
                )
            for index, raw_host in enumerate(host_projects):
                if not isinstance(raw_host, Mapping):
                    raise CapabilityGeneralityConfigurationError(
                        f"project manifest host is invalid: {project_id}[{index}]"
                    )
                name = str(raw_host.get("name") or "").strip()
                repository_url = str(
                    raw_host.get("repository")
                    or raw_host.get("repository_url")
                    or ""
                ).strip()
                if not name or not repository_url:
                    raise CapabilityGeneralityConfigurationError(
                        f"project manifest host identity is incomplete: "
                        f"{project_id}[{index}]"
                    )
                for marker in _host_marker_candidates(name, repository_url):
                    markers.append(
                        CapabilityHostMarker(
                            project_id=project_id,
                            marker=marker,
                            source=f"host_projects[{index}]",
                        )
                    )
        return cls(tuple(markers))

    @classmethod
    def from_portfolio(
        cls,
        repository_root: Path,
        portfolio_path: PurePosixPath = DEFAULT_PORTFOLIO_PATH,
    ) -> CapabilityGeneralityGate:
        root = repository_root.resolve(strict=True)
        portfolio = _read_policy_json(root, portfolio_path.as_posix())
        members = portfolio.get("projects")
        if not isinstance(members, list) or not members:
            raise CapabilityGeneralityConfigurationError(
                "capability portfolio contains no projects"
            )
        manifests: dict[str, Mapping[str, Any]] = {}
        allowed_manifest_root = PurePosixPath(
            "docs/experiments/lilies-collaboration"
        )
        for index, member in enumerate(members):
            if not isinstance(member, Mapping):
                raise CapabilityGeneralityConfigurationError(
                    f"capability portfolio member is invalid: {index}"
                )
            project_id = str(member.get("project_id") or "").strip()
            manifest_path = _safe_relative_path(str(member.get("manifest") or ""))
            pure_manifest = PurePosixPath(manifest_path)
            if (
                not project_id
                or not pure_manifest.is_relative_to(allowed_manifest_root)
                or pure_manifest.name != "project.json"
            ):
                raise CapabilityGeneralityConfigurationError(
                    f"capability portfolio member path is invalid: {index}"
                )
            manifest = _read_policy_json(root, manifest_path)
            if manifest.get("project_id") != project_id:
                raise CapabilityGeneralityConfigurationError(
                    f"capability portfolio member identity changed: {project_id}"
                )
            manifests[project_id] = manifest
        return cls.from_project_manifests(manifests)

    @classmethod
    def from_repository(
        cls,
        repository_root: Path,
    ) -> CapabilityGeneralityGate:
        """Discover every selected real-project portfolio in the repository."""

        root = repository_root.resolve(strict=True)
        portfolio_root = root / "docs/experiments/lilies-collaboration"
        if portfolio_root.is_symlink() or not portfolio_root.is_dir():
            raise CapabilityGeneralityConfigurationError(
                "capability portfolio directory is unavailable"
            )
        portfolio_paths = sorted(
            path
            for path in portfolio_root.glob("portfolio-*.json")
            if path.is_file() and not path.is_symlink()
        )
        if not portfolio_paths:
            raise CapabilityGeneralityConfigurationError(
                "no real-project capability portfolio is available"
            )
        markers: list[CapabilityHostMarker] = []
        for portfolio_path in portfolio_paths:
            relative = PurePosixPath(
                portfolio_path.relative_to(root).as_posix()
            )
            markers.extend(
                cls.from_portfolio(root, relative).markers
            )
        return cls(tuple(markers))

    def inspect_delta(
        self,
        changes: Mapping[str, tuple[bytes | None, bytes | None]],
        *,
        include_final_workflow: bool = True,
    ) -> CapabilityGeneralityResult:
        findings: list[CapabilityGeneralityFinding] = []
        for raw_path, (old_payload, new_payload) in sorted(changes.items()):
            path = _safe_relative_path(raw_path)
            scope = _source_scope(path)
            if scope is None or new_payload is None:
                continue
            path_surface = _normalize(path)
            new_text: str | None
            old_text: str
            try:
                new_text = new_payload.decode("utf-8")
                old_text = (
                    old_payload.decode("utf-8")
                    if old_payload is not None
                    else ""
                )
            except UnicodeDecodeError:
                new_text = None
                old_text = ""
            normalized_lines = (
                ()
                if new_text is None
                else tuple(_normalize(line) for line in new_text.splitlines())
            )
            matched_hosts: list[tuple[CapabilityHostMarker, int | None]] = []
            for host_marker in self._markers:
                line = (
                    None
                    if new_text is None
                    else _marker_line(normalized_lines, host_marker.marker)
                )
                if (
                    not _contains_marker(path_surface, host_marker.marker)
                    and line is None
                ):
                    continue
                matched_hosts.append((host_marker, line))
            constructs: tuple[str, ...] = ()
            if matched_hosts:
                content_surface = "_".join(normalized_lines)
                complete_surface = "_".join((path_surface, content_surface))
                constructs = tuple(
                    sorted(
                        marker
                        for marker in _HOST_IMPLEMENTATION_MARKERS
                        if _contains_marker(complete_surface, marker)
                    )
                )
            for host_marker, line in matched_hosts:
                findings.append(
                    CapabilityGeneralityFinding(
                        project_id=host_marker.project_id,
                        marker=host_marker.marker,
                        marker_source=host_marker.source,
                        path=path,
                        reason=(
                            "host_marker_in_platform_product_source"
                            if scope == "platform_product_source"
                            else "host_marker_in_generic_builder_runner"
                        ),
                        matched_constructs=constructs,
                        line=line,
                    )
                )
            if (
                include_final_workflow
                and new_text is not None
                and _explicit_final_workflow(_added_text(old_text, new_text))
            ):
                findings.append(
                    CapabilityGeneralityFinding(
                        project_id="portfolio",
                        marker="final_workflow",
                        marker_source="source_delta",
                        path=path,
                        reason="prebuilt_final_workflow_in_capability_source",
                    )
                )
        unique = {
            (
                finding.project_id,
                finding.marker,
                finding.marker_source,
                finding.path,
                finding.reason,
                finding.matched_constructs,
                finding.line,
            ): finding
            for finding in findings
        }
        ordered = tuple(
            unique[key]
            for key in sorted(
                unique,
                key=lambda value: tuple(str(item) for item in value),
            )
        )
        return CapabilityGeneralityResult(findings=ordered)

    def require_generic_delta(
        self,
        changes: Mapping[str, tuple[bytes | None, bytes | None]],
    ) -> CapabilityGeneralityResult:
        result = self.inspect_delta(changes)
        if not result.passed:
            raise CapabilityGeneralityViolation(result.findings)
        return result

    def inspect_repository(
        self,
        repository_root: Path,
    ) -> CapabilityGeneralityResult:
        root = repository_root.resolve(strict=True)
        changes: dict[str, tuple[bytes | None, bytes | None]] = {}
        for prefix in _PLATFORM_PRODUCT_PREFIXES:
            source_root = root.joinpath(*PurePosixPath(prefix.rstrip("/")).parts)
            if not source_root.is_dir() or source_root.is_symlink():
                continue
            for candidate in source_root.rglob("*"):
                if (
                    candidate.is_symlink()
                    or not candidate.is_file()
                    or candidate.suffix.casefold() not in _TEXT_SUFFIXES
                ):
                    continue
                path = candidate.relative_to(root).as_posix()
                changes[path] = (None, candidate.read_bytes())
        for relative in _GENERIC_RUNNER_PATHS:
            candidate = root.joinpath(*PurePosixPath(relative).parts)
            if (
                candidate.is_file()
                and not candidate.is_symlink()
                and not relative.startswith(_PROJECT_DATA_PREFIXES)
            ):
                changes[relative] = (None, candidate.read_bytes())
        return self.inspect_delta(changes, include_final_workflow=False)
