"""Deterministic repair for the tool-input JSON models actually emit.

Every rule here is reduced from a real failure captured in a build
transcript:

1. Interior ASCII quotes — prose like ``（如"已抓取"）`` inside a JSON string
   ends the string early. A quote only closes the string when what follows
   (after whitespace, and after any run of closing brackets) is structural.
2. Raw control characters inside strings (newlines, tabs) get escaped.
3. Missing trailing closers — a model that forgets the final ``}`` (or is
   truncated) leaves a balanced-prefix document; the scanner tracks the open
   bracket stack and completes it.

If the repaired text still does not parse, the caller falls back to the
normal invalid-input flow — repair can only turn failures into successes.
"""

from __future__ import annotations

import json
from typing import Any

_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
_WHITESPACE = " \t\r\n"


def _quote_is_closing(raw: str, index: int) -> bool:
    """Decide whether the quote at *index* ends the current string."""

    lookahead = index + 1
    length = len(raw)
    while lookahead < length and raw[lookahead] in _WHITESPACE:
        lookahead += 1
    if lookahead >= length:
        return True
    if raw[lookahead] in ",:":
        return True
    if raw[lookahead] in "}]":
        # Skip the whole closer run: a genuine string end here is followed by
        # ``,`` or the end of the document. Anything else (e.g. the Jinja text
        # ``"{{" }} expressions``) means the quote was content.
        while lookahead < length and raw[lookahead] in "}]" + _WHITESPACE:
            lookahead += 1
        return lookahead >= length or raw[lookahead] == ","
    return False


def repair_json_text(raw: str) -> str:
    """Escape interior quotes/control chars and complete missing closers."""

    out: list[str] = []
    stack: list[str] = []
    in_string = False
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index]
        if not in_string:
            if char == '"':
                in_string = True
            elif char in "{[":
                stack.append(char)
            elif char in "}]" and stack:
                stack.pop()
            out.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            out.append(char)
            out.append(raw[index + 1])
            index += 2
            continue
        if char in _CONTROL_ESCAPES:
            out.append(_CONTROL_ESCAPES[char])
            index += 1
            continue
        if char == '"':
            if _quote_is_closing(raw, index):
                in_string = False
                out.append(char)
            else:
                out.append('\\"')
            index += 1
            continue
        out.append(char)
        index += 1
    if in_string:
        out.append('"')
    for opener in reversed(stack):
        out.append("}" if opener == "{" else "]")
    return "".join(out)


def parse_tool_input(raw: str) -> tuple[dict[str, Any] | None, bool]:
    """Parse tool-input JSON, attempting one deterministic repair.

    Returns ``(parsed, repaired)``; ``(None, False)`` when even the repaired
    text does not parse or does not produce an object.
    """

    try:
        value = json.loads(raw)
        return (value, False) if isinstance(value, dict) else (None, False)
    except json.JSONDecodeError:
        pass
    try:
        value = json.loads(repair_json_text(raw))
    except json.JSONDecodeError:
        return None, False
    return (value, True) if isinstance(value, dict) else (None, False)
