"""Low-bit polynomial rewriting candidates for RTL recovery.

This module intentionally keeps the first implementation bounded.  It derives
Boolean polynomials for target low bits, compares them against a small set of
word-level arithmetic shapes, and returns only candidates whose low-bit
polynomial matches modulo the target width being considered.
"""

from __future__ import annotations

from functools import reduce
from typing import TYPE_CHECKING

import sympy as sp

from ..node import CONST0, CONST1, CONSTX
from ..symbolic.ast import Binary, Const, Expr, Var
from ..symbolic.resolver import Field
from ..symbolic.search import Candidate

if TYPE_CHECKING:
    from ..circuit import Circuit


class PolynomialTooLarge(RuntimeError):
    """Raised when polynomial expansion exceeds the configured cap."""


def _mul_all(items):
    return reduce(lambda a, b: a * b, items, sp.Integer(1))


def _xor_poly(args):
    out = sp.Integer(0)
    for arg in args:
        out = out + arg - 2 * out * arg
    return out


def _reduce_boolean(expr, symbols, max_terms: int):
    expr = sp.expand(expr)
    if not symbols:
        return sp.expand(expr)
    poly = sp.Poly(expr, *symbols)
    terms: dict[tuple[int, ...], sp.Integer] = {}
    for monom, coeff in poly.terms():
        key = tuple(1 if power else 0 for power in monom)
        terms[key] = terms.get(key, sp.Integer(0)) + coeff
        if len(terms) > max_terms:
            raise PolynomialTooLarge(
                f"boolean polynomial exceeded {max_terms} terms")
    out = sp.Integer(0)
    for monom, coeff in terms.items():
        if coeff == 0:
            continue
        term = sp.Integer(coeff)
        for sym, power in zip(symbols, monom):
            if power:
                term *= sym
        out += term
    return sp.expand(out)


def _field_symbols(inputs: list[Field]) -> dict[str, sp.Symbol]:
    syms: dict[str, sp.Symbol] = {}
    for field in inputs:
        for bit in field.bits:
            syms[bit.net] = sp.Symbol(
                bit.net.replace("[", "_").replace("]", ""))
    return syms


def _node_poly(circuit: Circuit, nid: int, symbols: dict[str, sp.Symbol],
               memo: dict[int, sp.Expr], max_terms: int) -> sp.Expr:
    if nid in memo:
        return memo[nid]
    node = circuit.nodes[nid]
    if node.kind == CONST0 or node.kind == CONSTX:
        out = sp.Integer(0)
    elif node.kind == CONST1:
        out = sp.Integer(1)
    elif node.is_pi:
        out = symbols.get(node.net, sp.Integer(0))
    elif node.is_po:
        out = _node_poly(circuit, node.inputs[0], symbols, memo, max_terms)
    else:
        args = [_node_poly(circuit, i, symbols, memo, max_terms)
                for i in node.inputs]
        kind = node.kind.lower()
        if kind == "buf":
            out = args[0]
        elif kind == "not":
            out = 1 - args[0]
        elif kind == "and":
            out = _mul_all(args)
        elif kind == "nand":
            out = 1 - _mul_all(args)
        elif kind == "or":
            out = 1 - _mul_all([1 - a for a in args])
        elif kind == "nor":
            out = _mul_all([1 - a for a in args])
        elif kind == "xor":
            out = _xor_poly(args)
        elif kind == "xnor":
            out = 1 - _xor_poly(args)
        else:
            raise ValueError(f"unsupported primitive for polynomial: {node.kind}")
    out = _reduce_boolean(out, list(symbols.values()), max_terms)
    memo[nid] = out
    return out


def _target_poly(circuit: Circuit, target: Field, symbols: dict[str, sp.Symbol],
                 low_width: int, max_terms: int) -> sp.Expr:
    memo: dict[int, sp.Expr] = {}
    out = sp.Integer(0)
    for bit in sorted(target.bits, key=lambda b: b.index):
        if bit.index >= low_width:
            continue
        out += (1 << bit.index) * _node_poly(
            circuit, bit.node_id, symbols, memo, max_terms)
    return _reduce_boolean(out, list(symbols.values()), max_terms)


