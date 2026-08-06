import json
import unicodedata
from pathlib import Path
from typing import Any

from .constants import (
    ACCESSIBILITY_CONFIG,
    APPLICATION_ID,
    BRIEF_ID,
    FLOW_CONFIG,
    ORACLE_ID,
    TASK_ID,
)
from .util import OracleError

ALLOWED_ACTIONS = {
    "assert_absent",
    "assert_count",
    "assert_offline_private",
    "assert_order",
    "assert_record_set",
    "assert_visible",
    "back",
    "click",
    "dump",
    "focus_trace",
    "force_stop_relaunch",
    "rotate",
    "screenshot",
    "set_text",
    "set_text_utf16_hex",
    "talkback_next",
    "wait",
}
MUTATIONS = {
    "back",
    "click",
    "force_stop_relaunch",
    "rotate",
    "set_text",
    "set_text_utf16_hex",
    "talkback_next",
}
FORBIDDEN_KEYS = {
    "coordinate",
    "coordinates",
    "x",
    "y",
    "tap_x",
    "tap_y",
    "global_ordinal",
    "index",
}

A08_SCREEN_IDS = [
    "onboarding",
    "empty_library",
    "create_seed",
    "category_chooser",
    "priority_chooser",
    "populated_library",
    "edit_seed",
    "filter_panel",
    "no_match_library",
    "delete_confirmation",
]
A08_ACTIONS = {"back", "build_a06_populated_milestone", "click", "set_text"}


def _card(name: str, category: str, priority: int, status: str) -> str:
    return f"火种：{name}；类别：{category}；优先级：{priority}；状态：{status}"


def _value_argument(group: dict[str, Any], field: str) -> dict[str, str]:
    if f"{field}_ref" in group:
        return {"value_ref": group[f"{field}_ref"]}
    return {"value": group.get(field, "")}


