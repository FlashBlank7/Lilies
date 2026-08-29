"""中缀公式引擎——确定性计算的"人能写"入口。

T6 三次失守的根因不是纪律，是表达式树（$subtract 嵌套）比一段提示词难写十倍。
这里给 variable_assigner 补上公式模式：

    avg(weekly_sales[-4:]) * (lead_time + 2) - stock
    when(stock < avg(weekly_sales[-4:]) * lead_time, max(ceil(gap), moq), 0)

- 纯手写递归下降解析，无 eval，无属性访问，无调用面。
- 支持：+ - * / %、比较（< <= > >= == !=）、and/or/not、括号、一元负号；
  标识符来自显式绑定的 vars；列表索引与切片 xs[-4:]；
  函数 avg/sum/min/max/len/abs/round/floor/ceil/when。
- 结果与中间值必须有限；除零、未绑定变量、类型不符都给出可读错误。
"""

from __future__ import annotations

import json

import math
from typing import Any

MAX_EXPRESSION_CHARS = 500
MAX_TOKENS = 200
MAX_DEPTH = 24

_FUNCTIONS = {
    "avg", "sum", "min", "max", "len", "abs", "round", "floor", "ceil", "when",
    # 记录聚合（2026-08-23）：此前对象数组无法确定性地提取字段/分组求和——
    # 日报类需求在积木层无解，模型只能硬编码键名作弊或选错积木。
    "pluck", "sum_by",
    # 字符串确定性处理（2026-08-27）：真机首次"对话生成"E2E 就栽在这——
    # 行数/去空白字数在积木层无解，莉莉丝探索 24 分钟后被迫用 llm 数数。
    # split(文本) 单参数按行切（词法器不做转义，写不出 "\n" 字面量）。
    "trim", "split", "count",
}
_KEYWORDS = {"and", "or", "not", "true", "false"}


def function_names() -> frozenset[str]:
    """公式引擎支持的函数名（校验器与提示词共用同一份真相）。"""
    return frozenset(_FUNCTIONS)


class FormulaError(ValueError):
    """公式解析或求值失败（消息面向排错，含位置提示）。"""


# ---------------- 词法 ----------------


def _tokenize(text: str) -> list[tuple[str, Any]]:
    if len(text) > MAX_EXPRESSION_CHARS:
        raise FormulaError(f"公式过长（>{MAX_EXPRESSION_CHARS} 字符）")
    tokens: list[tuple[str, Any]] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isdigit() or (ch == "." and i + 1 < len(text) and text[i + 1].isdigit()):
            j = i
            seen_dot = False
            while j < len(text) and (text[j].isdigit() or (text[j] == "." and not seen_dot)):
                seen_dot = seen_dot or text[j] == "."
                j += 1
            literal = text[i:j]
            tokens.append(("num", float(literal) if "." in literal else int(literal)))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            lowered = word.lower()
            if lowered in _KEYWORDS:
                tokens.append(("kw", lowered))
            else:
                tokens.append(("name", word))
            i = j
            continue
        if ch in ("\"", "'"):
            quote = ch
            j = i + 1
            while j < len(text) and text[j] != quote:
                j += 1
            if j >= len(text):
                raise FormulaError(f"字符串未闭合（位置 {i}）")
            tokens.append(("str", text[i + 1:j]))
            i = j + 1
            continue
        two = text[i:i + 2]
        if two in ("<=", ">=", "==", "!="):
            tokens.append(("op", two))
            i += 2
            continue
        if ch in "+-*/%()[]:,<>":
            tokens.append(("op", ch))
            i += 1
            continue
        raise FormulaError(f"公式包含不支持的字符 {ch!r}（位置 {i}）")
    if len(tokens) > MAX_TOKENS:
        raise FormulaError(f"公式过于复杂（>{MAX_TOKENS} 个记号）")
    return tokens


# ---------------- 语法（递归下降）----------------


