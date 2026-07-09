"""Cost-bounded native candidate generation over the TABLE-I AST."""

from __future__ import annotations

from dataclasses import dataclass

from .ast import (
    Binary, Conditional, Concat, Const, Expr, PartSelect, Replicate, Select,
    Unary, Var, expr_key,
)
from .resolver import Field


@dataclass(frozen=True)
class Candidate:
    expr: Expr
    source: str = "native"


def _const(value: int, width: int) -> Const:
    return Const(value, max(1, width))


def generate_candidates(inputs: list[Field], target_width: int,
                        *, mode: str = "auto",
                        max_cost: int | None = None) -> list[Candidate]:
    """Generate a deterministic, Verilog-semantics candidate pool."""
    vars_ = [Var(field.label, field.width) for field in inputs]
    target_width = max(1, target_width)
    constants = [
        _const(0, target_width),
        _const(1, target_width),
        _const((1 << target_width) - 1, target_width),
    ]
    out: list[Candidate] = []
    seen: set[str] = set()

    def add(expr: Expr, source: str = "native") -> None:
        try:
            cost = expr.cost()
        except Exception:
            return
        if max_cost is not None and cost > max_cost:
            return
        key = expr_key(expr)
        if key in seen:
            return
        seen.add(key)
        out.append(Candidate(expr, source))

    for expr in vars_ + constants:
        add(expr)

    # Selects and part-selects are first-class because they often recover bus
    # boundaries directly.
    selects: list[Expr] = []
    target_slices: list[Expr] = []
    for var in vars_:
        for i in range(var.width):
            sel = Select(var, i)
            selects.append(sel)
            add(sel, "select")
        if target_width <= var.width:
            for lo in range(0, var.width - target_width + 1):
                ps = PartSelect(var, lo + target_width - 1, lo)
                target_slices.append(ps)
                add(ps, "part-select")

    base = vars_ + target_slices + constants
    bit_exprs = [expr for expr in vars_ + selects + constants if expr.width == 1]
    target_exprs = [expr for expr in base if expr.width == target_width]

    # Structural candidates: concat and replication.
    if len(vars_) >= 2:
        total_width = sum(v.width for v in vars_)
        if total_width == target_width:
            add(Concat(tuple(vars_)), "concat")
            add(Concat(tuple(reversed(vars_))), "concat")
    for a in vars_ + selects:
        if target_width % a.width == 0 and target_width != a.width:
            add(Replicate(target_width // a.width, a), "replication")
    if target_width > 1 and len(bit_exprs) >= target_width:
        add(Concat(tuple(reversed(bit_exprs[:target_width]))), "concat")

    # Unary operators.
    for expr in vars_ + target_slices:
        add(Unary("~", expr), "bitwise-not")
        add(Unary("!", expr), "logical-not")
        add(Unary("&", expr), "reduction")
        add(Unary("|", expr), "reduction")
        add(Unary("^", expr), "reduction")
        add(Unary("~&", expr), "reduction")
        add(Unary("~|", expr), "reduction")
        add(Unary("~^", expr), "reduction")

    # Binary operators over primary expressions.
    arithmetic = ["+", "-", "*", "/", "%", "**"]
    bitwise = ["&", "|", "^", "^~"]
    compare = [">", ">=", "<", "<=", "==", "!=", "===", "!=="]
    logical = ["&&", "||"]

    pair_pool = vars_ + target_slices + constants
    for i, lhs in enumerate(pair_pool):
        for j, rhs in enumerate(pair_pool):
            if i == j and isinstance(lhs, Const):
                continue
            for op in arithmetic:
                if op == "**" and rhs.width > 8:
                    continue
                add(Binary(op, lhs, rhs), "arithmetic")
            for op in bitwise:
                add(Binary(op, lhs, rhs), "bitwise")
            for op in compare:
                add(Binary(op, lhs, rhs), "comparison")

    # Shifts by constants are far more useful than unconstrained variable shifts
    # for netlist recovery and keep the search pool bounded.
    for lhs in vars_ + target_slices:
        for shift in range(0, min(lhs.width, 16)):
            rhs = _const(shift, max(1, lhs.width.bit_length()))
            for op in ("<<", ">>", "<<<", ">>>"):
                add(Binary(op, lhs, rhs), "shift")

    for lhs in bit_exprs:
        for rhs in bit_exprs:
            for op in logical:
                add(Binary(op, lhs, rhs), "logical")

    # N-ary folds for common arithmetic/bitwise reductions.
    if len(vars_) >= 3:
        for op in ("+", "*", "&", "|", "^"):
            expr: Expr = vars_[0]
            for nxt in vars_[1:]:
                expr = Binary(op, expr, nxt)
            add(expr, f"{op}-fold")

    # Conditional muxes: conditions from bit expressions and comparisons,
    # branches from target-width expressions.
    conds: list[Expr] = list(bit_exprs[:16])
    for lhs in vars_[:4]:
        for rhs in vars_[:4]:
            if lhs is not rhs:
                conds.append(Binary("==", lhs, rhs))
                conds.append(Binary("<", lhs, rhs))
    branches = target_exprs[:24]
    for cond in conds[:32]:
        for t in branches:
            for f in branches:
                if t.render() != f.render():
                    add(Conditional(cond, t, f), "conditional")

    if mode == "bit":
        out = [c for c in out if c.expr.width == 1 or target_width == 1]
    return sorted(out, key=lambda c: (c.expr.cost(), c.expr.render()))