def _resolve_unicode_fixtures(raw: dict[str, Any]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    resolving: set[str] = set()

    def resolve(name: str) -> str:
        if name in resolved:
            return resolved[name]
        if name in resolving or name not in raw:
            raise OracleError(f"cyclic or missing Unicode fixture reference: {name}")
        resolving.add(name)
        fixture = raw[name]
        if isinstance(fixture, str):
            value = fixture
        elif isinstance(fixture, dict) and set(fixture) == {"unit", "repeat"}:
            unit, repeat = fixture["unit"], fixture["repeat"]
            if (
                not isinstance(unit, str)
                or not unit
                or not isinstance(repeat, int)
                or repeat < 1
                or repeat > 301
            ):
                raise OracleError(f"invalid Unicode fixture bounds: {name}")
            value = unit * repeat
        elif isinstance(fixture, dict) and set(fixture) == {"code_points"}:
            encoded = fixture["code_points"]
            if (
                not isinstance(encoded, list)
                or not encoded
                or any(
                    not isinstance(item, str)
                    or len(item) not in (4, 5, 6)
                    or any(character not in "0123456789abcdef" for character in item)
                    or not 0 <= int(item, 16) <= 0x10FFFF
                    or 0xD800 <= int(item, 16) <= 0xDFFF
                    for item in encoded
                )
            ):
                raise OracleError(f"invalid Unicode code-point fixture: {name}")
            value = "".join(chr(int(item, 16)) for item in encoded)
        elif isinstance(fixture, dict) and set(fixture) == {"concat"}:
            parts = fixture["concat"]
            if not isinstance(parts, list) or not parts:
                raise OracleError(f"invalid Unicode concat fixture: {name}")
            values = []
            for part in parts:
                if not isinstance(part, dict):
                    raise OracleError(f"invalid Unicode concat part: {name}")
                if set(part) == {"text"} and isinstance(part["text"], str):
                    values.append(part["text"])
                    continue
                if set(part) in ({"ref"}, {"ref", "reverse"}) and isinstance(
                    part.get("ref"), str
                ):
                    referenced = resolve(part["ref"])
                    if part.get("reverse") is True:
                        referenced = referenced[::-1]
                    elif "reverse" in part:
                        raise OracleError(f"invalid Unicode reverse flag: {name}")
                    values.append(referenced)
                    continue
                raise OracleError(f"invalid Unicode concat part: {name}")
            value = "".join(values)
        else:
            raise OracleError(f"invalid Unicode fixture specification: {name}")
        resolving.remove(name)
        resolved[name] = value
        return value

    for fixture_name in raw:
        resolve(fixture_name)
    return resolved


def load_accessibility_contract(
    path: Path = ACCESSIBILITY_CONFIG,
) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("case_id") != "A08"
        or value.get("application_id") != APPLICATION_ID
        or value.get("font_scales") != [1.0, 2.0]
    ):
        raise OracleError("A08 accessibility contract identity mismatch")
    screens = value.get("screens")
    if (
        not isinstance(screens, list)
        or any(not isinstance(screen, dict) for screen in screens)
        or [screen.get("id") for screen in screens] != A08_SCREEN_IDS
    ):
        raise OracleError("A08 must contain the exact ordered ten-screen matrix")
    for screen in screens:
        expected = screen.get("expected")
        if (
            not isinstance(expected, list)
            or not expected
            or any(
                not isinstance(item, str)
                or not item
                or item.count("|") != 1
                for item in expected
            )
        ):
            raise OracleError(f"A08 screen has invalid focus signatures: {screen['id']}")
        for phase in ("enter", "leave"):
            actions = screen.get(phase, [])
            if not isinstance(actions, list):
                raise OracleError(f"A08 {phase} actions must be a list")
            for action in actions:
                if (
                    not isinstance(action, dict)
                    or action.get("action") not in A08_ACTIONS
                    or any(key in action for key in FORBIDDEN_KEYS)
                ):
                    raise OracleError(f"A08 {phase} action is not semantic")
                kind = action["action"]
                if kind in {"click", "set_text"} and (
                    not isinstance(action.get("selector"), str)
                    or not action["selector"]
                ):
                    raise OracleError(f"A08 {kind} lacks an exact selector")
                if kind == "set_text" and not isinstance(action.get("value"), str):
                    raise OracleError("A08 set_text lacks a Unicode string value")
                if "scope" in action and (
                    not isinstance(action["scope"], str) or not action["scope"]
                ):
                    raise OracleError("A08 action has an invalid scope")
    return value