class _Parser:
    def __init__(self, tokens: list[tuple[str, Any]]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.depth = 0

    def peek(self) -> tuple[str, Any] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, kind: str | None = None, value: Any = None) -> tuple[str, Any]:
        token = self.peek()
        if token is None:
            raise FormulaError("公式意外结束")
        if kind is not None and token[0] != kind:
            raise FormulaError(f"位置 {self.pos}：期待 {kind}，实际 {token}")
        if value is not None and token[1] != value:
            raise FormulaError(f"位置 {self.pos}：期待 {value!r}，实际 {token[1]!r}")
        self.pos += 1
        return token

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise FormulaError("公式嵌套过深")

    def _exit(self) -> None:
        self.depth -= 1

    def parse(self) -> Any:
        node = self.parse_or()
        if self.peek() is not None:
            raise FormulaError(f"位置 {self.pos}：公式在 {self.peek()!r} 处有多余内容")
        return node

    def parse_or(self) -> Any:
        self._enter()
        node = self.parse_and()
        while (token := self.peek()) and token == ("kw", "or"):
            self.take()
            node = ("or", node, self.parse_and())
        self._exit()
        return node

    def parse_and(self) -> Any:
        node = self.parse_not()
        while (token := self.peek()) and token == ("kw", "and"):
            self.take()
            node = ("and", node, self.parse_not())
        return node

    def parse_not(self) -> Any:
        if (token := self.peek()) and token == ("kw", "not"):
            self.take()
            return ("not", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self) -> Any:
        node = self.parse_additive()
        token = self.peek()
        if token and token[0] == "op" and token[1] in ("<", "<=", ">", ">=", "==", "!="):
            operator = self.take()[1]
            node = ("cmp", operator, node, self.parse_additive())
        return node

    def parse_additive(self) -> Any:
        node = self.parse_multiplicative()
        while (token := self.peek()) and token[0] == "op" and token[1] in ("+", "-"):
            operator = self.take()[1]
            node = ("bin", operator, node, self.parse_multiplicative())
        return node

    def parse_multiplicative(self) -> Any:
        node = self.parse_unary()
        while (token := self.peek()) and token[0] == "op" and token[1] in ("*", "/", "%"):
            operator = self.take()[1]
            node = ("bin", operator, node, self.parse_unary())
        return node

    def parse_unary(self) -> Any:
        if (token := self.peek()) and token == ("op", "-"):
            self.take()
            return ("neg", self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self) -> Any:
        node = self.parse_primary()
        while (token := self.peek()) and token == ("op", "["):
            self.take()
            node = self._parse_index_or_slice(node)
        return node

    def _parse_index_or_slice(self, target: Any) -> Any:
        def maybe_int() -> int | None:
            token = self.peek()
            if token and token == ("op", "-"):
                self.take()
                value = self.take("num")[1]
                if not isinstance(value, int):
                    raise FormulaError("索引必须是整数")
                return -value
            if token and token[0] == "num":
                value = self.take()[1]
                if not isinstance(value, int):
                    raise FormulaError("索引必须是整数")
                return value
            return None

        start = maybe_int()
        token = self.peek()
        if token and token == ("op", ":"):
            self.take()
            stop = maybe_int()
            self.take("op", "]")
            return ("slice", target, start, stop)
        self.take("op", "]")
        if start is None:
            raise FormulaError("索引缺少数值")
        return ("index", target, start)

    def parse_primary(self) -> Any:
        token = self.peek()
        if token is None:
            raise FormulaError("公式意外结束")
        if token[0] == "str":
            self.take()
            return ("str", token[1])
        if token[0] == "str":
            self.take()
            return ("str", token[1])
        if token[0] == "num":
            self.take()
            return ("num", token[1])
        if token[0] == "kw" and token[1] in ("true", "false"):
            self.take()
            return ("bool", token[1] == "true")
        if token[0] == "name":
            name = self.take()[1]
            if (nxt := self.peek()) and nxt == ("op", "("):
                lowered = name.lower()
                if lowered not in _FUNCTIONS:
                    raise FormulaError(f"不支持的函数 {name}（可用：{'、'.join(sorted(_FUNCTIONS))}）")
                self.take()
                args: list[Any] = []
                if self.peek() != ("op", ")"):
                    args.append(self.parse_or())
                    while self.peek() == ("op", ","):
                        self.take()
                        args.append(self.parse_or())
                self.take("op", ")")
                return ("call", lowered, args)
            return ("var", name)
        if token == ("op", "("):
            self.take()
            node = self.parse_or()
            self.take("op", ")")
            return node
        raise FormulaError(f"位置 {self.pos}：无法理解 {token[1]!r}")


# ---------------- 求值 ----------------


