"""单元测试:有界涌现的复杂度路由(相位触发器)。

验证 classify_requirement 的确定性单/团队判定,与
docs/current-design/design_bounded_emergence_v1.md 层级 1 一致:
- 简单需求 → 默认单单元(allow_team=False,保守方)
- 显式协作信号 → 团队开放(allow_team=True,涌现方)
- 长多句需求 → 团队开放
- 确定性:同输入必同输出
"""
from __future__ import annotations

from agent_platform.complexity_router import classify_requirement


def test_simple_requirement_stays_single_unit() -> None:
    """短、单一交付物需求 → 单单元,不开放团队(保守方默认)。"""
    result = classify_requirement("搭一个根据材料生成公众号投稿的工作流")
    assert result["allow_team"] is False
    assert result["level"] == "simple"
    assert result["confidence"] >= 0.5


def test_explicit_team_signal_opens_team() -> None:
    """含明确协作信号的需求 → 团队开放(涌现方)。"""
    result = classify_requirement(
        "搭建一个多智能体协作的工作流:写手负责撰写,审核智能体负责评审,并行分工完成投稿"
    )
    assert result["allow_team"] is True
    assert result["level"] == "complex"
    assert "多智能体" in result["signals"]["team"]


def test_long_multi_sentence_requirement_opens_team() -> None:
    """超长多句、跨领域需求(无显式协作词)→ 按长度判为 complex,团队开放。"""
    result = classify_requirement(
        "搭建一个完整的企业数据管道工作流,它需要从多个不同的数据源采集原始数据,"
        "包括数据库、文件系统和外部 API,然后对采集到的数据进行清洗和去重,"
        "处理缺失值和异常值,再进行知识检索和模型推理,把结果整理成结构化记录,"
        "最后生成一份带引用的分析报告,并把报告交付到客户的企业系统中。"
        "这个流程覆盖数据接入、处理、分析、生成和交付五个阶段,"
        "每个阶段都有独立的验收标准,并且需要保证数据的完整性和可追溯性,"
        "同时要支持按计划调度执行,并在出现故障时能够自动重试和恢复运行。"
    )
    assert result["allow_team"] is True
    assert result["level"] == "complex"


def test_medium_requirement_opens_team() -> None:
    """中等长度需求(80-200 字符、无协作词)→ 团队开放(medium)。"""
    result = classify_requirement(
        "搭建一个工作流:读取一份 CSV 数据文件,对数据进行若干变换和校验,"
        "然后生成统计结果并输出到 Excel 工件,整个过程需要保证数据质量,"
        "并且把最终工件保存到指定目录供后续使用。"
    )
    assert result["allow_team"] is True
    assert result["level"] == "medium"


def test_deterministic_same_input_same_output() -> None:
    """确定性:同输入必同输出(相位触发不是随机涌现)。"""
    req = "搭建一个简单的翻译工作流,把中文翻译成英文"
    assert classify_requirement(req) == classify_requirement(req)
