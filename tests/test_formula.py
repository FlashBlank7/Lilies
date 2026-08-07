"""中缀公式引擎：T6 类补货算术一条公式写完，且确定性、安全、可读错误。"""

from __future__ import annotations

import pytest

from agent_platform.formula import FormulaError, evaluate_formula


SALES = [30, 35, 40, 38, 42, 45, 44, 42]  # 近 8 周


def test_t6_replenishment_shapes_in_one_formula() -> None:
    vars_ = {"sales": SALES, "stock": 60, "lead": 2, "moq": 100}
    forecast = evaluate_formula("avg(sales[-4:])", vars_)
    assert forecast == pytest.approx(43.25)

    need = evaluate_formula("stock < avg(sales[-4:]) * lead", vars_)
    assert need is True

    quantity = evaluate_formula(
        "when(stock < avg(sales[-4:]) * lead,"
        " max(ceil(avg(sales[-4:]) * (lead + 2) - stock), moq), 0)",
        vars_,
    )
    assert quantity == 113  # 43.25*4 - 60 = 113，恰为 T6 正确答案

    ok_stock = evaluate_formula(
        "when(stock < avg(sales[-4:]) * lead, max(ceil(avg(sales[-4:]) * (lead + 2) - stock), moq), 0)",
        {**vars_, "stock": 500},
    )
    assert ok_stock == 0


def test_determinism_and_operators() -> None:
    vars_ = {"xs": [1, 2, 3, 4], "a": 7}
    assert evaluate_formula("sum(xs) + a * 2 - 3 / 2", vars_) == pytest.approx(22.5)
    assert evaluate_formula("xs[0] + xs[-1]", vars_) == 5
    assert evaluate_formula("len(xs[1:3])", vars_) == 2
    assert evaluate_formula("a % 4", vars_) == 3
    assert evaluate_formula("-a + 10", vars_) == 3
    assert evaluate_formula("a >= 7 and not (a == 8)", vars_) is True
    assert evaluate_formula("min(xs) == 1 or false", vars_) is True
    # 同式同值：跑一百次一个样
    results = {evaluate_formula("avg(xs) * a", vars_) for _ in range(100)}
    assert len(results) == 1


@pytest.mark.parametrize(
    "expression, fragment",
    [
        ("stock +", "意外结束"),
        ("import os", "多余内容"),
        ("__class__", "未绑定"),
        ("a / 0", "除数为零"),
        ("evil(1)", "不支持的函数"),
        ("a @ 2", "不支持的字符"),
        ("xs[9]", "超出范围"),
        ("when(1, 2, 3)", "布尔"),
        ("a + xs", "需要数字"),
        ("(" * 30 + "1" + ")" * 30, "嵌套过深"),
    ],
)
def test_readable_errors_and_no_escape_hatch(expression: str, fragment: str) -> None:
    with pytest.raises(FormulaError) as error:
        evaluate_formula(expression, {"a": 1, "stock": 5, "xs": [1, 2]})
    assert fragment in str(error.value)


def test_runtime_assignment_operator_formula() -> None:
    from agent_platform.workflow_runtime import WorkflowRuntime

    context = {
        "inputs": {"skus": [{"weekly_sales": SALES, "stock": 60, "lead_time_weeks": 2, "moq": 100}]},
        "nodes": {},
    }
    value = {
        "$formula": {
            "expression": "max(ceil(avg(sales[-4:]) * (lead + 2) - stock), moq)",
            "vars": {
                "sales": {"$ref": {"node_id": "$inputs", "path": ["skus", "0", "weekly_sales"]}},
                "stock": {"$ref": {"node_id": "$inputs", "path": ["skus", "0", "stock"]}},
                "lead": {"$ref": {"node_id": "$inputs", "path": ["skus", "0", "lead_time_weeks"]}},
                "moq": {"$ref": {"node_id": "$inputs", "path": ["skus", "0", "moq"]}},
            },
        }
    }
    assert WorkflowRuntime._resolve_assignment(value, context) == 113
