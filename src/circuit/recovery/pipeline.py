"""High-level RTL recovery orchestration."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..symbolic.ast import Conditional, Var
from ..symbolic.pysr_backend import pysr_candidates
from ..symbolic.resolver import build_net_to_node, resolve_field, resolve_inputs
from ..symbolic.sampler import (
    EXHAUSTIVE_MAX_BITS, directed_random_patterns, exhaustive_patterns,
    support_bit_count,
)
from ..symbolic.search import Candidate, generate_candidates
from ..symbolic.verify import (
    CandidateResult, Sample, simulate_samples, verify_candidates,
)
from .fit import fitting_candidates
from .models import ExpressionCandidate, RecoveryReport, VerificationResult
from .polynomial import polynomial_candidates
from .word import analyze_words

if TYPE_CHECKING:
    from ..circuit import Circuit
    from ..symbolic.resolver import Field
    from .models import WordAnalysis


VerifyFunc = Callable[[str, int], VerificationResult]


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


def _samples(circuit: Circuit, input_fields: list[Field], target: Field,
             pattern_num: int, validation_num: int,
             seed: int | None) -> tuple[list[Sample], list[Sample], bool]:
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
    return (
        simulate_samples(circuit, input_fields, target, train_patterns),
        simulate_samples(circuit, input_fields, target, validation_patterns),
        exhaustive,
    )


def _symbolic_candidates(input_fields: list[Field], target: Field,
                         train_samples: list[Sample], *,
                         mode: str, max_cost: int,
                         timeout_s: int, niterations: int
                         ) -> tuple[list[Candidate], list[str]]:
    native = generate_candidates(
        input_fields, target.width, mode=mode, max_cost=max_cost)
    notes = [f"native generator produced {len(native)} candidate(s)"]
    pysr: list[Candidate] = []
    if mode == "bit":
        notes.append("PySR skipped in bit mode")
    else:
        pysr, status = pysr_candidates(
            input_fields, target, train_samples,
            niterations=niterations,
            timeout_s=timeout_s,
            maxsize=32,
        )
        pysr = [cand for cand in pysr if cand.expr.cost() <= max_cost]
        notes.append(status)
    return native + pysr, notes


def _passed_branch_candidates(candidates: list[Candidate],
                              branch_train: list[Sample],
                              branch_validation: list[Sample],
                              exhaustive: bool,
                              limit: int = 4) -> list[CandidateResult]:
    if not branch_train or not branch_validation:
        return []
    results = verify_candidates(
        _dedupe(candidates), branch_train, branch_validation,
        exhaustive=exhaustive,
    )
    return [r for r in results if r.passed][:limit]


def _control_candidates(input_fields: list[Field], target: Field,
                        train_samples: list[Sample],
                        validation_samples: list[Sample],
                        exhaustive: bool,
                        max_cost: int) -> tuple[list[Candidate], list[str]]:
    controls = [field for field in input_fields if field.width == 1][:4]
    if not controls:
        return [], ["control enumeration skipped: no scalar support input"]
    all_samples = train_samples + validation_samples
    out: list[Candidate] = []
    notes: list[str] = []
    for control in controls:
        rest = [field for field in input_fields if field.label != control.label]
        if not rest:
            continue
        branch_pool = generate_candidates(
            rest, target.width, mode="mixed", max_cost=max_cost)
        fit_pool, _ = fitting_candidates(
            rest, target.width, train_samples, validation_samples,
            max_cost=max_cost)
        pool = _dedupe(branch_pool + fit_pool)
        true_train = [s for s in train_samples
                      if s.env[control.label].unsigned == 1]
        false_train = [s for s in train_samples
                       if s.env[control.label].unsigned == 0]
        true_val = [s for s in validation_samples
                    if s.env[control.label].unsigned == 1]
        false_val = [s for s in validation_samples
                     if s.env[control.label].unsigned == 0]
        t_results = _passed_branch_candidates(
            pool, true_train, true_val, exhaustive)
        f_results = _passed_branch_candidates(
            pool, false_train, false_val, exhaustive)
        for t in t_results:
            for f in f_results:
                expr = Conditional(
                    Var(control.label, 1), t.expr, f.expr)
                if expr.cost() <= max_cost:
                    out.append(Candidate(expr, "control-enumeration"))
        if t_results and f_results:
            notes.append(
                f"control {control.label}: combined "
                f"{len(t_results)} true-branch and {len(f_results)} false-branch candidate(s)")
    if not out:
        notes.append("control enumeration produced no candidate")
    return out, notes


def _to_expression_candidates(results: list[CandidateResult], target: Field,
                              inputs: list[Field],
                              notes: list[str],
                              *, detail: bool) -> tuple[ExpressionCandidate, ...]:
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    shown = passed if detail else passed[:12]
    out: list[ExpressionCandidate] = []
    for result in shown:
        expr = result.expr
        out.append(ExpressionCandidate(
            target=target.label,
            expression=expr.render(),
            method=result.candidate.source,
            cost=expr.cost(),
            width=expr.width,
            inputs=tuple(field.compact() for field in inputs),
            verification=VerificationResult(result.status, result.reason),
            evidence=tuple(notes),
            expr_obj=expr,
        ))
    if detail:
        for result in failed[:12]:
            expr = result.expr
            out.append(ExpressionCandidate(
                target=target.label,
                expression=expr.render(),
                method=result.candidate.source,
                cost=expr.cost(),
                width=expr.width,
                inputs=tuple(field.compact() for field in inputs),
                verification=VerificationResult(result.status, result.reason),
                evidence=tuple(notes),
                expr_obj=expr,
            ))
    return tuple(out)


def recover_expression_data(circuit: Circuit, target_ref: str,
                            inputs: list[str] | None = None,
                            *, mode: str = "auto",
                            methods: str = "auto",
                            control: bool = True,
                            max_cost: int | None = None,
                            pattern_num: int = 4096,
                            validation_num: int = 16384,
                            seed: int | None = 0,
                            timeout_s: int = 120,
                            niterations: int = 200,
                            detail: bool = False,
                            word_analysis: WordAnalysis | None = None
                            ) -> RecoveryReport:
    """Recover one target expression and return structured data."""
    if mode not in {"auto", "word", "bit", "mixed"}:
        raise ValueError("mode must be one of: auto, word, bit, mixed")
    method_set = (
        {"symbolic", "polynomial", "fit", "control"}
        if methods == "auto" else
        {m.strip() for m in methods.split(",") if m.strip()}
    )
    net_to_node = build_net_to_node(circuit)
    target = resolve_field(circuit, target_ref, net_to_node, role="target")
    input_fields = resolve_inputs(circuit, inputs, target, net_to_node)
    train_samples, validation_samples, exhaustive = _samples(
        circuit, input_fields, target, pattern_num, validation_num, seed)
    effective_max_cost = max_cost if max_cost is not None else max(
        64, target.width * 2 + 8)

    notes: list[str] = []
    candidates: list[Candidate] = []
    if "symbolic" in method_set:
        produced, produced_notes = _symbolic_candidates(
            input_fields, target, train_samples, mode=mode,
            max_cost=effective_max_cost, timeout_s=timeout_s,
            niterations=niterations)
        candidates.extend(produced)
        notes.extend(produced_notes)
    if "polynomial" in method_set:
        produced, produced_notes = polynomial_candidates(
            circuit, input_fields, target,
            max_low_bits=min(10, target.width),
            max_cost=effective_max_cost)
        candidates.extend(produced)
        notes.extend(produced_notes)
    if "fit" in method_set:
        produced, produced_notes = fitting_candidates(
            input_fields, target.width, train_samples, validation_samples,
            max_cost=effective_max_cost)
        candidates.extend(produced)
        notes.extend(produced_notes)
    if control and "control" in method_set:
        produced, produced_notes = _control_candidates(
            input_fields, target, train_samples, validation_samples,
            exhaustive, effective_max_cost)
        candidates.extend(produced)
        notes.extend(produced_notes)

    results = verify_candidates(
        _dedupe(candidates), train_samples, validation_samples,
        exhaustive=exhaustive,
    )
    analysis = word_analysis or analyze_words(circuit, outputs=[target_ref])
    expression_candidates = _to_expression_candidates(
        results, target, input_fields, notes, detail=detail)
    if not expression_candidates:
        notes.append("no candidate passed validation")
    return RecoveryReport(
        circuit_name=circuit.name,
        word_analysis=analysis,
        candidates=expression_candidates,
        notes=tuple(notes),
    )


def _port_words(circuit: Circuit, direction: str):
    analysis = analyze_words(circuit)
    return analysis.input_words if direction == "input" else analysis.output_words


def _module_ports(circuit: Circuit) -> list[str]:
    original = list(getattr(circuit, "module_ports", []) or [])
    if original:
        return original
    names: list[str] = []
    for word in list(_port_words(circuit, "input")) + list(_port_words(circuit, "output")):
        if word.name not in names:
            names.append(word.name)
    return names


def _decl(word) -> str:
    if word.width <= 1:
        return f"  {word.direction} {word.name};"
    return f"  {word.direction} [{word.width - 1}:0] {word.name};"


def emit_candidate_rtl(circuit: Circuit,
                       chosen: dict[str, ExpressionCandidate]) -> str:
    """Build a simple candidate RTL module from recovered output expressions."""
    name = circuit.name or "top"
    ports = ", ".join(_module_ports(circuit))
    lines = [f"module {name}({ports});"]
    for word in _port_words(circuit, "input"):
        lines.append(_decl(word))
    for word in _port_words(circuit, "output"):
        lines.append(_decl(word))
    lines.append("")
    for word in _port_words(circuit, "output"):
        cand = chosen[word.name]
        lines.append(f"  assign {word.name} = {cand.expression};")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def recover_rtl_data(circuit: Circuit, outputs: list[str] | None = None,
                     *, source_code: str = "",
                     verify_func: VerifyFunc | None = None,
                     timeout_s: int = 300,
                     emit_unverified: bool = False,
                     pattern_num: int = 4096,
                     validation_num: int = 16384,
                     seed: int | None = 0,
                     niterations: int = 200,
                     detail: bool = False) -> RecoveryReport:
    """Recover every selected output word and optionally verify whole RTL."""
    analysis = analyze_words(circuit, outputs=outputs)
    target_words = analysis.output_words
    all_candidates: list[ExpressionCandidate] = []
    chosen: dict[str, ExpressionCandidate] = {}
    unrecovered: list[str] = []
    for word in target_words:
        report = recover_expression_data(
            circuit, word.name, None, mode="auto", methods="auto",
            control=True, pattern_num=pattern_num,
            validation_num=validation_num, seed=seed,
            timeout_s=min(timeout_s, 120), niterations=niterations,
            detail=detail, word_analysis=analysis)
        all_candidates.extend(report.candidates)
        accepted = [c for c in report.candidates if c.verification.accepted]
        if accepted:
            chosen[word.name] = sorted(
                accepted,
                key=lambda c: (0 if c.verification.status.endswith("exact") else 1,
                               c.cost, c.expression),
            )[0]
        else:
            unrecovered.append(word.name)

    candidate_rtl = ""
    verification = VerificationResult(
        "not-run", "candidate RTL not emitted because some outputs were unrecovered")
    if not unrecovered or emit_unverified:
        if unrecovered and emit_unverified:
            verification = VerificationResult(
                "not-run",
                "candidate RTL omitted because structural fallback emission is not available")
        else:
            candidate_rtl = emit_candidate_rtl(circuit, chosen)
            if source_code and verify_func is not None:
                verification = verify_func(candidate_rtl, timeout_s)
            elif source_code:
                verification = VerificationResult(
                    "not-run", "no CEC verifier callback was provided")
            else:
                verification = VerificationResult(
                    "not-run", "no original Verilog source is available for CEC")

    return RecoveryReport(
        circuit_name=circuit.name,
        word_analysis=analysis,
        candidates=tuple(all_candidates),
        candidate_rtl=candidate_rtl,
        verification=verification,
        unrecovered=tuple(unrecovered),
        notes=(
            "recover_rtl is a final assembly/sanity-check tool, not the primary analysis workflow.",
        ),
    )
