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


def test_record_aggregation_pluck_and_sum_by() -> None:
    """记录聚合（2026-08-23 能力补齐）：此前对象数组无法确定性分组求和——
    日报类需求在积木层无解，模型只能硬编码键名（假成功事故）或选错积木（0/8）。
    动态键名必须开箱即用。"""

    from agent_platform.formula import evaluate_formula

    sales = [
        {"store": "A店", "amount": 1200},
        {"store": "A店", "amount": 800},
        {"store": "B店", "amount": 3000},
    ]
    assert evaluate_formula('sum_by(sales, "store", "amount")', {"sales": sales}) == {
        "A店": 2000, "B店": 3000,
    }
    assert evaluate_formula('sum(pluck(sales, "amount"))', {"sales": sales}) == 5000
    # 未见过的键名同样工作（假成功事故的照妖镜输入）
    unseen = [{"store": "C店", "amount": 500}, {"store": "D店", "amount": 700}]
    assert evaluate_formula('sum_by(s, "store", "amount")', {"s": unseen}) == {
        "C店": 500, "D店": 700,
    }


def test_record_aggregation_errors_are_teachable() -> None:
    import pytest
    from agent_platform.formula import evaluate_formula, FormulaError

    with pytest.raises(FormulaError, match="缺少字段"):
        evaluate_formula('sum_by(s, "store", "amount")', {"s": [{"store": "A"}]})
    with pytest.raises(FormulaError, match="字符串字段名"):
        evaluate_formula('pluck(s, 1)', {"s": []})
    with pytest.raises(FormulaError, match="未闭合"):
        evaluate_formula('pluck(s, "amount)', {"s": []})


def test_string_ops_trim_split_count_len() -> None:
    """字符串确定性统计：真机 E2E 缺口（行数/净字数）在公式层一行可解。"""
    text = "  你好世界  \n第二行\n第三行  "
    assert evaluate_formula("len(split(text))", {"text": text}) == 3
    assert evaluate_formula("len(trim(text))", {"text": text}) == len(text.strip())
    assert evaluate_formula('split(csv, ",")', {"csv": "a,b,c"}) == ["a", "b", "c"]
    assert evaluate_formula('count(csv, ",")', {"csv": "a,b,c"}) == 2
    assert evaluate_formula('trim(word)', {"word": "  x  "}) == "x"
    # 组合：净行（先 trim 再按行切）
    assert evaluate_formula("len(split(trim(text)))", {"text": "\n a \n b \n"}) == 2


def test_string_ops_errors_are_teachable() -> None:
    for expression, fragment in [
        ("split(5)", "split(文本)"),
        ('split(text, "")', "非空字符串"),
        ('count(text, "")', "非空子串"),
        ("trim(7)", "trim(文本)"),
        ("len(3)", "列表或字符串"),
    ]:
        try:
            evaluate_formula(expression, {"text": "abc"})
        except FormulaError as error:
            assert fragment in str(error), (expression, str(error))
        else:
            raise AssertionError(f"{expression} 应当报错")