def _expand_recipe(group: dict[str, Any], fixtures: dict[str, str]) -> list[dict[str, Any]]:
    recipe = group.get("recipe")
    if recipe == "reject_invalid_utf16":
        values = group.get("values")
        if values != ["d800", "dc00", "0041d8000042", "0041dc000042"]:
            raise OracleError("invalid UTF-16 rejection recipe is not frozen-complete")
        operations: list[dict[str, Any]] = []
        for field, selector in (("name", "火种名称"), ("notes", "备注")):
            for index, encoded in enumerate(values, 1):
                prefix = f"{field}-{index}"
                operations.append(
                    {"id": f"{prefix}-open", "action": "click", "selector": "添加火种"}
                )
                if field == "notes":
                    operations.append(
                        {
                            "id": f"{prefix}-valid-name",
                            "action": "set_text",
                            "selector": "火种名称",
                            "value": "备注代理项",
                        }
                    )
                operations.extend(
                    [
                        {
                            "id": f"{prefix}-inject",
                            "action": "set_text_utf16_hex",
                            "selector": selector,
                            "value_utf16_hex": encoded,
                        },
                        {"id": f"{prefix}-save", "action": "click", "selector": "保存"},
                        {
                            "id": f"{prefix}-form-retained",
                            "action": "assert_visible",
                            "assertion": {"text": "保存"},
                        },
                        {
                            "id": f"{prefix}-ordinary-clear",
                            "action": "set_text",
                            "selector": selector,
                            "value": "",
                        },
                        {"id": f"{prefix}-cancel", "action": "click", "selector": "取消"},
                        {
                            "id": f"{prefix}-empty-set",
                            "action": "assert_record_set",
                            "assertion": {"texts": []},
                        },
                        {
                            "id": f"{prefix}-zero-count",
                            "action": "assert_visible",
                            "assertion": {"text": "已复原 0 / 0"},
                        },
                    ]
                )
        return operations
    if recipe == "create":
        name = fixtures[group["name_ref"]] if "name_ref" in group else group["name"]
        stored = (
            fixtures[group["stored_name_ref"]]
            if "stored_name_ref" in group
            else group.get("stored_name", name)
        )
        category = group["category"]
        priority = group.get("priority", 3)
        operations = [
            {"id": "open", "action": "click", "selector": "添加火种"},
            {
                "id": "name",
                "action": "set_text",
                "selector": "火种名称",
                **_value_argument(group, "name"),
            },
        ]
        if group.get("select_category", True):
            operations.extend(
                [
                    {"id": "category-open", "action": "click", "selector": "类别"},
                    {"id": "category-set", "action": "click", "selector": category},
                ]
            )
        if group.get("select_priority", priority != 3):
            operations.extend(
                [
                    {"id": "priority-open", "action": "click", "selector": "优先级"},
                    {"id": "priority-set", "action": "click", "selector": str(priority)},
                ]
            )
        if "notes" in group or "notes_ref" in group:
            operations.append(
                {
                    "id": "notes",
                    "action": "set_text",
                    "selector": "备注",
                    **_value_argument(group, "notes"),
                }
            )
        if "counter" in group:
            operations.append(
                {
                    "id": "counter",
                    "action": "assert_visible",
                    "assertion": {"text": group["counter"]},
                }
            )
        operations.append({"id": "save", "action": "click", "selector": "保存"})
        if "reject_text" in group or "reject_assertion" in group:
            assertion = group.get(
                "reject_assertion", {"text": group.get("reject_text")}
            )
            operations.append(
                {
                    "id": "rejected",
                    "action": assertion.get("action", "assert_visible"),
                    "assertion": {
                        key: item for key, item in assertion.items() if key != "action"
                    },
                }
            )
            operations.append(
                {"id": "cancel-rejected", "action": "click", "selector": "取消"}
            )
            if "post_cancel_assertion" in group:
                assertion = group["post_cancel_assertion"]
                operations.append(
                    {
                        "id": "unchanged-after-reject",
                        "action": assertion.get("action", "assert_visible"),
                        "assertion": {
                            key: item
                            for key, item in assertion.items()
                            if key != "action"
                        },
                    }
                )
        else:
            operations.append(
                {
                    "id": "created",
                    "action": "assert_visible",
                    "assertion": {
                        "text": _card(stored, category, priority, group.get("status", "沉睡"))
                    },
                }
            )
            if "verify_notes_ref" in group:
                scope = _card(stored, category, priority, group.get("status", "沉睡"))
                operations.extend(
                    [
                        {
                            "id": "verify-notes-open",
                            "action": "click",
                            "selector": "编辑",
                            "scope": scope,
                        },
                        {
                            "id": "verify-notes-value",
                            "action": "assert_visible",
                            "assertion": {"text": fixtures[group["verify_notes_ref"]]},
                        },
                        {
                            "id": "verify-notes-cancel",
                            "action": "click",
                            "selector": "取消",
                        },
                    ]
                )
        return operations
    if recipe == "delete":
        name = fixtures[group["name_ref"]] if "name_ref" in group else group["name"]
        name = (
            fixtures[group["stored_name_ref"]]
            if "stored_name_ref" in group
            else group.get("stored_name", name)
        )
        scope = _card(name, group["category"], group.get("priority", 3), group.get("status", "沉睡"))
        prompt = f"删除“{name}”吗？此操作无法撤销。"
        operations = [
            {"id": "request", "action": "click", "selector": "删除", "scope": scope},
            {
                "id": "prompt",
                "action": "assert_visible",
                "assertion": {"text": prompt},
            },
        ]
        if group.get("cancel_first"):
            operations.extend(
                [
                    {
                        "id": "cancel",
                        "action": "click",
                        "selector": "取消",
                        "scope": prompt,
                    },
                    {
                        "id": "still-present",
                        "action": "assert_visible",
                        "assertion": {"text": scope},
                    },
                    {
                        "id": "request-again",
                        "action": "click",
                        "selector": "删除",
                        "scope": scope,
                    },
                ]
            )
        operations.extend(
            [
                {
                    "id": "confirm",
                    "action": "click",
                    "selector": "删除",
                    "scope": prompt,
                },
                {
                    "id": "absent",
                    "action": "assert_absent",
                    "assertion": {"text": scope},
                },
            ]
        )
        return operations
    if recipe == "state":
        old_scope = _card(group["name"], group["category"], group["priority"], group["from"])
        new_scope = _card(group["name"], group["category"], group["priority"], group["to"])
        return [
            {
                "id": "action",
                "action": "click",
                "selector": group["selector"],
                "scope": old_scope,
            },
            {
                "id": "state",
                "action": "assert_visible",
                "assertion": {"text": new_scope},
            },
            {
                "id": "count",
                "action": "assert_visible",
                "assertion": {"text": group["restored_count"]},
            },
        ]
    if recipe == "edit":
        old_scope = _card(
            group["old_name"],
            group["old_category"],
            group["old_priority"],
            group.get("status", "沉睡"),
        )
        new_name = group.get("new_name", group["old_name"])
        new_category = group.get("new_category", group["old_category"])
        new_priority = group.get("new_priority", group["old_priority"])
        new_scope = _card(
            group.get("stored_name", new_name),
            new_category,
            new_priority,
            group.get("status", "沉睡"),
        )
        operations = [
            {"id": "open", "action": "click", "selector": "编辑", "scope": old_scope}
        ]
        for field, selector in (("new_name", "火种名称"), ("notes", "备注")):
            if field in group or f"{field}_ref" in group:
                operations.append(
                    {
                        "id": field,
                        "action": "set_text",
                        "selector": selector,
                        **_value_argument(group, field),
                    }
                )
        if new_category != group["old_category"]:
            operations.extend(
                [
                    {"id": "category-open", "action": "click", "selector": "类别"},
                    {"id": "category-set", "action": "click", "selector": new_category},
                ]
            )
        if new_priority != group["old_priority"]:
            operations.extend(
                [
                    {"id": "priority-open", "action": "click", "selector": "优先级"},
                    {"id": "priority-set", "action": "click", "selector": str(new_priority)},
                ]
            )
        if "counter" in group:
            operations.append(
                {
                    "id": "counter",
                    "action": "assert_visible",
                    "assertion": {"text": group["counter"]},
                }
            )
        operations.append({"id": "save", "action": "click", "selector": "保存"})
        if group.get("reject"):
            if "reject_text" in group:
                operations.append(
                    {
                        "id": "rejected",
                        "action": "assert_visible",
                        "assertion": {"text": group["reject_text"]},
                    }
                )
            operations.extend(
                [
                    {"id": "cancel", "action": "click", "selector": "取消"},
                    {
                        "id": "unchanged",
                        "action": "assert_visible",
                        "assertion": {"text": old_scope},
                    },
                ]
            )
            if "stored_notes_ref" in group:
                operations.extend(
                    [
                        {
                            "id": "verify-unchanged-open",
                            "action": "click",
                            "selector": "编辑",
                            "scope": old_scope,
                        },
                        {
                            "id": "verify-unchanged-notes",
                            "action": "assert_visible",
                            "assertion": {"text": fixtures[group["stored_notes_ref"]]},
                        },
                        {
                            "id": "verify-unchanged-cancel",
                            "action": "click",
                            "selector": "取消",
                        },
                    ]
                )
        else:
            operations.append(
                {
                    "id": "persisted",
                    "action": "assert_visible",
                    "assertion": {"text": new_scope},
                }
            )
            if "verify_notes_ref" in group:
                operations.extend(
                    [
                        {
                            "id": "verify-notes-open",
                            "action": "click",
                            "selector": "编辑",
                            "scope": new_scope,
                        },
                        {
                            "id": "verify-notes-value",
                            "action": "assert_visible",
                            "assertion": {"text": fixtures[group["verify_notes_ref"]]},
                        },
                        {
                            "id": "verify-notes-cancel",
                            "action": "click",
                            "selector": "取消",
                        },
                    ]
                )
        return operations
    raise OracleError(f"unsupported grouped recipe: {recipe!r}")


