"""Bounded structural expression rewrite from PO bits toward PI bits.

This module intentionally does not try to be a full Verilog optimizer.  It
builds a conservative bit-level expression DAG for one output word, with hard
budgets so an LLM can decide whether structural rewrite is worth attempting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..node import CONST0, CONST1, CONSTX, PI, PO
from .words import Word, split_bit, support_words

if TYPE_CHECKING:
    from ..circuit import Circuit


@dataclass(frozen=True)
class PolynomialBudget:
    max_nodes: int = 2_000
    max_expr_chars: int = 24_000
    max_intermediate_chars: int = 8_000


class RewriteBudgetExceeded(RuntimeError):
    """Raised when bounded rewrite exceeds the configured expression budget."""


def _backward_node_ids(circuit: "Circuit", nets: list[str]) -> set[int]:
    seen: set[int] = set()
    stack: list[int] = []
    for net in nets:
        nid = circuit._node_by_ref(net)  # Internal helper used by other query code.
        if nid is not None:
            stack.append(nid)
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        stack.extend(circuit.nodes[nid].inputs)
    return seen


def polynomial_rewrite_estimate(circuit: "Circuit",
                                output: Word,
                                input_words: dict[str, Word],
                                budget: PolynomialBudget | None = None
                                ) -> dict[str, Any]:
    """Estimate whether structural expression expansion is likely feasible."""
    budget = budget or PolynomialBudget()
    cone_ids = _backward_node_ids(circuit, output.nets_lsb_first)
    gate_ids = [nid for nid in cone_ids if circuit.nodes[nid].is_gate]
    support = support_words(circuit, output, input_words)
    hist: dict[str, int] = {}
    fanout_reconv = 0
    for nid in gate_ids:
        node = circuit.nodes[nid]
        hist[node.kind] = hist.get(node.kind, 0) + 1
        fanout_reconv += max(
            0,
            sum(1 for fo in node.fanouts if fo in cone_ids) - 1,
        )

    nonlinear = sum(hist.get(kind, 0) for kind in ("and", "nand", "or", "nor"))
    xor_like = sum(hist.get(kind, 0) for kind in ("xor", "xnor"))
    support_bits = sum(word.width for word in support)
    growth_score = (
        len(gate_ids)
        + 2 * nonlinear
        + xor_like
        + 3 * fanout_reconv
        + support_bits * max(1, output.width)
    )
    estimated_chars = max(1, output.width) * (24 + growth_score)
    estimated_seconds = round(estimated_chars / 50_000, 3)
    feasible = (
        len(cone_ids) <= budget.max_nodes
        and estimated_chars <= budget.max_expr_chars
    )

    reasons: list[str] = []
    if len(cone_ids) > budget.max_nodes:
        reasons.append(
            f"cone node count {len(cone_ids)} exceeds max_nodes={budget.max_nodes}"
        )
    if estimated_chars > budget.max_expr_chars:
        reasons.append(
            f"estimated expression chars {estimated_chars} exceeds "
            f"max_expr_chars={budget.max_expr_chars}"
        )
    if fanout_reconv:
        reasons.append(
            f"{fanout_reconv} reconvergent fanout point(s) may duplicate terms"
        )

    return {
        "output": output.name,
        "width": output.width,
        "support_words": [word.name for word in support],
        "support_bits": support_bits,
        "cone_nodes": len(cone_ids),
        "gate_count": len(gate_ids),
        "gate_histogram": dict(sorted(hist.items())),
        "reconvergence": fanout_reconv,
        "growth_score": growth_score,
        "estimated_chars": estimated_chars,
        "estimated_seconds": estimated_seconds,
        "feasible": feasible,
        "reasons": reasons,
    }


def _net_expr(net: str) -> str:
    base, idx = split_bit(net)
    if idx is None:
        return base
    return f"{base}[{idx}]"


def _join_bitwise(op: str, args: list[str]) -> str:
    if not args:
        return "1'b0"
    if len(args) == 1:
        return args[0]
    return "(" + f" {op} ".join(f"({arg})" for arg in args) + ")"


def _gate_expr(kind: str, args: list[str]) -> str:
    kind = kind.lower()
    if kind == "buf":
        return args[0] if args else "1'b0"
    if kind == "not":
        return f"!({args[0]})"
    if kind == "and":
        return _join_bitwise("&", args)
    if kind == "nand":
        return f"!({_join_bitwise('&', args)})"
    if kind == "or":
        return _join_bitwise("|", args)
    if kind == "nor":
        return f"!({_join_bitwise('|', args)})"
    if kind == "xor":
        return _join_bitwise("^", args)
    if kind == "xnor":
        return f"!({_join_bitwise('^', args)})"
    raise ValueError(f"unsupported gate kind for polynomial rewrite: {kind}")


def polynomial_rewrite_word(circuit: "Circuit",
                            output: Word,
                            budget: PolynomialBudget | None = None
                            ) -> dict[str, Any]:
    """Rewrite one output word as a PI-only bit-expression sum.

    The returned expression is Verilog-compatible and also evaluable by the
    local hypothesis checker.  Multi-bit words are rendered as a weighted sum of
    bit expressions instead of concat because the sample evaluator intentionally
    keeps its expression grammar small.
    """
    budget = budget or PolynomialBudget()
    memo: dict[int, str] = {}
    visiting: set[int] = set()
    visited_count = 0

    def expr_for(nid: int) -> str:
        nonlocal visited_count
        if nid in memo:
            return memo[nid]
        if nid in visiting:
            raise RewriteBudgetExceeded("cycle detected while expanding expression")
        if visited_count >= budget.max_nodes:
            raise RewriteBudgetExceeded(
                f"expanded more than max_nodes={budget.max_nodes}"
            )
        visited_count += 1
        visiting.add(nid)
        node = circuit.nodes[nid]
        if node.kind == PI:
            expr = _net_expr(node.net)
        elif node.kind == CONST0 or node.kind == CONSTX:
            expr = "1'b0"
        elif node.kind == CONST1:
            expr = "1'b1"
        elif node.kind == PO:
            expr = expr_for(node.inputs[0]) if node.inputs else "1'b0"
        else:
            args = [expr_for(src) for src in node.inputs]
            expr = _gate_expr(node.kind, args)

        visiting.remove(nid)
        if len(expr) > budget.max_intermediate_chars:
            raise RewriteBudgetExceeded(
                f"intermediate expression at node {nid} has {len(expr)} chars"
            )
        memo[nid] = expr
        return expr

    bit_terms: list[str] = []
    bit_exprs: list[tuple[int, str]] = []
    try:
        for idx, net in output.bits:
            nid = circuit._node_by_ref(net)
            if nid is None:
                raise ValueError(f"cannot resolve output net {net!r}")
            bit_expr = expr_for(nid)
            bit_exprs.append((idx, bit_expr))
            if bit_expr == "1'b0":
                continue
            if idx == 0:
                bit_terms.append(f"({bit_expr})")
            elif bit_expr == "1'b1":
                bit_terms.append(str(1 << idx))
            else:
                bit_terms.append(f"(({bit_expr}) << {idx})")
    except RewriteBudgetExceeded as exc:
        return {
            "status": "budget_exceeded",
            "output": output.name,
            "message": str(exc),
            "expanded_nodes": visited_count,
            "memoized_nodes": len(memo),
        }
    except Exception as exc:
        return {
            "status": "error",
            "output": output.name,
            "message": str(exc),
            "expanded_nodes": visited_count,
            "memoized_nodes": len(memo),
        }

    expr = " + ".join(bit_terms) if bit_terms else "0"
    if len(expr) > budget.max_expr_chars:
        return {
            "status": "budget_exceeded",
            "output": output.name,
            "message": (
                f"final expression has {len(expr)} chars; "
                f"max_expr_chars={budget.max_expr_chars}"
            ),
            "expanded_nodes": visited_count,
            "memoized_nodes": len(memo),
            "expr_chars": len(expr),
        }
    return {
        "status": "fit",
        "output": output.name,
        "expr": expr,
        "expr_chars": len(expr),
        "expanded_nodes": visited_count,
        "memoized_nodes": len(memo),
        "bit_exprs": bit_exprs,
    }
