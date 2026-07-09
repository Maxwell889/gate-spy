"""Restricted template fitting for scalable word-level recovery."""

from __future__ import annotations

from itertools import permutations
from fractions import Fraction

import sympy as sp

from ..symbolic.ast import Binary, Cast, Concat, Const, Expr, Replicate, Select, Var
from ..symbolic.bitvec import BitVec, mask
from ..symbolic.resolver import Field
from ..symbolic.search import Candidate
from ..symbolic.verify import Sample


def _eval_expr(expr: Expr, samples: list[Sample], width: int) -> bool:
    target_mask = mask(width)
    for sample in samples:
        try:
            value = expr.evaluate(sample.env)
        except Exception:
            return False
        if (value.unsigned & target_mask) != sample.target.unsigned:
            return False
    return True


def _mul_const(coeff: int, var: Var, width: int) -> Expr:
    if coeff == 1:
        return var
    return Binary("*", Const(coeff, max(width, var.width)), var)


def _linear_expr(coeffs: list[int], fields: list[Field], width: int) -> Expr:
    const = coeffs[0]
    terms: list[Expr] = []
    if const:
        terms.append(Const(const, max(1, width)))
    for coeff, field in zip(coeffs[1:], fields):
        if coeff == 0:
            continue
        var = Var(field.label, field.width)
        term = _mul_const(abs(coeff), var, max(width, field.width))
        if coeff < 0:
            term = Binary("-", Const(0, max(width, field.width)), term)
        terms.append(term)
    if not terms:
        return Const(0, max(1, width))
    expr = terms[0]
    for term in terms[1:]:
        expr = Binary("+", expr, term)
    return expr


def _fit_linear(fields: list[Field], samples: list[Sample],
                width: int) -> Expr | None:
    if not fields or len(samples) < len(fields) + 1:
        return None
    rows = []
    rhs = []
    for sample in samples[:max(len(fields) + 1, min(len(samples), 64))]:
        rows.append([1] + [sample.env[field.label].unsigned for field in fields])
        rhs.append(sample.target.unsigned)
    try:
        solution = sp.linsolve((sp.Matrix(rows), sp.Matrix(rhs)))
    except Exception:
        return None
    if not solution:
        return None
    sol = next(iter(solution), None)
    if sol is None:
        return None
    coeffs: list[int] = []
    for value in sol:
        if value.free_symbols:
            return None
        frac = Fraction(value)
        if frac.denominator != 1:
            return None
        coeffs.append(frac.numerator)
    expr = _linear_expr(coeffs, fields, width)
    return expr if _eval_expr(expr, samples, width) else None


def _fold(op: str, exprs: list[Expr]) -> Expr | None:
    if not exprs:
        return None
    out = exprs[0]
    for expr in exprs[1:]:
        out = Binary(op, out, expr)
    return out


def _threshold_candidates(fields: list[Field], samples: list[Sample],
                          width: int) -> list[Expr]:
    if width != 1:
        return []
    out: list[Expr] = []
    for field in fields:
        vals = sorted({sample.env[field.label].unsigned for sample in samples})
        for threshold in vals[:32]:
            rhs = Const(threshold, field.width)
            for op in (">", ">=", "<", "<=", "==", "!="):
                expr = Binary(op, Var(field.label, field.width), rhs)
                if _eval_expr(expr, samples, width):
                    out.append(expr)
    return out


def _zero_extend(field: Field, width: int) -> Expr:
    var = Var(field.label, field.width)
    if width <= field.width:
        return var
    return Concat((Const(0, width - field.width), var))


def _add_const(expr: Expr, value: int, width: int) -> Expr:
    if value == 0:
        return expr
    if value > 0:
        return Binary("+", expr, Const(value, width))
    return Binary("-", expr, Const(-value, width))


def _apply_modular_delta(expr: Expr, delta: int, width: int) -> Expr:
    modulus = 1 << width
    delta &= modulus - 1
    if delta == 0:
        return expr
    half = 1 << (width - 1)
    if delta >= half:
        return Binary("-", expr, Const(modulus - delta, width))
    return Binary("+", expr, Const(delta, width))


def _small_offsets(width: int) -> list[int]:
    offsets: list[int] = []
    seen: set[int] = set()

    def add(value: int) -> None:
        if value in seen:
            return
        offsets.append(value)
        seen.add(value)

    for value in range(-32, 33):
        add(value)
    for bit in range(min(width, 16)):
        base = 1 << bit
        for delta in (-2, -1, 0, 1, 2):
            add(base + delta)
            add(-(base + delta))
    return offsets


