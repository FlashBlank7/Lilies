"""出生配置：62 种积木拖到画布上必须全部合法出生——人类不该见到 Pydantic 原文。"""

from __future__ import annotations

from agent_platform.blocks import build_block_registry
from agent_platform.workflow_models import NodeSpec


def test_every_block_default_config_validates() -> None:
    registry = build_block_registry()
    failures: list[str] = []
    for definition in registry.list():
        node = NodeSpec(
            id=f"{definition.type}-default",
            type=definition.type,
            block_version=1,
            title=definition.title,
            description="",
            config=definition.default_config,
            position={"x": 0, "y": 0},
        )
        try:
            registry.validate_node(node)
        except Exception as error:  # noqa: BLE001 - 报告全部失败者
            failures.append(f"{definition.type}: {str(error)[:160]}")
    assert not failures, "默认配置不合法的积木：\n" + "\n".join(failures)


def test_default_config_is_served_in_catalog_payload() -> None:
    registry = build_block_registry()
    payload = registry.get("knowledge_index_sync").model_dump(mode="json")
    assert "default_config" in payload
    assert payload["default_config"].get("index_name")


def test_validation_errors_speak_chinese_with_field_labels() -> None:
    registry = build_block_registry()
    node = NodeSpec(
        id="a-1", type="answer", block_version=1, title="回答", description="",
        config={}, position={"x": 0, "y": 0},
    )
    try:
        registry.validate_node(node)
        raise AssertionError("empty answer config should fail")
    except ValueError as error:
        message = str(error)
        assert "积木「" in message and "还差这些没填" in message
        assert "Field required" not in message  # Pydantic 原文不许出门


def test_container_nesting_capped_at_two() -> None:
    registry = build_block_registry()
    iteration_default = registry.get("iteration").default_config

    def wrap(inner_config):
        import copy
        outer = copy.deepcopy(iteration_default)
        outer["workflow"]["nodes"].insert(1, {
            "id": "inner-loop", "type": "iteration", "title": "内层",
            "config": copy.deepcopy(inner_config), "position": {"x": 160, "y": 80},
        })
        return outer

    two_layers = wrap(iteration_default)          # 外 1 + 内 2 = 合法
    node = NodeSpec(id="it-1", type="iteration", block_version=1, title="循环",
                    description="", config=two_layers, position={"x": 0, "y": 0})
    registry.validate_node(node)

    three_layers = wrap(wrap(iteration_default))  # 3 层 → 拒绝
    node3 = NodeSpec(id="it-2", type="iteration", block_version=1, title="循环",
                     description="", config=three_layers, position={"x": 0, "y": 0})
    try:
        registry.validate_node(node3)
        raise AssertionError("3-layer nesting should be rejected")
    except ValueError as error:
        assert "嵌套超过" in str(error)


def test_module_reference_cycle_and_depth_helpers() -> None:
    from agent_platform.workflow_storage import WorkflowStorage

    class Snap:
        def __init__(self, blob: str) -> None:
            self._blob = blob
        def model_dump_json(self) -> str:
            return self._blob

    ids = WorkflowStorage.referenced_workflow_ids(
        Snap('{"tool_name": "workflow:aaaa-bbbb-cccc", "x": "workflow:dddd-eeee"}')
    )
    assert ids == {"aaaa-bbbb-cccc", "dddd-eeee"}
    assert WorkflowStorage.referenced_workflow_ids(Snap('{"tool_name": "Read"}')) == set()
