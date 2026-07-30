from pathlib import Path

from agent_platform.blocks import build_block_registry


ROOT = Path(__file__).resolve().parents[1]
STUDIO = ROOT / "platform/frontend/app/applications/[id]/page.tsx"
CATALOG = ROOT / "platform/frontend/app/applications/[id]/block-catalog-panel.tsx"
CATALOG_STYLES = (
    ROOT / "platform/frontend/app/applications/[id]/block-catalog-panel.module.css"
)


def test_block_catalog_inspects_before_it_mutates_the_draft() -> None:
    studio = STUDIO.read_text(encoding="utf-8")
    catalog = CATALOG.read_text(encoding="utf-8")

    assert 'data-block-catalog="inspect-before-add"' in catalog
    assert "setSelectedType(current => current === block.type ? null : block.type)" in catalog
    assert 'data-block-catalog-details={block.type}' in catalog
    assert "onClick={() => void addSelected(block)}" in catalog
    assert "Add and open configuration" in catalog
    assert "<BlockCatalogPanel" in studio
    assert "blocks={blocks}" in studio
    assert "onAdd={addBlock}" in studio
    assert 'onClick={() => addBlock(block)}' not in studio


def test_block_help_explains_purpose_ports_usage_and_boundaries() -> None:
    studio = STUDIO.read_text(encoding="utf-8")
    catalog = CATALOG.read_text(encoding="utf-8")
    styles = CATALOG_STYLES.read_text(encoding="utf-8")

    for evidence in (
        "localizedDescription(block, locale)",
        "portSummary(block.input_ports)",
        "portSummary(block.output_ports)",
        "block.when_to_use",
        "block.composability_constraints",
        "block.anti_patterns",
        "exampleConnection",
    ):
        assert evidence in catalog
    assert "JSON.stringify(example" not in catalog
    assert "<BlockPurpose block={selectedBlockDefinition} locale={locale} />" in studio
    assert "<BlockInstanceDetails locale={locale} node={selected} />" in studio
    assert "compositionLabel: nestedNodeCount" in studio
    assert "内层 ${nestedNodeCount} 个节点" in studio
    assert "operationLabel" in studio
    assert 'data-block-instance-details="connector_action"' in catalog
    assert "A Connector Action executes one registered operation." in catalog
    assert "nestedNodes.length" in catalog
    assert "Nested workflow contents" in catalog
    assert ".purpose" in styles
    assert ".instance" in styles


def test_added_block_is_selected_and_opens_its_configuration() -> None:
    studio = STUDIO.read_text(encoding="utf-8")

    start = studio.index(
        "async function addBlock(block: Block, requestedPosition?: CanvasPoint)"
    )
    end = studio.index("async function insertCapabilityModule", start)
    add_block = studio[start:end]
    assert "const nodeId =" in add_block
    assert "const added = next?.snapshot.workflow.nodes.find" in add_block
    assert "setSelectedNode(added)" in add_block
    assert "selected: node.id === nodeId" in add_block
    assert "setStudioTab('edit')" in add_block
    assert "请检查用途和配置" in add_block


def test_undefined_business_seed_is_visibly_not_a_completed_workflow() -> None:
    studio = STUDIO.read_text(encoding="utf-8")
    catalog = CATALOG.read_text(encoding="utf-8")
    runner = (
        ROOT / "scripts/run_v04_13_enterprise_experiment.py"
    ).read_text(encoding="utf-8")

    assert "const businessDefinitionMissing" in studio
    assert "<UndefinedBusinessWorkflowNotice" in studio
    assert 'data-business-workflow-definition="missing"' in catalog
    assert "不能据此判断项目已经完成" in catalog
    assert 'f"{TASK_ID} · {seed} · workflow pending"' in runner
    assert "Formal task and environment seed only." in runner


def test_connector_action_has_a_human_readable_chinese_identity() -> None:
    definition = build_block_registry().get("connector_action").model_dump(mode="json")

    assert definition["editor"]["i18n"]["zh"] == {
        "title": "连接器操作",
        "description": "通过租户、权限和请求策略执行一个已登记的外部系统接口操作。",
        "category": "集成",
    }


def test_studio_chrome_is_collapsible_without_draft_mutations() -> None:
    studio = STUDIO.read_text(encoding="utf-8")
    catalog = CATALOG.read_text(encoding="utf-8")

    for control in (
        'data-studio-chrome-toggle="header"',
        'data-studio-chrome-toggle="left-panel"',
        'data-studio-chrome-toggle="catalog"',
        'data-studio-chrome-toggle="guidance"',
        'data-studio-chrome-toggle="toolbar"',
        'data-studio-chrome-toggle="undefined-business"',
    ):
        assert control in studio or control in catalog
    assert "STUDIO_CHROME_STORAGE_KEY" in studio
    assert "parseStudioChromePreferences" in studio
    assert "window.localStorage.setItem" in studio
    toggle_start = studio.index("function toggleStudioChrome(")
    toggle_end = studio.index("useEffect(() =>", toggle_start)
    assert "mutation(" not in studio[toggle_start:toggle_end]
    assert "expected_revision" not in studio[toggle_start:toggle_end]


def test_canvas_uses_real_named_ports_and_serializes_draft_mutations() -> None:
    studio = STUDIO.read_text(encoding="utf-8")

    assert "<Handle id={port.name}" in studio
    assert "inputPorts.map" in studio
    assert "outputPorts.map" in studio
    assert "sourceHandle: item.source_port" in studio
    assert "targetHandle: item.target_port" in studio
    assert "source_port: contract.sourcePort.name" in studio
    assert "target_port: contract.targetPort.name" in studio
    assert "source_port: 'output', target_port: 'input'" not in studio[
        studio.index("const onConnect") : studio.index("async function addBlock")
    ]
    assert "const mutationQueueRef = useRef<Promise<void>>(Promise.resolve())" in studio
    mutation_start = studio.index("function mutation(")
    mutation_end = studio.index("async function updateDeliverySettings", mutation_start)
    mutation = studio[mutation_start:mutation_end]
    assert "mutationQueueRef.current.then" in mutation
    assert "const current = draftRef.current" in mutation
    assert mutation.index("mutationQueueRef.current.then") < mutation.index(
        "const current = draftRef.current"
    )


def test_catalog_drag_uses_the_same_add_path_at_canvas_coordinates() -> None:
    studio = STUDIO.read_text(encoding="utf-8")
    catalog = CATALOG.read_text(encoding="utf-8")

    assert "application/x-lilies-block-type" in studio
    assert "application/x-lilies-block-type" in catalog
    assert "draggable" in catalog
    assert "onDragStart={event => startBlockDrag(event, block)}" in catalog
    assert 'data-canvas-drop-target="block-catalog"' in studio
    assert "screenToFlowPosition" in studio
    assert "void addBlock(block, position" in studio
    assert "async function addBlock(block: Block, requestedPosition?: CanvasPoint)" in studio
