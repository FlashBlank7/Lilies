"""Template storage — load, list, search, and register workflow templates.

Templates live as JSON files under ``templates/`` and are loaded at startup.
Published applications can also be registered as templates via the API.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .template_models import Template, TemplateMeta
from .workflow_models import WorkflowSpec

if TYPE_CHECKING:
    from .template_models import ProvenanceSource


class TemplateStore:
    """In-memory template registry backed by JSON files and API registrations."""

    def __init__(self, templates_dir: Path | str | None = None) -> None:
        self._templates: dict[str, Template] = {}
        self._dir = Path(templates_dir) if templates_dir else None
        self._lock = threading.Lock()

    # ── load / reload ──────────────────────────────────────────

    def load_builtins(self, directory: Path | str) -> int:
        """Load all ``.json`` files from *directory* as templates."""
        root = Path(directory)
        if not root.is_dir():
            return 0
        count = 0
        for path in sorted(root.glob("*.json")):
            try:
                template = Template.model_validate_json(path.read_text(encoding="utf-8"))
                self._templates[template.meta.name] = template
                count += 1
            except Exception as exc:
                print(f"[template_store] skip {path.name}: {exc}")
        return count

    # ── CRUD ───────────────────────────────────────────────────

    def list(self, *, category: str | None = None, query: str = "") -> list[TemplateMeta]:
        result: list[TemplateMeta] = []
        needle = query.casefold().strip()
        for t in self._templates.values():
            if category and t.meta.category != category:
                continue
            if needle:
                searchable = " ".join([
                    t.meta.name, t.meta.title, t.meta.description,
                    t.meta.category, *t.meta.tags,
                ]).casefold()
                if needle not in searchable:
                    continue
            result.append(t.meta)
        return sorted(result, key=lambda m: m.title)

    def get(self, name: str) -> Template:
        try:
            return self._templates[name]
        except KeyError:
            raise KeyError(f"template not found: {name}") from None

    def get_workflow(self, name: str) -> WorkflowSpec:
        return self.get(name).workflow

    def register(
        self,
        name: str,
        workflow: WorkflowSpec,
        meta_overrides: dict[str, Any] | None = None,
    ) -> Template:
        """Register (or replace) a template from an existing workflow."""
        overrides = meta_overrides or {}
        with self._lock:
            meta = TemplateMeta(
                name=name,
                title=overrides.get("title", name),
                description=overrides.get("description", ""),
                category=overrides.get("category", "task_management"),
                tags=overrides.get("tags", []),
                icon=overrides.get("icon", "workflow"),
                author=overrides.get("author", "user"),
                version=overrides.get("version", 1),
            )
            template = Template(meta=meta, workflow=workflow)
            self._templates[name] = template
            return template

    def names(self) -> list[str]:
        return sorted(self._templates)

    def record_usage(self, name: str, *, success: bool = True) -> None:
        """Close the recommendation flywheel.

        Called after a build that started from this template completes.
        Updates total_uses, total_successes, and success_rate so that
        quality_score reflects actual downstream value, not just search
        popularity.

        Also applies auto-degradation: repeated failures (>3 consecutive)
        lower the confidence score to prevent unreliable templates from
        being recommended to new builds.
        """
        if name not in self._templates:
            return
        with self._lock:
            meta = self._templates[name].meta
            meta.total_uses += 1
            if success:
                meta.total_successes += 1
                meta.consecutive_failures = 0
            else:
                meta.consecutive_failures += 1
            if meta.total_uses > 0:
                meta.success_rate = round(meta.total_successes / meta.total_uses, 3)

            # Auto-degrade confidence on repeated failures
            # Only activate after enough samples to avoid noise
            MIN_USES = 5
            if meta.total_uses >= MIN_USES:
                if meta.consecutive_failures >= 5:
                    meta.confidence = max(0.15, meta.confidence)
                elif meta.consecutive_failures >= 3:
                    meta.confidence = round(max(0.30, meta.confidence - 0.10), 3)
                elif meta.success_rate < 0.5:
                    meta.confidence = round(max(0.40, meta.confidence - 0.05), 3)

    # ── Evolution support ───────────────────────────────────────

    def snapshot(self, name: str) -> "Template":
        """Return a deep copy of the template for rollback purposes.

        The caller is responsible for storing this snapshot and calling
        rollback() if the evolution attempt fails.
        """
        return self.get(name).model_copy(deep=True)

    def rollback(self, name: str, snapshot: "Template") -> "Template":
        """Restore a template to a previously-saved snapshot.

        This is used when an evolution merge produces an invalid result
        and needs to be reverted.
        """
        with self._lock:
            if name not in self._templates:
                raise KeyError(f"template not found for rollback: {name}")
            self._templates[name] = snapshot.model_copy(deep=True)
            return self._templates[name]

    def evolve(
        self,
        name: str,
        merged_workflow: "WorkflowSpec",
        source: "ProvenanceSource",
    ) -> "Template":
        """Apply an evolved workflow to a template, with confidence update.

        This is the atomic commit step after merge_workflow_graph() has been
        validated. It updates the template's workflow and metadata in one step.
        """
        with self._lock:
            template = self.get(name)
            template.workflow = merged_workflow
            template.meta.provenance.append(source)
            boost = 0.15 if template.meta.confidence < 0.80 else (
                0.10 if template.meta.confidence < 0.90 else 0.03
            )
            template.meta.confidence = round(min(0.99, template.meta.confidence + boost), 3)
            template.meta.version += 1
            template.meta.usage_count += 1
            template.meta.total_uses += 1
            template.meta.total_successes += 1
            if template.meta.total_uses > 0:
                template.meta.success_rate = round(
                    template.meta.total_successes / template.meta.total_uses, 3
                )
            return template

    def __len__(self) -> int:
        return len(self._templates)

    def categories(self) -> list[str]:
        cats: set[str] = set()
        for t in self._templates.values():
            cats.add(t.meta.category)
        return sorted(cats)

    # ── expand ─────────────────────────────────────────────────

    @staticmethod
    def _update_refs(value: Any, id_map: dict[str, str]) -> Any:
        """Recursively update ``$ref.node_id`` in *value* using *id_map*."""
        if isinstance(value, list):
            return [TemplateStore._update_refs(item, id_map) for item in value]
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, dict) and "node_id" in ref:
                ref["node_id"] = id_map.get(ref["node_id"], ref["node_id"])
            return {key: TemplateStore._update_refs(val, id_map) for key, val in value.items()}
        return value

    def expand_into_workflow(
        self,
        name: str,
        *,
        prefix: str = "",
        x: float = 0,
        y: float = 0,
    ) -> WorkflowSpec:
        """Return a copy of the template workflow with prefixed node IDs.

        All node IDs, edge source/target references, and ``$ref.node_id``
        values inside node configurations are rewritten.
        """
        template = self.get(name)
        with self._lock:
            template.meta.usage_count += 1
        wf = template.workflow.model_copy(deep=True)
        if prefix:
            id_map: dict[str, str] = {}
            for node in wf.nodes:
                new_id = f"{prefix}_{node.id}"
                id_map[node.id] = new_id
                node.id = new_id
                # Rewrite $ref references inside node config
                node.config = self._update_refs(node.config, id_map)
            for edge in wf.edges:
                edge.id = f"{prefix}_{edge.id}"
                edge.source = id_map.get(edge.source, edge.source)
                edge.target = id_map.get(edge.target, edge.target)
            # Shift all positions to avoid overlap
            for node in wf.nodes:
                pos = node.position
                pos.x += x
                pos.y += y
        return wf
