"""Safe arithmetic calculator.

Expressions are parsed with `ast.parse(mode="eval")` and evaluated by walking an explicit
whitelist of node types. `eval`/`exec` are never used. Anything else, including names,
attribute access, subscripts, comprehensions, lambdas, strings, imports and calls to
functions not in `FUNCTIONS`, is rejected before evaluation. Size and magnitude limits
keep evaluation fast.
"""

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.tools.errors import InvalidToolArgumentsError, ToolExecutionError
from app.events.types import ActionCategory
from app.tools.schemas import ToolContext, ToolDefinition

MAX_EXPRESSION_LENGTH = 500
MAX_NODES = 200
MAX_EXPONENT = 1000
MAX_ABS_VALUE = 1e100

BINARY_OPERATORS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
UNARY_OPERATORS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
}
CONSTANTS = {"pi": math.pi, "e": math.e}


class CalculatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str = Field(min_length=1, max_length=MAX_EXPRESSION_LENGTH)


class CalculatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expression: str
    result: float | int


def evaluate(expression: str) -> float | int:
    """Evaluate an arithmetic expression. Raises InvalidToolArgumentsError for anything
    outside the whitelist and ToolExecutionError for math errors."""
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise InvalidToolArgumentsError(f"expression longer than {MAX_EXPRESSION_LENGTH} chars")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise InvalidToolArgumentsError(f"not a valid expression: {exc}") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise InvalidToolArgumentsError(f"expression has more than {MAX_NODES} nodes")
    try:
        return _eval(tree.body)
    except (ZeroDivisionError, OverflowError, ValueError, TypeError) as exc:
        raise ToolExecutionError(f"math error: {exc}") from None


def _eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Constant):
        # type() rather than isinstance: bool is a subclass of int and is rejected.
        if type(node.value) not in (int, float):
            raise InvalidToolArgumentsError(f"unsupported literal {node.value!r}")
        return _checked(node.value)
    if isinstance(node, ast.BinOp):
        op = BINARY_OPERATORS.get(type(node.op))
        if op is None:
            raise InvalidToolArgumentsError(f"unsupported operator {type(node.op).__name__}")
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise InvalidToolArgumentsError(f"exponent larger than {MAX_EXPONENT}")
        return _checked(op(left, right))
    if isinstance(node, ast.UnaryOp):
        unary = UNARY_OPERATORS.get(type(node.op))
        if unary is None:
            raise InvalidToolArgumentsError(f"unsupported operator {type(node.op).__name__}")
        return _checked(unary(_eval(node.operand)))
    if isinstance(node, ast.Name):
        if node.id not in CONSTANTS:
            raise InvalidToolArgumentsError(f"unknown name {node.id!r}")
        return CONSTANTS[node.id]
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            raise InvalidToolArgumentsError("only these functions are allowed: " + ", ".join(sorted(FUNCTIONS)))
        if node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
            raise InvalidToolArgumentsError("keyword and starred arguments are not allowed")
        if not 1 <= len(node.args) <= 10:
            raise InvalidToolArgumentsError("functions take between 1 and 10 arguments")
        return _checked(FUNCTIONS[node.func.id](*(_eval(arg) for arg in node.args)))
    raise InvalidToolArgumentsError(f"unsupported syntax: {type(node).__name__}")


def _checked(value: Any) -> Any:
    if isinstance(value, complex):
        raise ToolExecutionError("complex result")
    if isinstance(value, float) and not math.isfinite(value):
        raise ToolExecutionError("result is not finite")
    if abs(value) > MAX_ABS_VALUE:
        raise ToolExecutionError(f"magnitude exceeds {MAX_ABS_VALUE:g}")
    return value


class CalculatorTool:
    definition = ToolDefinition(
        name="calculator",
        description="Evaluate an arithmetic expression, e.g. '(1299 - 999) / 999 * 100'.",
        capabilities=(
            "Numbers, + - * / // % **, parentheses, constants pi and e, and the functions "
            + ", ".join(sorted(FUNCTIONS))
            + ". No variables, strings or other code."
        ),
        input_model=CalculatorInput,
        output_model=CalculatorOutput,
        risk_level="low",
        category=ActionCategory.READ_ONLY,
        timeout_seconds=2.0,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> CalculatorOutput:
        assert isinstance(arguments, CalculatorInput)
        return CalculatorOutput(expression=arguments.expression, result=evaluate(arguments.expression))
