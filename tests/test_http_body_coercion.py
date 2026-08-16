"""http_request 正文解析：content-type 不可全信时看正文形状。

金蝶云星空 WebAPI 用 text/plain 回 JSON——真实集成里挖出的坑。
只认 content-type 会把 JSON 当字符串交给下游，$sum / 索引全炸。
"""

from __future__ import annotations

from agent_platform.workflow_runtime import _coerce_http_body


def test_content_type_json_parses() -> None:
    assert _coerce_http_body("application/json", '[[1,2],[3,4]]') == [[1, 2], [3, 4]]


def test_text_plain_json_array_parses() -> None:
    # 金蝶的真实回包形态：text/plain 但正文是 JSON 数组
    body = '[["12SO000001","FYLGSP",393163.26]]'
    assert _coerce_http_body("text/plain; charset=utf-8", body) == [
        ["12SO000001", "FYLGSP", 393163.26]
    ]


def test_text_plain_json_object_parses() -> None:
    assert _coerce_http_body("text/plain", '{"total": 621565.64}') == {"total": 621565.64}


def test_leading_whitespace_before_json_still_parses() -> None:
    assert _coerce_http_body("text/plain", '\n  [1,2,3]') == [1, 2, 3]


def test_plain_text_stays_text() -> None:
    assert _coerce_http_body("text/plain", "hello world") == "hello world"


def test_json_shaped_but_invalid_falls_back_to_text() -> None:
    # 首字符像 JSON 但解析失败——退回原文，不抛异常
    broken = '[not really json'
    assert _coerce_http_body("text/plain", broken) == broken


def test_empty_body_stays_empty() -> None:
    assert _coerce_http_body("text/plain", "") == ""
