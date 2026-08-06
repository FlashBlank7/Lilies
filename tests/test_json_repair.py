from __future__ import annotations

import json

from agent_platform.json_repair import parse_tool_input, repair_json_text


def test_repairs_the_exact_unescaped_quote_failure_from_the_build_transcript() -> None:
    # Reduced from build d9a4dd84 turn 8: prose quotes inside a JSON string.
    raw = '{"node": {"id":"m1","config":{"system":"不声称已完成（如"已抓取""已处理"）,如实说明"}}}'
    parsed, repaired = parse_tool_input(raw)
    assert repaired is True
    assert parsed is not None
    assert parsed["node"]["config"]["system"] == '不声称已完成（如"已抓取""已处理"）,如实说明'


def test_repairs_raw_newlines_inside_strings() -> None:
    raw = '{"template":"第一行\n第二行"}'
    parsed, repaired = parse_tool_input(raw)
    assert repaired is True
    assert parsed == {"template": "第一行\n第二行"}


def test_valid_json_passes_through_untouched() -> None:
    raw = json.dumps({"a": 'say "hi"', "n": 3}, ensure_ascii=False)
    parsed, repaired = parse_tool_input(raw)
    assert repaired is False
    assert parsed == {"a": 'say "hi"', "n": 3}
    assert repair_json_text(raw) == raw


def test_completes_a_missing_trailing_brace_from_build_transcript() -> None:
    # Build b9f2d788 turn 10: the model emitted a full node payload but
    # forgot one closing brace; the parser failed at EOF.
    raw = '{"node": {"id":"analyze","type":"model_turn","config":{"settings":{"system":"规则：\\n1. 仅处理日语评论","output_format":"json"},"position":{"x":880,"y":200}}}'
    parsed, repaired = parse_tool_input(raw)
    assert repaired is True
    assert parsed is not None
    assert parsed["node"]["config"]["settings"]["output_format"] == "json"


def test_repairs_content_quotes_followed_by_closing_braces() -> None:
    # Build b9f2d788 turn 12: Jinja escaping inside a template value —
    # '{{ "{{" }}' has a content quote immediately before ' }}'.
    raw = '{"config":{"template":"清单\\n{{ "{{" }} expressions {{ "}}" }}","variables":{}}}'
    parsed, repaired = parse_tool_input(raw)
    assert repaired is True
    assert parsed is not None
    assert parsed["config"]["template"] == '清单\n{{ "{{" }} expressions {{ "}}" }}'


def test_unrepairable_input_still_reports_failure() -> None:
    parsed, repaired = parse_tool_input('{"a": [1, 2,, oops')
    assert parsed is None
    assert repaired is False


def test_non_object_json_is_rejected() -> None:
    parsed, repaired = parse_tool_input("[1, 2, 3]")
    assert parsed is None
    assert repaired is False
