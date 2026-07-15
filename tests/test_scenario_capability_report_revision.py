from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree

from docx import Document

from scripts.revise_scenario_capability_report import ANNOTATION_COVERAGE, validate_annotation_inventory


ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs/lilies_agent_scenario_capability_boundary_v0_4_x_latest.docx"
SOURCE = ROOT / "docs/lilies_agent_scenario_capability_boundary_v0_4_x_revised.docx"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def document_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    return "\n".join(node.text or "" for node in root.iter(f"{{{W_NS}}}t"))


def test_latest_report_integrates_inline_annotations() -> None:
    text = document_text(REPORT)

    assert "批注：" not in text
    assert "Quick、Guided、Governed" in text
    assert "发布权属于用户" in text
    assert "一键把失败原因" in text
    assert "面向人的积木配置界面" in text
    assert "模板市场与工作流模块" in text
    assert "先判断承载层，再决定是否新增积木" in text
    assert "Lilies 的原始设定是" in text
    assert "自动演进的长任务控制架构" in text
    assert "对批注问题问答的回答：" not in text
    for item in ANNOTATION_COVERAGE:
        for marker in item["output_markers"]:
            assert marker in text, f"{item['id']} missing output marker: {marker}"


def test_source_annotation_inventory_is_complete_and_strict() -> None:
    annotations = validate_annotation_inventory(Document(SOURCE))

    assert len(annotations) == 7
    assert len(ANNOTATION_COVERAGE) == 7


def test_latest_report_preserves_structural_docx_parts() -> None:
    with zipfile.ZipFile(REPORT) as archive:
        names = set(archive.namelist())
        document = ElementTree.fromstring(archive.read("word/document.xml"))

    assert "word/styles.xml" in names
    assert "word/numbering.xml" in names
    assert "word/comments.xml" not in names
    assert len(list(document.iter(f"{{{W_NS}}}tbl"))) >= 22