def _word_poly(field: Field, symbols: dict[str, sp.Symbol]) -> sp.Expr:
    out = sp.Integer(0)
    for bit in field.bits:
        out += (1 << bit.index) * symbols.get(bit.net, sp.Integer(0))
    return out


def _expr_shapes(inputs: list[Field], target_width: int) -> list[tuple[Expr, sp.Expr]]:
    vars_ = [(Var(field.label, field.width), field) for field in inputs]
    syms = _field_symbols(inputs)
    polys = {field.label: _word_poly(field, syms) for field in inputs}
    out: list[tuple[Expr, sp.Expr]] = []
    for var, field in vars_:
        out.append((var, polys[field.label]))
    for i, (lhs, lf) in enumerate(vars_):
        for j, (rhs, rf) in enumerate(vars_):
            if i == j:
                continue
            lp = polys[lf.label]
            rp = polys[rf.label]
            out.append((Binary("+", lhs, rhs), lp + rp))
            out.append((Binary("-", lhs, rhs), lp - rp))
            out.append((Binary("*", lhs, rhs), lp * rp))
    if len(vars_) >= 3:
        expr: Expr = vars_[0][0]
        poly = polys[vars_[0][1].label]
        for var, field in vars_[1:]:
            expr = Binary("+", expr, var)
            poly += polys[field.label]
        out.append((expr, poly))
    for value in (0, 1, (1 << max(1, target_width)) - 1):
        out.append((Const(value, max(1, target_width)), sp.Integer(value)))
    return out


def _mod_equivalent(delta: sp.Expr, symbols: list[sp.Symbol],
                    modulus: int, max_terms: int) -> bool:
    reduced = _reduce_boolean(delta, symbols, max_terms)
    poly = sp.Poly(reduced, *symbols) if symbols else sp.Poly(reduced)
    for _monom, coeff in poly.terms():
        if int(coeff) % modulus != 0:
            return False
    return True


def polynomial_candidates(circuit: Circuit, input_fields: list[Field],
                          target: Field, *, max_low_bits: int = 10,
                          max_cost: int | None = None,
                          max_terms: int = 1_000,
                          max_support_bits: int = 24,
                          max_cone_nodes: int = 400,
                          ) -> tuple[list[Candidate], list[str]]:
    """Return bounded polynomial-rewriting candidates for *target*."""
    if not input_fields:
        return [], ["polynomial skipped: no support inputs"]
    support_bits = sum(field.width for field in input_fields)
    if support_bits > max_support_bits:
        return [], [
            f"polynomial skipped: support has {support_bits} bit(s), "
            f"cap is {max_support_bits}"
        ]
    cone = circuit.find_cone([str(nid) for nid in target.node_ids], "backward")
    if cone["total_nodes"] > max_cone_nodes:
        return [], [
            f"polynomial skipped: cone has {cone['total_nodes']} node(s), "
            f"cap is {max_cone_nodes}"
        ]
    low_width = min(target.width, max_low_bits)
    symbols = _field_symbols(input_fields)
    try:
        target_poly = _target_poly(
            circuit, target, symbols, low_width, max_terms)
    except PolynomialTooLarge as exc:
        return [], [f"polynomial skipped: {exc}"]
    except Exception as exc:
        return [], [f"polynomial skipped: {exc}"]

    candidates: list[Candidate] = []
    notes: list[str] = []
    modulus = 1 << low_width
    for expr, expr_poly in _expr_shapes(input_fields, target.width):
        try:
            if max_cost is not None and expr.cost() > max_cost:
                continue
            if _mod_equivalent(target_poly - expr_poly,
                               list(symbols.values()), modulus, max_terms):
                source = (
                    "polynomial-rewriting"
                    if low_width == target.width else
                    f"partial-polynomial-low{low_width}"
                )
                candidates.append(Candidate(expr, source))
        except PolynomialTooLarge as exc:
            notes.append(f"polynomial comparison skipped one shape: {exc}")
        except Exception:
            continue
    if not candidates:
        notes.append(
            f"polynomial found no low-{low_width}-bit word-level shape")
    else:
        notes.append(
            f"polynomial matched {len(candidates)} shape(s) on low {low_width} bit(s)")
    return candidates, notes
