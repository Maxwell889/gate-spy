"""High-level expression inference orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .pysr_backend import pysr_candidates
from .report import format_report
from .resolver import build_net_to_node, resolve_field, resolve_inputs
from .sampler import (
    EXHAUSTIVE_MAX_BITS, directed_random_patterns, exhaustive_patterns,
    support_bit_count,
)
from .search import Candidate, generate_candidates
from .verify import simulate_samples, verify_candidates

if TYPE_CHECKING:
    from ..circuit import Circuit


def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for cand in candidates:
        key = cand.expr.render()
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


def infer_expression(circuit: Circuit, *, source: str | None,
                     targets: list[str] | str,
                     inputs: list[str] | None = None,
                     mode: str = "auto",
                     max_cost: int | None = None,
                     pattern_num: int = 4096,
                     validation_num: int = 16384,
                     seed: int | None = 0,
                     timeout_s: int = 120,
                     niterations: int = 200,
                     maxsize: int = 32,
                     detail: bool = False) -> str:
    """Infer Verilog-compatible expressions for one or more targets."""
    if mode not in {"auto", "word", "bit", "mixed"}:
        raise ValueError("mode must be one of: auto, word, bit, mixed")
    if pattern_num < 1 or validation_num < 1:
        raise ValueError("pattern_num and validation_num must both be >= 1")

    target_refs = [targets] if isinstance(targets, str) else list(targets)
    if not target_refs:
        raise ValueError("targets must not be empty")

    reports: list[str] = []
    net_to_node = build_net_to_node(circuit)
    for target_ref in target_refs:
        target = resolve_field(circuit, target_ref, net_to_node, role="target")
        input_fields = resolve_inputs(circuit, inputs, target, net_to_node)
        bit_count = support_bit_count(input_fields)
        exhaustive = bit_count <= EXHAUSTIVE_MAX_BITS
        if exhaustive:
            patterns = exhaustive_patterns(input_fields)
            train_patterns = patterns
            validation_patterns = patterns
        else:
            train_patterns = directed_random_patterns(input_fields, pattern_num, seed)
            validation_patterns = directed_random_patterns(
                input_fields, validation_num,
                None if seed is None else seed + 1,
            )

        train_samples = simulate_samples(circuit, input_fields, target, train_patterns)
        validation_samples = simulate_samples(
            circuit, input_fields, target, validation_patterns)

        effective_max_cost = (
            max_cost if max_cost is not None
            else max(64, target.width * 2 + 8)
        )
        native = generate_candidates(
            input_fields, target.width, mode=mode, max_cost=effective_max_cost)
        pysr: list[Candidate] = []
        pysr_status = "PySR skipped in bit mode"
        if mode != "bit":
            pysr, pysr_status = pysr_candidates(
                input_fields, target, train_samples,
                niterations=niterations,
                timeout_s=timeout_s,
                maxsize=maxsize,
            )
            pysr = [
                cand for cand in pysr
                if cand.expr.cost() <= effective_max_cost
            ]

        candidates = _dedupe(native + pysr)
        results = verify_candidates(
            candidates, train_samples, validation_samples, exhaustive=exhaustive)
        reports.append(format_report(
            source=source,
            target=target,
            inputs=input_fields,
            mode=mode,
            max_cost=effective_max_cost,
            train_count=len(train_samples),
            validation_count=len(validation_samples),
            exhaustive=exhaustive,
            native_count=len(native),
            pysr_status=pysr_status,
            results=results,
            detail=detail,
        ))

    if len(reports) == 1:
        return reports[0]
    return "\n\n" + ("\n\n".join(
        f"### Target {i + 1}/{len(reports)}\n{report}"
        for i, report in enumerate(reports)
    ))
