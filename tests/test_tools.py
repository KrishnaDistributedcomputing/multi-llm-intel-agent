"""Arithmetic boundaries and tool input validation."""

import json
from datetime import datetime

import pytest

from agent.tools import ToolError, execute_tool


@pytest.mark.parametrize(
    ("expression", "expected"),
    [("6 * 7", 42), ("(125 * 8) / 4", 250), ("-3 + +5", 2), ("7 % 3", 1)],
)
def test_calculator(expression: str, expected: float) -> None:
    assert json.loads(execute_tool("calculator", json.dumps({"expression": expression}))) == {
        "result": expected
    }


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo bad')",
        "2 ** 100000000",
        "1 / 0",
        "True + 1",
        "1e999",
        "1000000000001",
        "[1,2]",
        "sum([1,2])",
        "+".join(["1"] * 30),
        "1" * 161,
        "",
    ],
)
def test_rejected_arithmetic(expression: str) -> None:
    with pytest.raises(ToolError):
        execute_tool("calculator", json.dumps({"expression": expression}))


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("shell", "{}"),
        ("calculator", '{"expression": "1", "extra": true}'),
        ("calculator", '{"expression": 12}'),
        ("calculator", "not-json"),
        ("utc_time", '{"extra": true}'),
    ],
)
def test_rejected_tool_input(name: str, arguments: str) -> None:
    with pytest.raises(ToolError):
        execute_tool(name, arguments)


def test_utc() -> None:
    timestamp = json.loads(execute_tool("utc_time", "{}"))["utc"]
    assert datetime.fromisoformat(timestamp).utcoffset().total_seconds() == 0