def _record_assertions(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ordered = sorted(
        records,
        key=lambda item: (
            -item["priority"],
            tuple(ord(character) for character in unicodedata.normalize("NFC", item["name"])),
            item["creation_sequence"],
        ),
    )
    restored = sum(item["status"] == "已复原" for item in records)
    return [
        {
            "id": "exact-record-set-after-mutation",
            "action": "assert_record_set",
            "assertion": {
                "texts": [
                    _card(
                        item["name"],
                        item["category"],
                        item["priority"],
                        item["status"],
                    )
                    for item in ordered
                ]
            },
        },
        {
            "id": "exact-count-after-mutation",
            "action": "assert_visible",
            "assertion": {"text": f"已复原 {restored} / {len(records)}"},
        },
    ]


def _bind_persistent_mutation(
    group: dict[str, Any],
    operations: list[dict[str, Any]],
    fixtures: dict[str, str],
    records: list[dict[str, Any]],
    next_sequence: int,
) -> int:
    """Insert exact whole-collection/count checks immediately after persistence mutations."""
    recipe = group.get("recipe")
    after_id = None
    if recipe == "create" and not (
        "reject_text" in group or "reject_assertion" in group
    ):
        raw_name = fixtures[group["name_ref"]] if "name_ref" in group else group["name"]
        name = (
            fixtures[group["stored_name_ref"]]
            if "stored_name_ref" in group
            else group.get("stored_name", raw_name)
        )
        records.append(
            {
                "name": name,
                "category": group["category"],
                "priority": group.get("priority", 3),
                "status": group.get("status", "沉睡"),
                "creation_sequence": next_sequence,
            }
        )
        next_sequence += 1
        after_id = "save"
    elif recipe == "delete":
        raw_name = fixtures[group["name_ref"]] if "name_ref" in group else group["name"]
        name = (
            fixtures[group["stored_name_ref"]]
            if "stored_name_ref" in group
            else group.get("stored_name", raw_name)
        )
        matches = [
            item
            for item in records
            if (
                item["name"],
                item["category"],
                item["priority"],
                item["status"],
            )
            == (
                name,
                group["category"],
                group.get("priority", 3),
                group.get("status", "沉睡"),
            )
        ]
        if len(matches) != 1:
            raise OracleError(f"delete recipe does not identify one modeled record: {group['id']}")
        records.remove(matches[0])
        after_id = "confirm"
    elif recipe == "state":
        matches = [
            item
            for item in records
            if (
                item["name"],
                item["category"],
                item["priority"],
                item["status"],
            )
            == (
                group["name"],
                group["category"],
                group["priority"],
                group["from"],
            )
        ]
        if len(matches) != 1:
            raise OracleError(f"state recipe does not identify one modeled record: {group['id']}")
        matches[0]["status"] = group["to"]
        after_id = "action"
    elif recipe == "edit" and not group.get("reject"):
        matches = [
            item
            for item in records
            if (
                item["name"],
                item["category"],
                item["priority"],
                item["status"],
            )
            == (
                group["old_name"],
                group["old_category"],
                group["old_priority"],
                group.get("status", "沉睡"),
            )
        ]
        if len(matches) != 1:
            raise OracleError(f"edit recipe does not identify one modeled record: {group['id']}")
        record = matches[0]
        record["name"] = group.get("stored_name", group.get("new_name", record["name"]))
        record["category"] = group.get("new_category", record["category"])
        record["priority"] = group.get("new_priority", record["priority"])
        after_id = "save"
    if after_id is None:
        return next_sequence
    insert_at = next(
        index + 1 for index, operation in enumerate(operations) if operation["id"] == after_id
    )
    operations[insert_at:insert_at] = _record_assertions(records)
    return next_sequence


def load_flow(path: Path = FLOW_CONFIG) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        value = json.load(source)
    raw_boundaries = value.get("unicode_boundaries", {})
    if not isinstance(raw_boundaries, dict):
        raise OracleError("Unicode fixtures must be an object")
    boundaries = _resolve_unicode_fixtures(raw_boundaries)
    value["unicode_boundaries"] = boundaries
    flattened = []
    modeled_records: list[dict[str, Any]] = []
    next_creation_sequence = 0
    for group in value.get("steps", []):
        operations = group.get("operations")
        if operations is None and "recipe" in group:
            operations = _expand_recipe(group, boundaries)
            next_creation_sequence = _bind_persistent_mutation(
                group,
                operations,
                boundaries,
                modeled_records,
                next_creation_sequence,
            )
        if operations is None:
            flattened.append(group)
            continue
        if not isinstance(operations, list) or not operations:
            raise OracleError("grouped A06 step requires non-empty operations")
        for index, operation in enumerate(operations, 1):
            item = dict(operation)
            item["id"] = f"{group['id']}.{index:02d}-{item['id']}"
            if index == len(operations) and group.get("screenshot"):
                item["screenshot"] = group["screenshot"]
            flattened.append(item)
    for index, step in enumerate(flattened, 1):
        step["number"] = index
    value["steps"] = flattened
    validate_flow(value)
    return value


def _walk_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in FORBIDDEN_KEYS:
                raise OracleError(f"coordinate/ordinal pass input forbidden at {path}.{key}")
            _walk_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_keys(child, f"{path}[{index}]")


def validate_flow(flow: dict[str, Any]) -> None:
    if flow.get("schema_version") != 1:
        raise OracleError("unsupported flow schema")
    expected = {
        "task_id": TASK_ID,
        "oracle_id": ORACLE_ID,
        "brief_id": BRIEF_ID,
        "application_id": APPLICATION_ID,
        "case_id": "A06",
    }
    for key, value in expected.items():
        if flow.get(key) != value:
            raise OracleError(f"frozen flow {key} mismatch")
    _walk_keys(flow)
    steps = flow.get("steps")
    if not isinstance(steps, list) or not steps:
        raise OracleError("flow must contain steps")
    numbers = [step.get("number") for step in steps]
    if numbers != list(range(1, len(steps) + 1)):
        raise OracleError("step numbers must be monotonic and gap-free")
    names = set()
    for step in steps:
        name = step.get("id")
        if not isinstance(name, str) or not name or name in names:
            raise OracleError("step ids must be unique non-empty strings")
        names.add(name)
        action = step.get("action")
        if action not in ALLOWED_ACTIONS:
            raise OracleError(f"unsupported action {action!r}")
        selector = step.get("selector")
        if action in {"click", "set_text", "set_text_utf16_hex", "wait"}:
            if not isinstance(selector, str) or not selector:
                raise OracleError(f"{name}: semantic selector is required")
        if "scope" in step and (
            not isinstance(step["scope"], str) or not step["scope"]
        ):
            raise OracleError(f"{name}: scope must be an exact semantic string")
        if action == "set_text":
            direct = isinstance(step.get("value"), str)
            reference = isinstance(step.get("value_ref"), str)
            if direct == reference:
                raise OracleError(
                    f"{name}: set_text requires exactly one Unicode value or fixture reference"
                )
            if reference and step["value_ref"] not in flow.get("unicode_boundaries", {}):
                raise OracleError(f"{name}: unknown Unicode fixture reference")
        if action == "set_text_utf16_hex":
            value = step.get("value_utf16_hex")
            if (
                not isinstance(value, str)
                or len(value) % 4
                or not value
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise OracleError(f"{name}: invalid lowercase UTF-16 code-unit hex")
        if action.startswith("assert_") and not step.get("assertion"):
            raise OracleError(f"{name}: assertion inputs are required")
        if step.get("visual_assertion") and not step.get("screenshot"):
            raise OracleError(f"{name}: visual assertion requires a screenshot")
        if action in MUTATIONS and step.get("evidence") is False:
            raise OracleError(f"{name}: mutation cannot disable evidence")
    boundary = flow.get("unicode_boundaries", {})
    required_boundaries = {
        "white_space_15",
        "white_space_15_reverse",
        "trim_name",
        "internal_white_space_name",
        "non_trim_members_name",
        "notes_white_space",
        "supplementary_40",
        "supplementary_41",
        "decomposed_40",
        "decomposed_41",
        "composed_40",
        "notes_supplementary_300",
        "notes_supplementary_301",
        "notes_decomposed_300",
        "notes_decomposed_301",
        "notes_composed_300",
    }
    if set(boundary) != required_boundaries:
        raise OracleError("Unicode boundary fixture set is not frozen-complete")
    white_space = boundary["white_space_15"]
    expected_white_space = "".join(
        chr(value)
        for value in (
            *range(0x0009, 0x000E),
            0x0020,
            0x0085,
            0x00A0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
        )
    )
    if white_space != expected_white_space or len(white_space) != 25:
        raise OracleError("White_Space_15 fixture is not the exact frozen sequence")
    if boundary["white_space_15_reverse"] != white_space[::-1]:
        raise OracleError("White_Space_15 reverse fixture mismatch")
    if boundary["trim_name"] != white_space + "已修剪" + white_space[::-1]:
        raise OracleError("trim_name does not cover both complete White_Space_15 edges")
    if boundary["internal_white_space_name"] != "内" + white_space + "部":
        raise OracleError("internal White_Space_15 fixture mismatch")
    if boundary["non_trim_members_name"] != "\u200b不应修剪\ufeff":
        raise OracleError("non-member trim fixture mismatch")
    if boundary["notes_white_space"] != white_space + "保留" + white_space[::-1]:
        raise OracleError("notes White_Space_15 fixture mismatch")
    utf16_boundaries = flow.get("utf16_hex_boundaries")
    if utf16_boundaries != ["d800", "dc00", "0041d8000042", "0041dc000042"]:
        raise OracleError("UTF-16 invalid-boundary fixture set is not frozen-complete")
    injected = [
        step["value_utf16_hex"]
        for step in steps
        if step["action"] == "set_text_utf16_hex"
    ]
    if injected != utf16_boundaries + utf16_boundaries:
        raise OracleError(
            f"name and notes require all four invalid UTF-16 fixtures: {injected!r}"
        )
    if len(boundary["supplementary_40"]) != 40:
        raise OracleError("supplementary_40 must contain 40 code points")
    if len(boundary["supplementary_41"]) != 41:
        raise OracleError("supplementary_41 must contain 41 code points")
    if len(boundary["decomposed_40"]) != 80:
        raise OracleError("decomposed_40 must contain 40 decomposed pairs")
    if len(boundary["decomposed_41"]) != 82:
        raise OracleError("decomposed_41 must contain 41 decomposed pairs")
