# SPDX-License-Identifier: AGPL-3.0-or-later
"""Safe arithmetic for parametric values, e.g. "=length - 2*margin".

Only numbers, variable names, + - * / // % **, parentheses and a few math
functions are allowed. Trig functions take degrees.
"""

from __future__ import annotations

import ast
import math
import operator

_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "min": min, "max": max, "round": round,
    "sin": lambda d: math.sin(math.radians(d)), "cos": lambda d: math.cos(math.radians(d)),
    "tan": lambda d: math.tan(math.radians(d)), "atan": lambda x: math.degrees(math.atan(x)),
}
_CONSTS = {"pi": math.pi}


class ExprError(ValueError):
    pass


def evaluate(text: str, names: dict[str, float]) -> float:
    text = convert_units(text)
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        raise ExprError(f"cannot parse expression {text!r}")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            return _BIN[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name):
            if node.id in names:
                return float(names[node.id])
            if node.id in _CONSTS:
                return _CONSTS[node.id]
            raise ExprError(f"unknown variable {node.id!r} in {text!r}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return float(_FUNCS[node.func.id](*[ev(a) for a in node.args]))
        raise ExprError(f"unsupported syntax in {text!r}")

    try:
        return ev(tree)
    except ZeroDivisionError:
        raise ExprError(f"division by zero in {text!r}")


_UNITS = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8, "deg": 1.0}
_UNIT_RE = __import__("re").compile(r"(\d+(?:\.\d*)?|\.\d+)\s*(mm|cm|m|in|ft|deg)\b")


def convert_units(text: str) -> str:
    """'2 in + 5mm' -> '50.8 + 5.0' (project units are mm and degrees)."""
    return _UNIT_RE.sub(lambda m: repr(float(m.group(1)) * _UNITS[m.group(2)]), text)


def names_in(text: str) -> set[str]:
    """Variable names referenced by an expression (empty if it does not parse)."""
    try:
        tree = ast.parse(convert_units(text.strip().lstrip("=")), mode="eval")
    except SyntaxError:
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in _CONSTS and n.id not in _FUNCS}


def is_expression(value) -> bool:
    return isinstance(value, str) and value.strip().startswith("=")
