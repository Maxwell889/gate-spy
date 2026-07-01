"""Strategy ranking for the LLM-guided recovery workbench."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .polynomial import PolynomialBudget, polynomial_rewrite_estimate
from .words import Word, cone_gate_histogram, format_words, support_words

if TYPE_CHECKING:
    from ..circuit import Circuit


def _method_status(name: str, rank: int, status: str, reason: str,
                   call: str) -> dict[str, Any]:
    return {
        "method": name,
        "rank": rank,
        "status": status,
        "reason": reason,
        "call": call,
    }


def propose_output_strategy(circuit: "Circuit",
                            output: Word,
                            input_words: dict[str, Word],
                            poly_budget: PolynomialBudget | None = None
                            ) -> dict[str, Any]:
    """Rank the three recovery lanes for one output word."""
    support = support_words(circuit, output, input_words)
    hist = cone_gate_histogram(circuit, output)
    gate_count = sum(hist.values())
    support_bits = sum(word.width for word in support)
    poly = polynomial_rewrite_estimate(
        circuit, output, input_words, budget=poly_budget)

    methods: list[dict[str, Any]] = []

    template_reason = (
        "cheap first-pass lane; uses cone support, samples, built-in templates, "
        "and LLM-supplied basis/template fallback"
    )
    if len(support) > 12:
        template_reason += "; support is wide, so prefer grouped basis terms"
    methods.append(_method_status(
        "template",
        1,
        "recommended",
        template_reason,
        f'run_method(output="{output.name}", method="template")',
    ))

    if poly["feasible"]:
        poly_status = "recommended"
        poly_reason = (
            f"bounded structural rewrite looks feasible "
            f"({poly['gate_count']} gates, est {poly['estimated_chars']} chars)"
        )
    else:
        poly_status = "defer"
        poly_reason = "; ".join(poly["reasons"]) or "estimated rewrite is too large"
    methods.append(_method_status(
        "polynomial",
        2,
        poly_status,
        poly_reason,
        f'run_method(output="{output.name}", method="polynomial")',
    ))

    if len(support) <= 8 and support_bits <= 48:
        symbolic_status = "fallback"
        symbolic_reason = (
            "sample-driven grammar search is within input budget; use after "
            "template or polynomial rewrite misses"
        )
    else:
        symbolic_status = "defer"
        symbolic_reason = (
            f"support has {len(support)} words/{support_bits} bits; construct "
            "a smaller basis or cone cut before symbolic regression"
        )
    methods.append(_method_status(
        "symbolic",
        3,
        symbolic_status,
        symbolic_reason,
        f'run_method(output="{output.name}", method="symbolic")',
    ))

    cex_advice = (
        "If a candidate fails, call trace_counterexample on the hypothesis. "
        "Use the mismatch bit and subterm values to decide whether to add a "
        "missing basis term, adjust selector logic, or switch lanes."
    )

    return {
        "output": output.name,
        "width": output.width,
        "support": format_words(support),
        "support_word_count": len(support),
        "support_bits": support_bits,
        "cone_gate_count": gate_count,
        "cone_histogram": hist,
        "polynomial_estimate": poly,
        "methods": methods,
        "cex_advice": cex_advice,
    }


def format_strategy_report(strategies: list[dict[str, Any]],
                           detail: bool = False) -> str:
    lines = ["Three-lane recovery strategy"]
    for item in strategies:
        lines.extend([
            "",
            f"== {item['output']}[{item['width']}] ==",
            f"support : {item['support']}",
            f"cone    : {item['cone_gate_count']} gates "
            + ", ".join(f"{k}:{v}" for k, v in item["cone_histogram"].items()),
        ])
        poly = item["polynomial_estimate"]
        lines.append(
            "poly_estimate : "
            f"feasible={poly['feasible']}, chars={poly['estimated_chars']}, "
            f"seconds~{poly['estimated_seconds']}, reconv={poly['reconvergence']}"
        )
        if detail and poly.get("reasons"):
            for reason in poly["reasons"]:
                lines.append(f"  poly_reason: {reason}")
        lines.append("methods:")
        for method in item["methods"]:
            lines.append(
                f"  {method['rank']}. {method['method']} "
                f"[{method['status']}] — {method['reason']}"
            )
            if detail:
                lines.append(f"     call: {method['call']}")
        if detail:
            lines.append("cex_advice : " + item["cex_advice"])
    return "\n".join(lines)