def _as_number(value: Any, label: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FormulaError(f"{label} 需要数字，得到 {type(value).__name__}")
    if isinstance(value, float) and not math.isfinite(value):
        raise FormulaError(f"{label} 不是有限数")
    return value

def _as_number_list(value: Any, label: str) -> list[float | int]:
    if not isinstance(value, list):
        raise FormulaError(f"{label} 需要数字列表，得到 {type(value).__name__}")
    return [_as_number(item, f"{label} 的元素") for item in value]


def _evaluate(node: Any, vars_: dict[str, Any]) -> Any:
    kind = node[0]
    if kind == "num" or kind == "bool" or kind == "str":
        return node[1]
    if kind == "var":
        name = node[1]
        if name not in vars_:
            raise FormulaError(f"变量 {name} 未绑定（vars 里没有它）")
        return vars_[name]
    if kind == "neg":
        return -_as_number(_evaluate(node[1], vars_), "负号")
    if kind == "not":
        return not _truthy(_evaluate(node[1], vars_))
    if kind == "and":
        return _truthy(_evaluate(node[1], vars_)) and _truthy(_evaluate(node[2], vars_))
    if kind == "or":
        return _truthy(_evaluate(node[1], vars_)) or _truthy(_evaluate(node[2], vars_))
    if kind == "cmp":
        _, operator, left_node, right_node = node
        left = _evaluate(left_node, vars_)
        right = _evaluate(right_node, vars_)
        if operator in ("==", "!="):
            equal = left == right
            return equal if operator == "==" else not equal
        left_num = _as_number(left, "比较左侧")
        right_num = _as_number(right, "比较右侧")
        return {
            "<": left_num < right_num,
            "<=": left_num <= right_num,
            ">": left_num > right_num,
            ">=": left_num >= right_num,
        }[operator]
    if kind == "bin":
        _, operator, left_node, right_node = node
        left = _as_number(_evaluate(left_node, vars_), f"{operator} 左侧")
        right = _as_number(_evaluate(right_node, vars_), f"{operator} 右侧")
        if operator == "+":
            result: float | int = left + right
        elif operator == "-":
            result = left - right
        elif operator == "*":
            result = left * right
        elif operator == "/":
            if right == 0:
                raise FormulaError("除数为零")
            result = left / right
        else:
            if right == 0:
                raise FormulaError("取模的除数为零")
            result = left % right
        _as_number(result, "运算结果")
        return result
    if kind == "index":
        _, target_node, index = node
        target = _as_number_list(_evaluate(target_node, vars_), "索引对象")
        try:
            return target[index]
        except IndexError as error:
            raise FormulaError(f"索引 {index} 超出范围（长度 {len(target)}）") from error
    if kind == "slice":
        _, target_node, start, stop = node
        target = _as_number_list(_evaluate(target_node, vars_), "切片对象")
        return target[start:stop]
    if kind == "call":
        _, name, arg_nodes = node
        args = [_evaluate(arg, vars_) for arg in arg_nodes]
        return _call(name, args)
    raise FormulaError(f"未知节点 {kind}")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise FormulaError("and/or/not 只作用于比较结果（布尔值）")


def _numbers_from(args: list[Any], name: str) -> list[float | int]:
    if len(args) == 1 and isinstance(args[0], list):
        values = _as_number_list(args[0], f"{name} 的参数")
    else:
        values = [_as_number(item, f"{name} 的参数") for item in args]
    if not values:
        raise FormulaError(f"{name} 需要至少一个数")
    return values


def _record_array(value: Any, usage: str) -> list:
    """记录数组参数的类型闸：带引号的 JSON 字面量要点破真因，别让人修没坏的工作流。"""

    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                json.loads(stripped)
            except ValueError:
                pass
            else:
                raise FormulaError(
                    f"{usage} 收到的是 JSON 字符串而不是数组本体——"
                    "值被整体加了引号。请去掉外层引号，直接传数组。"
                )
    raise FormulaError(f"{usage} 需要一个对象数组")


def _call(name: str, args: list[Any]) -> Any:
    if name == "pluck":
        if len(args) != 2 or not isinstance(args[1], str):
            raise FormulaError('pluck(记录数组, "字段名") 需要一个对象数组和一个字符串字段名')
        args = [_record_array(args[0], "pluck"), args[1]]
        field = args[1]
        out = []
        for index, item in enumerate(args[0]):
            if not isinstance(item, dict):
                raise FormulaError(f"pluck 的第 {index + 1} 个元素不是对象")
            if field not in item:
                raise FormulaError(f"pluck 的第 {index + 1} 个元素缺少字段 {field!r}")
            out.append(item[field])
        return out
    if name == "sum_by":
        if (len(args) != 3
                or not isinstance(args[1], str) or not isinstance(args[2], str)):
            raise FormulaError('sum_by(记录数组, "分组字段", "数值字段") 需要一个对象数组和两个字符串字段名')
        args = [_record_array(args[0], "sum_by"), args[1], args[2]]
        key_field, value_field = args[1], args[2]
        totals: dict[str, float | int] = {}
        for index, item in enumerate(args[0]):
            if not isinstance(item, dict):
                raise FormulaError(f"sum_by 的第 {index + 1} 个元素不是对象")
            if key_field not in item or value_field not in item:
                raise FormulaError(
                    f"sum_by 的第 {index + 1} 个元素缺少字段（需要 {key_field!r} 与 {value_field!r}）"
                )
            key = str(item[key_field])
            value = _as_number(item[value_field], f"sum_by 的 {value_field}")
            totals[key] = totals.get(key, 0) + value
        return totals
    if name == "when":
        if len(args) != 3:
            raise FormulaError("when(条件, 成立值, 不成立值) 需要三个参数")
        return args[1] if _truthy(args[0]) else args[2]
    if name == "len":
        if len(args) != 1 or not isinstance(args[0], (list, str)):
            raise FormulaError("len 需要一个列表或字符串参数")
        return len(args[0])
    if name == "trim":
        if len(args) != 1 or not isinstance(args[0], str):
            raise FormulaError("trim(文本) 需要一个字符串参数")
        return args[0].strip()
    if name == "split":
        if not args or not isinstance(args[0], str):
            raise FormulaError('split(文本) 按行切分；split(文本, "分隔符") 按分隔符切分')
        if len(args) == 1:
            return args[0].splitlines()
        if len(args) != 2 or not isinstance(args[1], str) or not args[1]:
            raise FormulaError('split 的分隔符需要一个非空字符串（按行切分请用单参数 split(文本)）')
        return args[0].split(args[1])
    if name == "count":
        if (len(args) != 2 or not isinstance(args[0], str)
                or not isinstance(args[1], str) or not args[1]):
            raise FormulaError('count(文本, "子串") 需要一个字符串和一个非空子串')
        return args[0].count(args[1])
    if name == "abs":
        if len(args) != 1:
            raise FormulaError("abs 需要一个数字参数")
        return abs(_as_number(args[0], "abs 的参数"))
    if name in ("round", "floor", "ceil"):
        # round 收第二个可选参数（保留几位小数）。业务报表里"均价 12.35"
        # 这类需求极常见，只有整数取整时模型只能去别处凑，或者干脆放弃四舍五入。
        if name == "round" and len(args) == 2:
            value = _as_number(args[0], "round 的参数")
            digits = _as_number(args[1], "round 的小数位数")
            if digits != int(digits) or not 0 <= int(digits) <= 10:
                raise FormulaError("round 的小数位数必须是 0..10 之间的整数")
            return round(value, int(digits))
        if len(args) != 1:
            raise FormulaError(
                f"{name} 需要一个数字参数"
                + ("（round 也可以写 round(数字, 小数位数)）" if name == "round" else "")
            )
        value = _as_number(args[0], f"{name} 的参数")
        if name == "round":
            return round(value)
        return math.floor(value) if name == "floor" else math.ceil(value)
    values = _numbers_from(args, name)
    if name == "sum":
        return sum(values)
    if name == "avg":
        return sum(values) / len(values)
    if name == "min":
        return min(values)
    if name == "max":
        return max(values)
    raise FormulaError(f"不支持的函数 {name}")


def check_formula(expression: str) -> None:
    """只解析，不求值。语法有毛病就抛 FormulaError。

    存在的理由：公式写在节点配置里，是**静态可查**的，
    可发布校验从来不碰它——语法写错的公式能顺利发布，
    第一次真跑才炸。真机上就有过一次：
    「node assigner failed: 公式包含不支持的字符 '.'（位置 8）」。
    业主眼里是"东西发布了、跑起来却坏了"，而这个错在发布那一刻就能看见。

    只解析：变量的值要到运行时才有，求值必然失败，那不是语法问题。
    """
    if not isinstance(expression, str) or not expression.strip():
        raise FormulaError("公式为空")
    _Parser(_tokenize(expression)).parse()


def evaluate_formula(expression: str, vars_: dict[str, Any] | None = None) -> Any:
    """解析并求值一条中缀公式。vars_ 里的值必须已解析为数字/数字列表/布尔。"""

    if not isinstance(expression, str) or not expression.strip():
        raise FormulaError("公式为空")
    tree = _Parser(_tokenize(expression)).parse()
    result = _evaluate(tree, vars_ or {})
    if isinstance(result, float) and not math.isfinite(result):
        raise FormulaError("结果不是有限数")
    return result
