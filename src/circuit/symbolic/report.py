"""Human-readable reports for expression inference."""

from __future__ import annotations

from .resolver import Field
from .verify import CandidateResult


def _field_list(fields: list[Field]) -> str:
    if not fields:
        return "(none)"
    return ", ".join(field.compact() for field in fields)


def format_report(*, source: str | None, target: Field, inputs: list[Field],
                  mode: str, max_cost: int | None,
                  train_count: int, validation_count: int, exhaustive: bool,
                  native_count: int, pysr_status: str,
                  results: list[CandidateResult], detail: bool = False) -> str:
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    lines = [
        f"Expression inference report — {source or '(in-memory circuit)'}",
        f"    target      : {target.compact()}",
        f"    inputs      : {_field_list(inputs)}",
        f"    mode        : {mode}",
        f"    max cost    : {max_cost if max_cost is not None else '(auto)'}",
        f"    samples     : train={train_count}, validation={validation_count}",
        f"    verification: {'exhaustive' if exhaustive else 'sample-verified'}",
        f"    candidates  : native={native_count}; {pysr_status}",
        "",
        "== Candidate ranking ==",
    ]
    if not passed:
        lines.append("  No candidate passed validation.")
    else:
        limit = len(passed) if detail else min(12, len(passed))
        for i, result in enumerate(passed[:limit], 1):
            expr = result.expr
            lines.append(
                f"  {i:>2}. [{result.status}] cost={expr.cost()} "
                f"width={expr.width} source={result.candidate.source}"
            )
            lines.append(f"      {target.label} = {expr.render()}")
        if limit < len(passed):
            lines.append(f"  (+{len(passed) - limit} more passing candidates)")

    lines += ["", "== Failed candidate summary =="]
    if not failed:
        lines.append("  (none)")
    else:
        shown = failed if detail else failed[:8]
        for result in shown:
            lines.append(
                f"  [{result.status}] cost={result.expr.cost()} "
                f"source={result.candidate.source}: {result.expr.render()}"
            )
            if result.reason:
                lines.append(f"      reason: {result.reason}")
        if len(shown) < len(failed):
            lines.append(f"  (+{len(failed) - len(shown)} more failed candidates)")

    lines += [
        "",
        "Cost model: TABLE I Verilog operators. Bitwise binary and bitwise-not "
        "cost scale by result width; reduction/select/arithmetic/shift/"
        "comparison/logical/conditional cost 1; concat/replication cost 1 per "
        "symbol.",
    ]
    if not exhaustive:
        lines.append(
            "NOTE: validation is sample-based because the input support is too "
            "large for exhaustive enumeration; this is not a formal equivalence proof."
        )
    return "\n".join(lines)
