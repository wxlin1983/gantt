"""Restricted ``{{ }}`` expression evaluator (implement.md §4.4).

Implemented as ``ast.parse`` plus a whitelist node walk. **eval/exec are never
used.** Any syntax node outside the whitelist is rejected, so template authors
cannot reach the filesystem, the network or interpreter internals.

Namespaces:

===========  =========================================================
``para.*``   parameters supplied by the user
``case.*``   ``case.name`` / ``case.target_date`` / ``case.created_at``
``role.*``   usernames bound to template roles at case creation
``item``     current element, only inside a ``for_each`` node
``index``    0-based index, only inside a ``for_each`` node
===========  =========================================================

`para` / `case` / `role` are syntactically attribute access
(``ast.Attribute``), so the whitelist permits attribute access of **depth one**
whose left side is one of those namespaces. ``para.x.y`` and every other form
of attribute access is rejected: that restriction is what stops sandbox escapes
like ``().__class__.__bases__``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from .errors import DslError

# A string that is exactly one expression yields a native value, so that
# for_each receives a list and when receives a bool.
_FULL_EXPR = re.compile(r"^\s*\{\{(.+?)\}\}\s*$", re.DOTALL)
_EMBEDDED = re.compile(r"\{\{(.+?)\}\}", re.DOTALL)

NAMESPACES = frozenset({"para", "case", "role"})
LOOP_NAMES = frozenset({"item", "index"})

#: Cap for range() and sequence repetition, so that an expression like
#: range(10**9) cannot exhaust memory during expansion.
MAX_SEQUENCE_SIZE = 1000

_ALLOWED_FUNCS: dict[str, Any] = {
    "int": int,
    "float": float,
    "str": str,
    "round": round,
    "max": max,
    "min": min,
    "len": len,
    "range": range,
}

# Pow is deliberately excluded: the spec lists only the four arithmetic
# operators, and 2**9999999 is a cheap denial-of-service vector.
_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
}

_CMP_OPS: dict[type[ast.cmpop], Any] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

_MISSING = object()


@dataclass(slots=True)
class EvalContext:
    """Namespaces visible during evaluation."""

    para: dict[str, Any] = field(default_factory=dict)
    case: dict[str, Any] = field(default_factory=dict)
    role: dict[str, Any] = field(default_factory=dict)
    item: Any = _MISSING
    index: Any = _MISSING

    def with_loop(self, item: Any, index: int) -> EvalContext:
        return EvalContext(self.para, self.case, self.role, item, index)

    @property
    def in_loop(self) -> bool:
        return self.index is not _MISSING


class _Evaluator(ast.NodeVisitor):
    def __init__(self, ctx: EvalContext, path: str, source: str):
        self.ctx = ctx
        self.path = path
        self.source = source

    def _fail(self, code: str, message: str) -> DslError:
        detail = self.source.strip()
        return DslError.single(code, f"{message} (in: {detail})", self.path)

    def generic_visit(self, node: ast.AST):
        raise self._fail(
            "E_BAD_EXPRESSION",
            f"syntax not allowed here: {type(node).__name__}",
        )

    def visit_Expression(self, node: ast.Expression):
        return self.visit(node.body)

    def visit_Constant(self, node: ast.Constant):
        return node.value

    def visit_Name(self, node: ast.Name):
        name = node.id
        if name in LOOP_NAMES:
            value = getattr(self.ctx, name)
            if value is _MISSING:
                raise self._fail(
                    "E_BAD_EXPRESSION",
                    f"`{name}` is only available inside a for_each node",
                )
            return value
        if name in NAMESPACES:
            raise self._fail(
                "E_BAD_EXPRESSION",
                f"`{name}` is a namespace; write {name}.field",
            )
        raise self._fail("E_BAD_EXPRESSION", f"unknown name: {name}")

    def visit_Attribute(self, node: ast.Attribute):
        # Only depth-one access rooted at a known namespace is permitted.
        if (
            not isinstance(node.value, ast.Name)
            or node.value.id not in NAMESPACES
        ):
            raise self._fail(
                "E_BAD_EXPRESSION",
                "only para.x / case.x / role.x attribute access is allowed",
            )
        namespace = node.value.id
        table: dict[str, Any] = getattr(self.ctx, namespace)
        if node.attr not in table:
            code = (
                "E_UNKNOWN_PARAM"
                if namespace == "para"
                else "E_BAD_EXPRESSION"
            )
            raise self._fail(code, f"{namespace}.{node.attr} is not defined")
        return table[node.attr]

    def visit_BinOp(self, node: ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise self._fail(
                "E_BAD_EXPRESSION",
                f"operator not allowed: {type(node.op).__name__} "
                "(exponentiation is not available)",
            )
        left, right = self.visit(node.left), self.visit(node.right)
        if isinstance(node.op, ast.Mult):
            self._guard_sequence_multiply(left, right)
        try:
            return op(left, right)
        except ZeroDivisionError as exc:
            raise self._fail("E_BAD_EXPRESSION", "division by zero") from exc
        except TypeError as exc:
            raise self._fail(
                "E_BAD_EXPRESSION", f"incompatible types: {exc}"
            ) from exc

    def _guard_sequence_multiply(self, left: Any, right: Any) -> None:
        """Block ``"x" * 10**9`` style memory exhaustion."""
        for seq, count in ((left, right), (right, left)):
            if isinstance(seq, str | list | tuple) and isinstance(count, int):
                if len(seq) * max(count, 0) > MAX_SEQUENCE_SIZE:
                    raise self._fail(
                        "E_BAD_EXPRESSION",
                        f"repeated sequence exceeds the "
                        f"{MAX_SEQUENCE_SIZE} element cap",
                    )

    def visit_UnaryOp(self, node: ast.UnaryOp):
        operand = self.visit(node.operand)
        match node.op:
            case ast.USub():
                return -operand
            case ast.UAdd():
                return +operand
            case ast.Not():
                return not operand
            case _:
                raise self._fail(
                    "E_BAD_EXPRESSION",
                    f"unary operator not allowed: {type(node.op).__name__}",
                )

    def visit_BoolOp(self, node: ast.BoolOp):
        # Short-circuits like Python, returning the deciding operand.
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = self.visit(value)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = self.visit(value)
            if result:
                return result
        return result

    def visit_Compare(self, node: ast.Compare):
        left = self.visit(node.left)
        pairs = zip(node.ops, node.comparators, strict=True)
        for op_node, comparator in pairs:
            op = _CMP_OPS.get(type(op_node))
            if op is None:
                raise self._fail(
                    "E_BAD_EXPRESSION",
                    f"comparison not allowed: {type(op_node).__name__}",
                )
            right = self.visit(comparator)
            try:
                if not op(left, right):
                    return False
            except TypeError as exc:
                raise self._fail(
                    "E_BAD_EXPRESSION", f"values are not comparable: {exc}"
                ) from exc
            left = right
        return True

    def visit_Call(self, node: ast.Call):
        if not isinstance(node.func, ast.Name):
            raise self._fail(
                "E_BAD_EXPRESSION",
                "only whitelisted named functions may be called",
            )
        func = _ALLOWED_FUNCS.get(node.func.id)
        if func is None:
            allowed = ", ".join(sorted(_ALLOWED_FUNCS))
            raise self._fail(
                "E_BAD_EXPRESSION",
                f"{node.func.id}() is not available; allowed: {allowed}",
            )
        if node.keywords:
            raise self._fail(
                "E_BAD_EXPRESSION", "keyword arguments are not supported"
            )
        args = [self.visit(arg) for arg in node.args]
        if func is range:
            return self._safe_range(args)
        try:
            return func(*args)
        except (TypeError, ValueError) as exc:
            raise self._fail(
                "E_BAD_EXPRESSION", f"{node.func.id}() failed: {exc}"
            ) from exc

    def _safe_range(self, args: list[Any]) -> list[int]:
        try:
            values = range(*args)
        except (TypeError, ValueError) as exc:
            raise self._fail(
                "E_BAD_EXPRESSION", f"range() failed: {exc}"
            ) from exc
        if len(values) > MAX_SEQUENCE_SIZE:
            raise self._fail(
                "E_BAD_EXPRESSION",
                f"range() yields {len(values)} elements, over the "
                f"{MAX_SEQUENCE_SIZE} cap",
            )
        return list(values)

    def visit_List(self, node: ast.List):
        return [self.visit(element) for element in node.elts]

    def visit_Tuple(self, node: ast.Tuple):
        return tuple(self.visit(element) for element in node.elts)


def evaluate(source: str, ctx: EvalContext, path: str = "") -> Any:
    """Evaluate one expression body (without the surrounding ``{{ }}``)."""
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:
        raise DslError.single(
            "E_BAD_EXPRESSION",
            f"malformed expression {source.strip()!r}: {exc.msg}",
            path,
        ) from exc
    return _Evaluator(ctx, path, source).visit(tree)


def render(value: Any, ctx: EvalContext, path: str = "") -> Any:
    """Expand ``{{ }}`` inside a string.

    A string that is exactly one expression returns a **native value**
    (``"{{ range(3) }}"`` -> ``[0, 1, 2]``); otherwise the expressions are
    interpolated into text (``"batch {{ index + 1 }}"`` -> ``"batch 1"``).
    Non-string values pass through untouched.
    """
    if not isinstance(value, str):
        return value

    full = _FULL_EXPR.match(value)
    if full is not None:
        return evaluate(full.group(1), ctx, path)

    def substitute(match: re.Match[str]) -> str:
        return str(evaluate(match.group(1), ctx, path))

    return _EMBEDDED.sub(substitute, value)


def has_expression(value: Any) -> bool:
    return isinstance(value, str) and _EMBEDDED.search(value) is not None


def referenced_params(value: Any) -> set[str]:
    """Collect ``para.*`` names referenced by a string.

    Used for the unused-parameter warning. Syntax errors are skipped silently
    here; reporting them is :func:`evaluate`'s job, this is a best-effort scan.
    """
    if not isinstance(value, str):
        return set()
    found: set[str] = set()
    for match in _EMBEDDED.finditer(value):
        try:
            tree = ast.parse(match.group(1).strip(), mode="eval")
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "para"
            ):
                found.add(node.attr)
    return found
