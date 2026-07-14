"""Safe expression evaluator for lumenframe layer properties.

This module implements a restricted expression language for keyframe values,
inspired by After Effects expressions. Expressions are evaluated at render-time
in a controlled environment with whitelisted functions and bindings.

Example usage:
    expr = "time * 2"  # opacity animates 0→1 over 0.5s at 2x speed
    evaluator = SafeEvaluator(time=0.5)
    result = evaluator.eval(expr)  # → 1.0

    expr = "ease_in_quad(0, 100, 1.0)"  # position 0→100 over 1s
    evaluator = SafeEvaluator(time=0.8)
    result = evaluator.eval(expr)  # → 64

Safety guarantees:
  - No `import`, `exec`, `eval`, `__` access
  - Only arithmetic, comparison, logical operators
  - Only whitelisted functions (math, easing, layer refs)
  - Parse-time syntax validation
  - No mutation of bindings
"""

import ast
import math
from typing import Any


class ExprError(ValueError):
    """Expression evaluation or parse error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        self.message = str(message)
        super().__init__(f"{self.code}: {self.message}")


# ── Whitelisted functions ──────────────────────────────────────────────────


def _ease_in_quad(a: float, b: float, t: float) -> float:
    """Ease in (quadratic). t ∈ [0,1]."""
    t = max(0.0, min(1.0, float(t)))
    return a + (b - a) * (t * t)


def _ease_out_quad(a: float, b: float, t: float) -> float:
    """Ease out (quadratic)."""
    t = max(0.0, min(1.0, float(t)))
    return a + (b - a) * (1 - (1 - t) * (1 - t))


def _ease_in_out_quad(a: float, b: float, t: float) -> float:
    """Ease in-out (quadratic)."""
    t = max(0.0, min(1.0, float(t)))
    if t < 0.5:
        return a + (b - a) * (2 * t * t)
    else:
        return a + (b - a) * (1 - 2 * (1 - t) * (1 - t))


def _ease_in_cubic(a: float, b: float, t: float) -> float:
    """Ease in (cubic)."""
    t = max(0.0, min(1.0, float(t)))
    return a + (b - a) * (t * t * t)


def _ease_out_cubic(a: float, b: float, t: float) -> float:
    """Ease out (cubic)."""
    t = max(0.0, min(1.0, float(t)))
    return a + (b - a) * (1 - (1 - t) * (1 - t) * (1 - t))


def _ease_in_out_cubic(a: float, b: float, t: float) -> float:
    """Ease in-out (cubic)."""
    t = max(0.0, min(1.0, float(t)))
    if t < 0.5:
        return a + (b - a) * (4 * t * t * t)
    else:
        return a + (b - a) * (1 - 4 * (1 - t) * (1 - t) * (1 - t))


def _linear(a: float, b: float, t: float) -> float:
    """Linear interpolation."""
    t = max(0.0, min(1.0, float(t)))
    return a + (b - a) * t


WHITELIST_FUNCTIONS = {
    "ease_in_quad": _ease_in_quad,
    "ease_out_quad": _ease_out_quad,
    "ease_in_out_quad": _ease_in_out_quad,
    "ease_in_cubic": _ease_in_cubic,
    "ease_out_cubic": _ease_out_cubic,
    "ease_in_out_cubic": _ease_in_out_cubic,
    "linear": _linear,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "exp": math.exp,
    "min": min,
    "max": max,
}


# ── Parser ─────────────────────────────────────────────────────────────────


class _SafeNodeVisitor(ast.NodeVisitor):
    """Validate that an AST contains only safe operations."""

    FORBIDDEN_NAMES = {
        "__import__",
        "__builtins__",
        "__loader__",
        "__spec__",
        "__cached__",
        "__file__",
        "__annotations__",
        "eval",
        "exec",
        "compile",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "__dict__",
    }

    def __init__(self, allowed_names: set[str]) -> None:
        self.allowed_names = allowed_names
        self.errors = []

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.FORBIDDEN_NAMES:
            self.errors.append(f"Forbidden name: {node.id}")
        elif node.id not in self.allowed_names:
            self.errors.append(f"Undefined name: {node.id}")

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            if node.func.id not in WHITELIST_FUNCTIONS:
                self.errors.append(f"Forbidden function: {node.func.id}")
        elif isinstance(node.func, ast.Attribute):
            self.errors.append(f"Forbidden: attribute access")
        else:
            self.errors.append(f"Forbidden: complex call")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.errors.append(f"Forbidden: attribute access")

    def visit_Import(self, node: ast.Import) -> None:
        self.errors.append(f"Forbidden: import")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.errors.append(f"Forbidden: import")

    def visit_Subscript(self, node: ast.Subscript) -> None:
        self.errors.append(f"Forbidden: subscript/indexing")

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.errors.append(f"Forbidden: lambda")

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self.errors.append(f"Forbidden: list comprehension")

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self.errors.append(f"Forbidden: dict comprehension")

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self.errors.append(f"Forbidden: set comprehension")

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.errors.append(f"Forbidden: generator expression")

    def visit_Assign(self, node: ast.Assign) -> None:
        self.errors.append(f"Forbidden: assignment")

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.errors.append(f"Forbidden: augmented assignment")

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.errors.append(f"Forbidden: annotated assignment")

    def visit_For(self, node: ast.For) -> None:
        self.errors.append(f"Forbidden: for loop")

    def visit_While(self, node: ast.While) -> None:
        self.errors.append(f"Forbidden: while loop")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.errors.append(f"Forbidden: function definition")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.errors.append(f"Forbidden: async function definition")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.errors.append(f"Forbidden: class definition")


# ── Safe evaluator ────────────────────────────────────────────────────────


class SafeEvaluator:
    """Evaluate an expression string in a controlled environment.

    Args:
        time: Current time in seconds (float). Auto-available in expressions.
        **bindings: Additional name bindings (e.g., width=1920, duration=5.0).

    Example:
        evaluator = SafeEvaluator(time=0.5, width=1920, duration=5.0)
        result = evaluator.eval("width * sin(time * 2 * 3.14159)")
    """

    def __init__(self, time: float = 0.0, **bindings: Any) -> None:
        self.time = float(time)
        self.bindings = {"time": self.time, **bindings}

    def eval(self, expr_str: str) -> Any:
        """Evaluate an expression string. Returns a number or bool.

        Raises ExprError on parse/eval failure.
        """
        expr_str = (expr_str or "").strip()
        if not expr_str:
            raise ExprError("E_EMPTY", "Expression is empty")

        # Parse
        try:
            tree = ast.parse(expr_str, mode="eval")
        except SyntaxError as e:
            raise ExprError("E_SYNTAX", f"Parse error: {e.msg}") from None

        # Validate
        allowed_names = set(self.bindings.keys()) | set(WHITELIST_FUNCTIONS.keys())
        visitor = _SafeNodeVisitor(allowed_names)
        visitor.visit(tree)
        if visitor.errors:
            raise ExprError("E_UNSAFE", f"Validation failed: {visitor.errors[0]}")

        # Evaluate
        try:
            compiled = compile(tree, filename="<expr>", mode="eval")
            ns = dict(self.bindings)
            ns.update(WHITELIST_FUNCTIONS)
            result = eval(compiled, {"__builtins__": {}}, ns)
            return result
        except ZeroDivisionError as e:
            raise ExprError("E_RUNTIME", f"Division by zero") from None
        except (ValueError, TypeError) as e:
            raise ExprError("E_RUNTIME", f"Runtime error: {e}") from None
        except Exception as e:
            raise ExprError("E_RUNTIME", f"Unexpected error: {e}") from None


# ── Validation helpers ─────────────────────────────────────────────────────


def validate_expression(expr_str: str) -> tuple[bool, str | None]:
    """Check if an expression string is valid (safe + parseable).

    Returns (is_valid, error_message).
    """
    try:
        evaluator = SafeEvaluator()
        evaluator.eval(expr_str)
        return True, None
    except ExprError as e:
        return False, e.message
    except Exception as e:
        return False, str(e)