def _offset_subtract_candidates(fields: list[Field], samples: list[Sample],
                                width: int) -> list[Expr]:
    """Fit ``X_ext - Y_ext +/- CONST`` from a sample-derived modular delta."""
    if width <= 1 or len(fields) < 2:
        return []
    out: list[Expr] = []
    for left, right in permutations(fields, 2):
        lhs = _zero_extend(left, width)
        rhs = _zero_extend(right, width)
        base = Binary("-", lhs, rhs)
        deltas: set[int] = set()
        for sample in samples:
            try:
                base_value = base.evaluate(sample.env).unsigned & mask(width)
            except Exception:
                deltas = set()
                break
            deltas.add((sample.target.unsigned - base_value) & mask(width))
            if len(deltas) > 1:
                break
        if len(deltas) != 1:
            continue
        expr = _apply_modular_delta(base, next(iter(deltas)), width)
        if _eval_expr(expr, samples, width):
            out.append(expr)
    return out


def _offset_comparator_candidates(fields: list[Field], samples: list[Sample],
                                  width: int) -> list[Expr]:
    """Fit ``X_ext CMP (Y_ext +/- CONST)`` with a bounded constant scan."""
    if width != 1 or len(fields) < 2:
        return []
    out: list[Expr] = []
    for left, right in permutations(fields, 2):
        cmp_width = max(left.width, right.width) + 1
        lhs = _zero_extend(left, cmp_width)
        rhs_base = _zero_extend(right, cmp_width)
        for offset in _small_offsets(cmp_width):
            rhs = _add_const(rhs_base, offset, cmp_width)
            for op in (">=", ">", "<=", "<"):
                expr = Binary(op, lhs, rhs)
                if _eval_expr(expr, samples, width):
                    out.append(expr)
    return out


def _signed_var(field: Field) -> Expr:
    return Cast(True, Var(field.label, field.width))


def _sign_extend(field: Field, width: int) -> Expr:
    var = Var(field.label, field.width)
    if width <= field.width:
        return Cast(True, var)
    sign = Select(var, field.width - 1)
    extend = width - field.width
    prefix: Expr = sign if extend == 1 else Replicate(extend, sign)
    return Cast(True, Concat((prefix, var)))


def _signed_comparator_candidates(fields: list[Field], samples: list[Sample],
                                  width: int) -> list[Expr]:
    if width != 1:
        return []
    out: list[Expr] = []
    for left, right in permutations(fields, 2):
        for op in ("<", "<=", ">", ">="):
            expr = Binary(op, _signed_var(left), _signed_var(right))
            if _eval_expr(expr, samples, width):
                out.append(expr)
    return out


def _signed_affine_difference_candidates(fields: list[Field],
                                         samples: list[Sample],
                                         width: int) -> list[Expr]:
    if width != 1:
        return []
    wide_fields = [field for field in fields if field.width > 1]
    if len(wide_fields) < 3 or len(wide_fields) > 5:
        return []
    extend_width = max(field.width for field in wide_fields) + 1
    out: list[Expr] = []
    for a, b, c in permutations(wide_fields, 3):
        lhs = Binary("-", _sign_extend(a, extend_width), _sign_extend(b, extend_width))
        rhs = Binary("-", _sign_extend(b, extend_width), _sign_extend(c, extend_width))
        for op in ("<", "<=", ">", ">="):
            expr = Binary(op, lhs, rhs)
            if _eval_expr(expr, samples, width):
                out.append(expr)
    return out


def fitting_candidates(input_fields: list[Field], target_width: int,
                       train_samples: list[Sample],
                       validation_samples: list[Sample],
                       *, max_cost: int | None = None
                       ) -> tuple[list[Candidate], list[str]]:
    """Generate restricted linear/product/comparator template candidates."""
    samples = train_samples + validation_samples
    candidates: list[Candidate] = []
    notes: list[str] = []

    def add(expr: Expr, source: str) -> None:
        if max_cost is not None and expr.cost() > max_cost:
            return
        if _eval_expr(expr, samples, target_width):
            candidates.append(Candidate(expr, source))

    linear = _fit_linear(input_fields, samples, target_width)
    if linear is not None:
        add(linear, "linear-coefficient-fitting")

    vars_ = [Var(field.label, field.width) for field in input_fields]
    product = _fold("*", vars_)
    if product is not None:
        add(product, "multiplication-template-fitting")
    total = _fold("+", vars_)
    if total is not None:
        add(total, "linear-template-fitting")

    for expr in _threshold_candidates(input_fields, samples, target_width):
        add(expr, "comparator-boundary-fitting")
    for expr in _offset_subtract_candidates(input_fields, samples, target_width):
        add(expr, "offset-subtract-template-fitting")
    for expr in _offset_comparator_candidates(input_fields, samples, target_width):
        add(expr, "offset-comparator-template-fitting")
    for expr in _signed_comparator_candidates(input_fields, samples, target_width):
        add(expr, "signed-comparator-template-fitting")
    for expr in _signed_affine_difference_candidates(input_fields, samples, target_width):
        add(expr, "signed-affine-difference-comparator-template-fitting")

    if candidates:
        notes.append(f"template fitting produced {len(candidates)} candidate(s)")
    else:
        notes.append("template fitting found no restricted-form candidate")
    return candidates, notes
