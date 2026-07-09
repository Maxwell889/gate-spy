"""PySR arithmetic candidate backend.

PySR is a search engine here; any expression it returns must still be converted
to the GateSpy AST and verified with Verilog bit-vector semantics.
"""

from __future__ import annotations

from fractions import Fraction

from .ast import Binary, Const, Expr, Unary, Var
from .resolver import Field
from .search import Candidate
from .verify import Sample


def _const_from_number(value, width: int) -> Const | None:
    try:
        frac = Fraction(value).limit_denominator(1024)
    except Exception:
        return None
    if frac.denominator == 1:
        return Const(frac.numerator, width)
    return None


def _sympy_to_expr(node, symbols: dict[str, Var], width: int) -> Expr | None:
    import sympy as sp

    if node.is_Symbol:
        return symbols.get(str(node))
    if node.is_Integer or node.is_Rational or node.is_Float:
        return _const_from_number(node, width)
    if isinstance(node, sp.Add):
        args = list(node.args)
        expr = _sympy_to_expr(args[0], symbols, width)
        if expr is None:
            return None
        for arg in args[1:]:
            rhs = _sympy_to_expr(arg, symbols, width)
            if rhs is None:
                return None
            expr = Binary("+", expr, rhs)
        return expr
    if isinstance(node, sp.Mul):
        args = list(node.args)
        expr = _sympy_to_expr(args[0], symbols, width)
        if expr is None:
            return None
        for arg in args[1:]:
            rhs = _sympy_to_expr(arg, symbols, width)
            if rhs is None:
                return None
            expr = Binary("*", expr, rhs)
        return expr
    if isinstance(node, sp.Pow):
        base = _sympy_to_expr(node.args[0], symbols, width)
        exp = _sympy_to_expr(node.args[1], symbols, width)
        if base is None or exp is None:
            return None
        return Binary("**", base, exp)
    if node.func.__name__ == "Neg":
        inner = _sympy_to_expr(node.args[0], symbols, width)
        return Unary("-", inner) if inner is not None else None
    return None


def pysr_candidates(input_fields: list[Field], target: Field,
                    samples: list[Sample], *,
                    niterations: int = 200,
                    timeout_s: int = 120,
                    maxsize: int = 32) -> tuple[list[Candidate], str]:
    """Run PySR and return converted arithmetic candidates plus a status line."""
    if niterations <= 0 or not input_fields or not samples:
        return [], "PySR skipped (niterations <= 0 or no samples)"

    try:
        import numpy as np
        from pysr import PySRRegressor
    except Exception as exc:
        return [], (
            "PySR unavailable; run install.sh or `uv sync --dev` to install "
            f"project dependencies ({exc})")

    X = np.array(
        [[sample.env[field.label].unsigned for field in input_fields]
         for sample in samples],
        dtype=float,
    )
    y = np.array([sample.target.unsigned for sample in samples], dtype=float)

    model_kwargs = dict(
        maxsize=maxsize,
        niterations=niterations,
        binary_operators=["+", "-", "*", "/", "%", "^"],
        unary_operators=[],
        model_selection="best",
        verbosity=0,
        progress=False,
    )
    if timeout_s > 0:
        model_kwargs["timeout_in_seconds"] = timeout_s
    try:
        model = PySRRegressor(**model_kwargs)
        model.fit(X, y)
    except TypeError:
        model_kwargs.pop("timeout_in_seconds", None)
        model = PySRRegressor(**model_kwargs)
        model.fit(X, y)
    except Exception as exc:
        return [], f"PySR failed: {exc}"

    symbols = {
        f"x{i}": Var(field.label, field.width)
        for i, field in enumerate(input_fields)
    }
    candidates: list[Candidate] = []
    equations = getattr(model, "equations_", None)
    if equations is None:
        return [], "PySR completed but returned no equations"
    for _, row in equations.iterrows():
        sym = row.get("sympy_format", None)
        if sym is None:
            continue
        expr = _sympy_to_expr(sym, symbols, target.width)
        if expr is not None:
            candidates.append(Candidate(expr, "pysr"))
    return candidates, f"PySR completed ({len(candidates)} convertible candidates)"
