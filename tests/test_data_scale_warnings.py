from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.tabular_models import (
    EvaluateTabularModelRequest,
    FeatureContract,
    LabeledObservation,
    TabularModelService,
    TrainTabularModelRequest,
)


def _rows(labels: list[int]) -> list[LabeledObservation]:
    return [
        LabeledObservation(
            features={"signal": index - len(labels) / 2 if label == 0 else index + 1},
            units={"signal": "u"},
            label=label,
        )
        for index, label in enumerate(labels)
    ]


async def _service(tmp_path: Path) -> TabularModelService:
    service = TabularModelService(tmp_path / "models.db")
    await service.initialize()
    return service


def _train_request(rows: list[LabeledObservation], key: str) -> TrainTabularModelRequest:
    return TrainTabularModelRequest(
        model_name="scale-check",
        features=[FeatureContract(name="signal", unit="u", minimum=-100, maximum=100)],
        rows=rows,
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_train_on_tiny_dataset_warns_about_sample_counts(tmp_path: Path) -> None:
    service = await _service(tmp_path)

    trained = await service.train(
        _train_request(_rows([0, 0, 0, 0, 1, 1, 1, 1]), "train-tiny-001")
    )

    warnings = trained["data_scale_warnings"]
    assert any("训练样本仅 8 个" in item for item in warnings)
    assert any("类别 0 样本仅 4 个" in item for item in warnings)
    assert any("类别 1 样本仅 4 个" in item for item in warnings)


@pytest.mark.asyncio
async def test_train_on_imbalanced_dataset_warns_about_ratio(tmp_path: Path) -> None:
    service = await _service(tmp_path)

    trained = await service.train(
        _train_request(_rows([0] * 12 + [1] * 3), "train-imbalanced-001")
    )

    warnings = trained["data_scale_warnings"]
    assert any("类别失衡" in item and "4.0:1" in item for item in warnings)


@pytest.mark.asyncio
async def test_evaluate_on_tiny_test_set_warns_about_significance(tmp_path: Path) -> None:
    service = await _service(tmp_path)
    trained = await service.train(
        _train_request(_rows([0, 0, 0, 0, 1, 1, 1, 1]), "train-for-eval-001")
    )

    evaluated = await service.evaluate(
        trained["model_id"],
        1,
        EvaluateTabularModelRequest(
            rows=_rows([0, 0, 1, 1]),
            idempotency_key="evaluate-tiny-001",
        ),
    )

    warnings = evaluated["data_scale_warnings"]
    assert any("测试集仅 4 条，指标无统计显著性，不能作为验收依据" == item for item in warnings)
