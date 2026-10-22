"""Bounded arithmetic and UTC tools without eval, shell, files or network access."""

import ast
import json
import math
import operator
from datetime import UTC, datetime
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class CalculatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expression: Annotated[str, Field(min_length=1, max_length=160)]


class TimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolError(ValueError):
    """A rejected tool request."""


class Calculator:
    operations: ClassVar = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
    }

    @classmethod
    def calculate(cls, expression: str) -> float:
        tree = ast.parse(expression, mode="eval")
        if len(list(ast.walk(tree))) > 32:
            raise ToolError("Expression exceeds 32 syntax nodes")

        def visit(node: ast.AST) -> float:
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)
            ):
                value = float(node.value)
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                operand = visit(node.operand)
                value = operand if isinstance(node.op, ast.UAdd) else -operand
            elif isinstance(node, ast.BinOp) and type(node.op) in cls.operations:
                value = cls.operations[type(node.op)](visit(node.left), visit(node.right))
            else:
                raise ToolError("Only numbers, parentheses, +, -, *, / and % are allowed")
            if not math.isfinite(value) or abs(value) > 1e12:
                raise ToolError("Arithmetic values must be finite and within +/-1e12")
            return value

        return visit(tree.body)


TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Calculate arithmetic using +, -, *, /, %, and parentheses.",
            "parameters": CalculatorInput.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "utc_time",
            "description": "Return the current UTC time in ISO 8601 format.",
            "parameters": TimeInput.model_json_schema(),
        },
    },
]


def execute_tool(name: str, arguments: str) -> str:
    try:
        if name == "calculator":
            data = CalculatorInput.model_validate_json(arguments)
            return json.dumps({"result": Calculator.calculate(data.expression)})
        if name == "utc_time":
            TimeInput.model_validate_json(arguments)
            return json.dumps({"utc": datetime.now(UTC).isoformat()})
        raise ToolError("Unknown tool")
    except (ValueError, SyntaxError, ArithmeticError, RecursionError) as exc:
        raise ToolError(f"Rejected {name} tool input: {exc}") from exc
