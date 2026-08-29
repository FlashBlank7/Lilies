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


# ── 运算符逐个钉死 ──
#
# 变异验证（2026-08-29）在这套 33 条用例底下发现四个空档：
#   · `<`  改成 `<=`      全绿
#   · `>`  改成 `>=`      全绿
#   · `==` 和 `!=` 不分   全绿
#   · 取模的除数为零不判   全绿
# 这个模块的存在理由是"业务算术要确定、可复算"（不交给模型算），
# 而它的比较运算符边界、!= 的取反，一条都没被钉住。
# 边界差一位，在补货、阈值告警这类公式里就是一次错误的进货或一次漏报。


@pytest.mark.parametrize("expression,expected", [
    # 小于：真、边界、假
    ("1 < 2", True), ("2 < 2", False), ("3 < 2", False),
    # 小于等于：边界必须为真——这一位是 `<` 和 `<=` 的唯一区别
    ("1 <= 2", True), ("2 <= 2", True), ("3 <= 2", False),
    # 大于
    ("2 > 1", True), ("2 > 2", False), ("1 > 2", False),
    # 大于等于：边界必须为真
    ("2 >= 1", True), ("2 >= 2", True), ("1 >= 2", False),
])
def test_every_comparison_pins_its_boundary(expression, expected) -> None:
    assert evaluate_formula(expression, {}) is expected


@pytest.mark.parametrize("expression,expected", [
    ("1 == 1", True), ("1 == 2", False),
    ("1 != 2", True), ("1 != 1", False),
    # 不等于必须真的取反，不能跟等于同一个结果
    ("'a' == 'a'", True), ("'a' != 'a'", False),
    ("'a' != 'b'", True),
])
def test_equality_and_inequality_are_opposites(expression, expected) -> None:
    assert evaluate_formula(expression, {}) is expected


def test_not_equal_is_not_a_copy_of_equal() -> None:
    """性质断言：任意一对值，== 和 != 必须永远相反。

    上面那些是点，这条是面——把实现改成 `return equal`（不取反）
    时，点可能恰好都对，面一定错。
    """
    for left, right in ((1, 1), (1, 2), (0, 0), (-3, 3), (2.5, 2.5)):
        vars_ = {"a": left, "b": right}
        assert (evaluate_formula("a == b", vars_)
                is not evaluate_formula("a != b", vars_)), (left, right)


def test_modulo_by_zero_says_so_instead_of_crashing() -> None:
    """除法那一支早就判了零，取模这一支同样判了——但没人测过。

    不判的话抛的是 ZeroDivisionError，而不是这里能读懂的 FormulaError；
    在运行时它会变成一句英文栈信息，而不是「取模的除数为零」。
    """
    with pytest.raises(FormulaError) as caught:
        evaluate_formula("7 % 0", {})
    assert "取模" in str(caught.value) and "零" in str(caught.value)


def test_division_by_zero_still_says_so() -> None:
    with pytest.raises(FormulaError) as caught:
        evaluate_formula("7 / 0", {})
    assert "除数为零" in str(caught.value)


def test_modulo_still_computes_when_the_divisor_is_fine() -> None:
    """别为了判零把功能判没了。"""
    assert evaluate_formula("7 % 3", {}) == 1


# ── 输入规模的两道闸 ──
#
# 变异验证（2026-08-29，禁写字节码后重验）：把 MAX_EXPRESSION_CHARS 和
# MAX_TOKENS 各自拉大 100 倍，**这套用例全绿**。只有 MAX_DEPTH 有人盯着。
#
# 公式是**模型生成的**——生成方写出一条几万字符的表达式并不稀奇，
# 而这两道闸是唯一挡在解析器前面的东西。它们坏了不会立刻出事，
# 只是某天一条畸形公式把一次运行拖死，而报错停在解析器深处。


def test_the_caps_are_actually_small_enough_to_protect() -> None:
    """光验"机制在"不够——还得验**数值本身是有保护力的**。

    上面那些用例都从常量现算输入（`MAX_EXPRESSION_CHARS // 2`），
    于是把常量拉大 100 倍它们照样绿：测的是机制，不是那个数。
    变异验证当场逮住了这一点。
    真正要保的是"畸形公式进不了解析器"，那就得对数值本身设个上界。
    """
    from agent_platform.formula import (MAX_DEPTH, MAX_EXPRESSION_CHARS,
                                        MAX_TOKENS)

    assert 0 < MAX_EXPRESSION_CHARS <= 5_000
    assert 0 < MAX_TOKENS <= 2_000
    assert 0 < MAX_DEPTH <= 100


def test_a_very_long_expression_is_refused_before_parsing() -> None:
    from agent_platform.formula import MAX_EXPRESSION_CHARS

    with pytest.raises(FormulaError) as caught:
        evaluate_formula("1 + " * (MAX_EXPRESSION_CHARS // 2) + "1", {})
    assert "过长" in str(caught.value)


def test_an_expression_just_under_the_length_cap_still_works() -> None:
    """别把闸关死：贴着上限的合法公式要能算。"""
    from agent_platform.formula import MAX_EXPRESSION_CHARS, MAX_TOKENS

    # "1 + 1 + … + 1"。两道闸会同时管着这种公式，而且**记号数先到顶**：
    # 每项 4 个字符却是 2 个记号，所以项数要同时满足两边。
    # （第一版只按字符算，结果撞在记号闸上——闸之间会互相挡，
    #   写"贴着上限"的用例时要把两边都算进去。）
    terms = min((MAX_EXPRESSION_CHARS - 10) // 4, (MAX_TOKENS - 10) // 2)
    assert evaluate_formula(" + ".join(["1"] * terms), {}) == terms


def test_too_many_tokens_is_refused() -> None:
    """字符数没超、记号数超了——两道闸挡的是不同的东西。

    「1+1+1+…」每项只占 2 个字符却是 2 个记号，
    只看长度的话这种公式能穿过去。
    """
    from agent_platform.formula import MAX_TOKENS

    with pytest.raises(FormulaError) as caught:
        evaluate_formula("+".join(["1"] * (MAX_TOKENS // 2 + 20)), {})
    assert "复杂" in str(caught.value) or "过长" in str(caught.value)


def test_an_ordinary_formula_is_nowhere_near_the_caps() -> None:
    """确认这两道闸不会误伤真实用法——真机上的公式都很短。"""
    assert evaluate_formula("max(ceil(avg(sales[-4:]) * 2 - stock), moq)",
                            {"sales": [30, 35, 40, 38], "stock": 60, "moq": 100}) == 100
