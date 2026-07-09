"""Small, agent-facing recovery tools.

Each entrypoint intentionally performs one bounded action.  The goal is to make
the agent inspect evidence and revise its plan instead of delegating a whole
cluster to an opaque multi-method search.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING

from ..symbolic.pysr_backend import pysr_candidates
from ..symbolic.parser import expression_identifiers, parse_expression
from ..symbolic.resolver import Field, build_net_to_node, resolve_field, resolve_inputs
from ..symbolic.sampler import (
    EXHAUSTIVE_MAX_BITS,
    directed_random_patterns,
    exhaustive_patterns,
    support_bit_count,
)
from ..symbolic.search import Candidate, generate_candidates
from ..symbolic.verify import (
    CandidateResult,
    Sample,
    simulate_samples,
    verify_candidates,
)
from .fit import fitting_candidates
from .models import ExpressionCandidate, RecoveryReport
from .pipeline import _dedupe, _samples, _to_expression_candidates
from .polynomial import polynomial_candidates
from .tool_profiles import get_tool_profile
from .word import analyze_words

if TYPE_CHECKING:
    from ..circuit import Circuit

_EXHAUSTIVE_CANDIDATE_SAMPLE_CAP = 2_000_000
_EXHAUSTIVE_SURVIVOR_LIMIT = 128


def _resolve_context(circuit: Circuit, target_ref: str,
                     inputs: list[str] | None) -> tuple[Field, list[Field]]:
    net_to_node = build_net_to_node(circuit)
    target = resolve_field(circuit, target_ref, net_to_node, role="target")
    input_fields = resolve_inputs(circuit, inputs, target, net_to_node)
    return target, input_fields


def _default_max_cost(target: Field, max_cost: int | None) -> int:
    return max_cost if max_cost is not None else max(64, target.width * 2 + 8)


def _format_field_list(fields: list[Field]) -> str:
    return ", ".join(field.compact() for field in fields) if fields else "(none)"


def _input_base(ref: str) -> str:
    return ref.split("[", 1)[0]


def _merge_fields(*field_groups: list[Field]) -> list[Field]:
    out: list[Field] = []
    seen: set[str] = set()
    for fields in field_groups:
        for field in fields:
            if field.label in seen:
                continue
            out.append(field)
            seen.add(field.label)
    return out


def _input_refs_for_expression(expression: str,
                               inputs: list[str] | None,
                               fixed_inputs: dict[str, int] | None) -> list[str]:
    refs = list(inputs or [])
    seen = set(refs)
    for name in expression_identifiers(expression):
        base = _input_base(name)
        if base not in seen:
            refs.append(base)
            seen.add(base)
    for key in fixed_inputs or {}:
        base = _input_base(key)
        if base not in seen:
            refs.append(base)
            seen.add(base)
    return refs


def _conditioned_bases(fixed_inputs: dict[str, int] | None) -> set[str]:
    return {_input_base(key) for key in fixed_inputs or {}}


def _fixed_refs(fixed_inputs: dict[str, int] | None) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for key in fixed_inputs or {}:
        base = _input_base(key)
        if base not in seen:
            refs.append(base)
            seen.add(base)
    return refs


def _unfixed_scalar_support(support_fields: list[Field],
                            fixed_inputs: dict[str, int] | None) -> list[Field]:
    fixed_bases = _conditioned_bases(fixed_inputs)
    return [
        field for field in support_fields
        if field.width == 1 and _input_base(field.label) not in fixed_bases
    ]


def _partial_control_notes(support_fields: list[Field],
                           fixed_inputs: dict[str, int] | None) -> list[str]:
    """Warn when a branch-like call leaves scalar support inputs variable."""
    if not fixed_inputs:
        return []
    remaining = _unfixed_scalar_support(support_fields, fixed_inputs)
    if not remaining:
        return []
    remaining_desc = _format_field_list(remaining)
    fixed_keys = ", ".join(sorted(str(key) for key in fixed_inputs))
    return [
        (
            "diagnostic=partial_control_assignment_suspected: "
            f"fixed_inputs covers {{{fixed_keys}}} but scalar support still varies: "
            f"{remaining_desc}"
        ),
        (
            "if those scalar inputs are controls, this is not a complete branch; "
            "rerun split_control_cases and validate/fit every full control assignment "
            "before escalating to native search or PySR"
        ),
        (
            "if the remaining scalar inputs are intentional data bits, confirm that "
            "with directed simulate or influence_profile instead of inferring it from "
            "marginal sample rows"
        ),
    ]


def _resolve_validation_context(
    circuit: Circuit,
    target_ref: str,
    expression: str,
    inputs: list[str] | None,
    fixed_inputs: dict[str, int] | None,
    validate_scope: str,
) -> tuple[Field, list[Field], list[Field], list[Field], list[Field]]:
    if validate_scope not in {"support", "expr_only"}:
        raise ValueError("validate_scope must be one of: support, expr_only")

    net_to_node = build_net_to_node(circuit)
    target = resolve_field(circuit, target_ref, net_to_node, role="target")
    support_fields = resolve_inputs(circuit, None, target, net_to_node)
    expression_refs = _input_refs_for_expression(expression, inputs, fixed_inputs)
    expression_fields = resolve_inputs(circuit, expression_refs, target, net_to_node)

    if validate_scope == "support":
        input_fields = _merge_fields(support_fields, expression_fields)
    else:
        input_fields = expression_fields

    active_bases = {
        _input_base(name) for name in expression_identifiers(expression)
    } | _conditioned_bases(fixed_inputs)
    omitted_support = [
        field for field in support_fields
        if _input_base(field.label) not in active_bases
    ]
    return target, input_fields, support_fields, expression_fields, omitted_support


def _fixed_field_bits(input_fields: list[Field],
                      fixed_inputs: dict[str, int] | None) -> dict[str, int]:
    if not fixed_inputs:
        return {}
    fields_by_label = {field.label: field for field in input_fields}
    net_to_field = {
        bit.net: (field, bit.index)
        for field in input_fields
        for bit in field.bits
    }
    fixed_bits: dict[str, int] = {}
    for key, raw in fixed_inputs.items():
        value = int(raw)
        if key in fields_by_label:
            field = fields_by_label[key]
            hi = (1 << field.width) - 1
            if not (0 <= value <= hi):
                raise ValueError(
                    f"fixed input {key!r}={value} exceeds {field.width}-bit range 0..{hi}")
            for bit in field.bits:
                fixed_bits[bit.net] = (value >> bit.index) & 1
            continue
        if key in net_to_field:
            if value not in (0, 1):
                raise ValueError(f"fixed bit {key!r} must be 0 or 1")
            fixed_bits[key] = value
            continue
        raise ValueError(
            f"fixed input {key!r} is not in validation inputs; "
            f"available inputs: {', '.join(fields_by_label) or '(none)'}")
    return fixed_bits


def _merge_fixed(patterns: list[dict[str, int]],
                 fixed_bits: dict[str, int]) -> list[dict[str, int]]:
    if not fixed_bits:
        return patterns
    return [{**pattern, **fixed_bits} for pattern in patterns]


def _samples_with_fixed(circuit: Circuit, input_fields: list[Field], target: Field,
                        pattern_num: int, validation_num: int,
                        seed: int | None, exhaustive: str,
                        fixed_inputs: dict[str, int] | None
                        ) -> tuple[list[Sample], list[Sample], bool, int]:
    fixed_bits = _fixed_field_bits(input_fields, fixed_inputs)
    variable_fields = [
        field for field in input_fields
        if not all(bit.net in fixed_bits for bit in field.bits)
    ]
    variable_bit_count = support_bit_count(variable_fields)
    use_exhaustive = exhaustive != "never" and variable_bit_count <= EXHAUSTIVE_MAX_BITS
    if exhaustive == "always" and not use_exhaustive:
        raise ValueError(
            f"variable support has {variable_bit_count} bit(s); "
            f"exhaustive limit is {EXHAUSTIVE_MAX_BITS}")
    if use_exhaustive:
        patterns = _merge_fixed(exhaustive_patterns(variable_fields), fixed_bits)
        train_patterns = patterns
        validation_patterns = patterns
    else:
        train_patterns = _merge_fixed(
            directed_random_patterns(variable_fields, pattern_num, seed),
            fixed_bits)
        validation_patterns = _merge_fixed(
            directed_random_patterns(
                variable_fields, validation_num,
                None if seed is None else seed + 1),
            fixed_bits)
    return (
        simulate_samples(circuit, input_fields, target, train_patterns),
        simulate_samples(circuit, input_fields, target, validation_patterns),
        use_exhaustive,
        variable_bit_count,
    )


def _sample_rows(samples: list[Sample], input_fields: list[Field],
                 limit: int = 8) -> list[str]:
    rows: list[str] = []
    for idx, sample in enumerate(samples[:limit]):
        ins = ", ".join(
            f"{field.label}={sample.env[field.label].unsigned}"
            for field in input_fields
        )
        rows.append(f"      {idx}: {ins} -> target={sample.target.unsigned}")
    return rows


def _slice_evidence_results(results: list[CandidateResult]) -> list[CandidateResult]:
    out: list[CandidateResult] = []
    for result in results:
        if result.passed:
            out.append(CandidateResult(
                result.candidate,
                True,
                f"slice-{result.status}",
                (
                    "expression-only slice evidence; omitted support inputs "
                    "were not sampled and defaulted to 0"
                ),
                result.mismatches,
            ))
        else:
            out.append(result)
    return out


def probe_target_data(circuit: Circuit, target_ref: str,
                      inputs: list[str] | None = None,
                      *, pattern_num: int = 32,
                      seed: int | None = 0,
                      detail: bool = False) -> str:
    """Profile one target without generating candidate expressions."""
    target, input_fields = _resolve_context(circuit, target_ref, inputs)
    profile = get_tool_profile("probe_target")
    patterns = directed_random_patterns(input_fields, pattern_num, seed)
    samples = simulate_samples(circuit, input_fields, target, patterns)
    cone = circuit.find_cone([str(nid) for nid in target.node_ids], "backward")
    support_bits = support_bit_count(input_fields)
    target_values = [sample.target.unsigned for sample in samples]
    unique_values = sorted(set(target_values))
    value_preview = ", ".join(str(v) for v in unique_values[:16])
    if len(unique_values) > 16:
        value_preview += f", ... (+{len(unique_values) - 16} more)"

    scalar_controls = [field for field in input_fields if field.width == 1]
    wide_inputs = [field for field in input_fields if field.width > 1]
    lines = [
        f"Target probe — {target.label}",
        f"    effort       : {profile.effort}",
        f"    search space : {profile.search_space}",
        f"    risk         : {profile.risk}",
        f"    width        : {target.width}",
        f"    inputs       : {_format_field_list(input_fields)}",
        f"    support bits : {support_bits}",
        f"    cone nodes   : {cone['total_nodes']}",
        f"    samples      : {len(samples)} seed={seed}",
        f"    output values: {len(unique_values)} unique"
        + (f" ({value_preview})" if value_preview else ""),
        "",
        "== Sample profile ==",
    ]
    if target.width == 1:
        ones = sum(1 for value in target_values if value)
        zeros = len(target_values) - ones
        lines.append(f"  scalar distribution: zeros={zeros} ones={ones}")
    else:
        changed = len(unique_values) > 1
        lines.append(
            "  word activity: "
            + ("varies across directed samples" if changed else "constant in sampled patterns")
        )
    if detail:
        lines.extend(_sample_rows(samples, input_fields))
    lines.append("")
    lines.append("== Next-step suggestions ==")
    if target.width == 1:
        lines.append("  - Try fit_template with comparator templates for this predicate.")
    if scalar_controls and wide_inputs:
        controls = ", ".join(field.label for field in scalar_controls)
        lines.append(
            f"  - Scalar controls present ({controls}); run split_control_cases before fitting one formula.")
    if support_bits <= EXHAUSTIVE_MAX_BITS:
        lines.append("  - Support is small enough for exhaustive validation; if you have a formula, use validate_expr before search.")
        lines.append("  - If no formula hypothesis exists, try fit_template before infer_native_expr.")
    elif cone["total_nodes"] > 400:
        lines.append("  - Cone is large; inspect find_cone(detail=True) or narrow to a bit/part-select before search.")
    else:
        lines.append("  - Use a light local attempt next; avoid PySR unless native/template fails on a narrowed target.")
    return "\n".join(lines)


def _candidate_report(circuit: Circuit, target_ref: str,
                      inputs: list[str] | None,
                      candidates: list[Candidate],
                      notes: list[str],
                      *, pattern_num: int,
                      validation_num: int,
                      seed: int | None,
                      detail: bool) -> RecoveryReport:
    target, input_fields = _resolve_context(circuit, target_ref, inputs)
    unique_candidates = _dedupe(candidates)
    support_bits = support_bit_count(input_fields)
    exhaustive_patterns_count = (
        1 << support_bits if support_bits <= EXHAUSTIVE_MAX_BITS else 0
    )
    work_estimate = len(unique_candidates) * exhaustive_patterns_count
    if (exhaustive_patterns_count
            and work_estimate > _EXHAUSTIVE_CANDIDATE_SAMPLE_CAP):
        notes.append(
            "native exhaustive verification pruned: "
            f"{len(unique_candidates)} candidate(s) x {exhaustive_patterns_count} "
            f"pattern(s) exceeds cap {_EXHAUSTIVE_CANDIDATE_SAMPLE_CAP}")
        notes.append(
            "using directed samples to filter the candidate pool, then exhaustive "
            "validation only for surviving candidates")
        train_patterns = directed_random_patterns(input_fields, pattern_num, seed)
        train_samples = simulate_samples(circuit, input_fields, target, train_patterns)
        preliminary = verify_candidates(
            unique_candidates, train_samples, train_samples, exhaustive=False)
        survivors = [
            result.candidate for result in preliminary if result.passed
        ][:_EXHAUSTIVE_SURVIVOR_LIMIT]
        notes.append(
            f"directed prefilter survivors={len(survivors)} "
            f"limit={_EXHAUSTIVE_SURVIVOR_LIMIT}")
        if survivors:
            patterns = exhaustive_patterns(input_fields)
            exhaustive_samples = simulate_samples(
                circuit, input_fields, target, patterns)
            results = verify_candidates(
                survivors, train_samples, exhaustive_samples, exhaustive=True)
        else:
            results = preliminary
    else:
        train_samples, validation_samples, exhaustive = _samples(
            circuit, input_fields, target, pattern_num, validation_num, seed)
        results = verify_candidates(
            unique_candidates, train_samples, validation_samples,
            exhaustive=exhaustive,
        )
    expression_candidates = _to_expression_candidates(
        results, target, input_fields, notes, detail=detail)
    if not expression_candidates:
        notes.append("no candidate passed validation")
    return RecoveryReport(
        circuit_name=circuit.name,
        word_analysis=analyze_words(circuit, outputs=[target_ref]),
        candidates=expression_candidates,
        notes=tuple(notes),
    )


def fit_template_data(circuit: Circuit, target_ref: str,
                      inputs: list[str] | None = None,
                      *, templates: str = "linear,product,comparator",
                      pattern_num: int = 512,
                      validation_num: int = 2048,
                      seed: int | None = 0,
                      exhaustive: str = "auto",
                      fixed_inputs: dict[str, int] | None = None,
                      detail: bool = False) -> RecoveryReport:
    """Run restricted template fitting only; never invokes PySR."""
    if exhaustive not in {"auto", "always", "never"}:
        raise ValueError("exhaustive must be one of: auto, always, never")
    net_to_node = build_net_to_node(circuit)
    target = resolve_field(circuit, target_ref, net_to_node, role="target")
    support_fields = resolve_inputs(circuit, None, target, net_to_node)
    fixed_fields = (
        resolve_inputs(circuit, _fixed_refs(fixed_inputs), target, net_to_node)
        if fixed_inputs else []
    )
    candidate_fields = resolve_inputs(circuit, inputs, target, net_to_node)
    fixed_bases = _conditioned_bases(fixed_inputs)
    if inputs is None and fixed_bases:
        candidate_fields = [
            field for field in candidate_fields
            if _input_base(field.label) not in fixed_bases
        ]
    sample_fields = _merge_fields(support_fields, candidate_fields, fixed_fields)
    train_samples, validation_samples, is_exhaustive, variable_bit_count = (
        _samples_with_fixed(
            circuit, sample_fields, target, pattern_num, validation_num,
            seed, exhaustive, fixed_inputs)
    )
    candidates, notes = fitting_candidates(
        candidate_fields, target.width, train_samples, validation_samples,
        max_cost=_default_max_cost(target, None),
    )
    profile = get_tool_profile("fit_template")
    fixed_desc = ", ".join(
        f"{key}={value}" for key, value in sorted((fixed_inputs or {}).items()))
    bit_count = support_bit_count(sample_fields)
    notes.insert(
        0,
        f"tool effort={profile.effort} search_space={profile.search_space} risk={profile.risk}")
    notes.insert(1, f"restricted templates requested: {templates}")
    notes.insert(
        2,
        "comparator fitting includes signed comparators, signed affine-difference comparators, "
        "and bounded offset forms X_ext - Y_ext +/- CONST / X_ext CMP (Y_ext +/- CONST)")
    notes.append(f"sample_scope=full-support sample_bits={bit_count} variable_bits={variable_bit_count}")
    notes.append(f"support_inputs={_format_field_list(support_fields)}")
    notes.append(f"template_inputs={_format_field_list(candidate_fields)}")
    if fixed_desc:
        notes.append(f"branch condition fixed_inputs: {fixed_desc}")
        notes.append(
            "branch-conditioned template candidate is evidence only; combine control cases before assemble_rtl")
        notes.extend(_partial_control_notes(support_fields, fixed_inputs))
    results = verify_candidates(
        _dedupe(candidates), train_samples, validation_samples,
        exhaustive=is_exhaustive,
    )
    expression_candidates = _to_expression_candidates(
        results, target, sample_fields, notes, detail=detail)
    if fixed_desc:
        for cand in expression_candidates:
            cand.case_condition = fixed_desc
    if not expression_candidates:
        notes.append("no candidate passed validation")
    return RecoveryReport(
        circuit_name=circuit.name,
        word_analysis=analyze_words(circuit, outputs=[target_ref]),
        candidates=expression_candidates,
        notes=tuple(notes),
    )


def validate_expr_data(circuit: Circuit, target_ref: str,
                       expression: str,
                       inputs: list[str] | None = None,
                       *, pattern_num: int = 512,
                       validation_num: int = 2048,
                       seed: int | None = 0,
                       exhaustive: str = "auto",
                       fixed_inputs: dict[str, int] | None = None,
                       validate_scope: str = "support",
                       detail: bool = False) -> RecoveryReport:
    """Validate an explicit expression hypothesis and cacheable candidate."""
    if exhaustive not in {"auto", "always", "never"}:
        raise ValueError("exhaustive must be one of: auto, always, never")
    target, input_fields, support_fields, expression_fields, omitted_support = (
        _resolve_validation_context(
            circuit, target_ref, expression, inputs, fixed_inputs, validate_scope)
    )
    bit_count = support_bit_count(input_fields)
    expr = parse_expression(
        expression,
        input_widths={field.label: field.width for field in input_fields},
        target_width=target.width,
    )
    train_samples, validation_samples, is_exhaustive, variable_bit_count = _samples_with_fixed(
        circuit, input_fields, target, pattern_num, validation_num,
        seed, exhaustive, fixed_inputs)
    candidate = Candidate(expr, "manual-hypothesis")
    fixed_desc = ", ".join(
        f"{key}={value}" for key, value in sorted((fixed_inputs or {}).items()))
    scope_desc = "full-support" if validate_scope == "support" else "expr-only-slice"
    omitted_desc = _format_field_list(omitted_support)
    notes = [
        "tool effort=verify search_space=none risk=low",
        "validated explicit expression hypothesis; no search was run",
        f"tool effort=verify search_space=none sample_bits={bit_count} variable_bits={variable_bit_count}",
        f"sample_scope={scope_desc}",
        f"support_inputs={_format_field_list(support_fields)}",
        f"expression_inputs={_format_field_list(expression_fields)}",
        f"omitted_support={omitted_desc}",
    ]
    if fixed_desc:
        notes.append(f"branch condition fixed_inputs: {fixed_desc}")
        notes.extend(_partial_control_notes(support_fields, fixed_inputs))
    if validate_scope == "support":
        notes.append(
            "full-support validation samples every non-fixed primary input in the target cone, "
            "including support omitted by the expression")
    else:
        notes.append(
            "expr_only slice validation samples only expression/fixed inputs; omitted support "
            "inputs are not driven and therefore default to 0 in circuit simulation")
        notes.append(
            "slice evidence is hypothesis-only: it is not cached as C#/B# and assemble_rtl cannot consume it")
    if inputs is None:
        notes.append(
            "expression inputs inferred from expression variables and fixed_inputs; "
            "full-support validation still samples the target cone support")
    results = verify_candidates(
        [candidate], train_samples, validation_samples,
        exhaustive=is_exhaustive,
    )
    if validate_scope == "expr_only":
        results = _slice_evidence_results(results)
    expression_candidates = _to_expression_candidates(
        results, target, input_fields, notes, detail=detail)
    if fixed_desc:
        for cand in expression_candidates:
            cand.case_condition = fixed_desc
    accepted = [cand for cand in expression_candidates if cand.verification.accepted]
    if not expression_candidates:
        notes.append("explicit expression failed validation")
    elif fixed_desc and accepted:
        notes.append(
            "branch-conditioned candidate is evidence only; combine control cases before assemble_rtl")
    return RecoveryReport(
        circuit_name=circuit.name,
        word_analysis=analyze_words(circuit, outputs=[target_ref]),
        candidates=expression_candidates,
        notes=tuple(notes),
    )


def _signed_delta(delta: int, width: int) -> int:
    modulus = 1 << width
    half = 1 << (width - 1)
    delta %= modulus
    return delta - modulus if delta >= half else delta


def _pattern_from_field_values(input_fields: list[Field],
                               values: dict[str, int]) -> dict[str, int]:
    pattern: dict[str, int] = {}
    for field in input_fields:
        value = int(values.get(field.label, 0))
        for bit in field.bits:
            pattern[bit.net] = (value >> bit.index) & 1
    return pattern


def _field_value_from_pattern(field: Field, pattern: dict[str, int]) -> int:
    value = 0
    for bit in field.bits:
        value |= int(pattern.get(bit.net, 0)) << bit.index
    return value


def _profile_refs(inputs: list[str] | None,
                  fixed_inputs: dict[str, int] | None,
                  base_inputs: dict[str, int] | None) -> list[str] | None:
    if inputs is None:
        return None
    refs = list(inputs)
    seen = {_input_base(ref) for ref in refs}
    for mapping in (fixed_inputs or {}, base_inputs or {}):
        for key in mapping:
            base = _input_base(key)
            if base not in seen:
                refs.append(base)
                seen.add(base)
    return refs


def influence_profile_data(circuit: Circuit, target_ref: str,
                           fixed_inputs: dict[str, int] | None = None,
                           inputs: list[str] | None = None,
                           *,
                           base_inputs: dict[str, int] | None = None,
                           pattern_num: int = 2,
                           seed: int | None = 0,
                           detail: bool = False) -> str:
    """Perturb support bits under an optional fixed branch and report deltas."""
    if pattern_num < 1:
        raise ValueError(f"pattern_num must be >= 1, got {pattern_num}")
    target, input_fields = _resolve_context(
        circuit, target_ref, _profile_refs(inputs, fixed_inputs, base_inputs))
    fixed_bits = _fixed_field_bits(input_fields, fixed_inputs)
    if base_inputs:
        base_bits = _fixed_field_bits(input_fields, base_inputs)
        base_patterns = [_merge_fixed(
            [_pattern_from_field_values(input_fields, {})],
            {**base_bits, **fixed_bits},
        )[0]]
        base_desc = ", ".join(
            f"{key}={value}" for key, value in sorted(base_inputs.items()))
    else:
        variable_fields = [
            field for field in input_fields
            if not all(bit.net in fixed_bits for bit in field.bits)
        ]
        base_patterns = _merge_fixed(
            directed_random_patterns(variable_fields, pattern_num, seed),
            fixed_bits,
        )
        base_desc = "directed-zero/random base pattern(s)"

    modulus = 1 << target.width
    fixed_desc = ", ".join(
        f"{key}={value}" for key, value in sorted((fixed_inputs or {}).items()))
    bit_effects: dict[str, list[tuple[int, int, int]]] = {}
    word_effects: dict[str, list[tuple[int, int, int, int]]] = {}
    fixed_bit_nets = set(fixed_bits)

    for base_index, base_pattern in enumerate(base_patterns):
        base_values = circuit.simulate_values(base_pattern)
        base_target = target.value_from_nodes(base_values)
        for field in input_fields:
            free_bits = [bit for bit in field.bits if bit.net not in fixed_bit_nets]
            if not free_bits:
                continue
            for bit in free_bits:
                perturbed = dict(base_pattern)
                before_bit = int(perturbed.get(bit.net, 0))
                perturbed[bit.net] = 0 if before_bit else 1
                values = circuit.simulate_values(perturbed)
                target_value = target.value_from_nodes(values)
                delta = (target_value - base_target) % modulus
                label = bit.net
                bit_effects.setdefault(label, []).append(
                    (base_index, delta, _signed_delta(delta, target.width)))
            if field.width > 1:
                current = _field_value_from_pattern(field, base_pattern)
                next_value = ((1 << field.width) - 1) if current == 0 else 0
                perturbed = dict(base_pattern)
                for bit in free_bits:
                    perturbed[bit.net] = (next_value >> bit.index) & 1
                values = circuit.simulate_values(perturbed)
                target_value = target.value_from_nodes(values)
                delta = (target_value - base_target) % modulus
                word_effects.setdefault(field.label, []).append(
                    (base_index, current, next_value,
                     _signed_delta(delta, target.width)))

    nonzero_bits = {
        label: effects for label, effects in bit_effects.items()
        if any(delta != 0 for _base, delta, _signed in effects)
    }
    zero_bits = sorted(
        label for label, effects in bit_effects.items()
        if all(delta == 0 for _base, delta, _signed in effects)
    )
    nonzero_words = {
        label: effects for label, effects in word_effects.items()
        if any(signed != 0 for _base, _before, _after, signed in effects)
    }
    zero_words = sorted(
        label for label, effects in word_effects.items()
        if all(signed == 0 for _base, _before, _after, signed in effects)
    )
    lines = [
        f"Influence profile — {target.label}",
        "    effort       : verify",
        "    search space : directed perturbation, no expression search",
        "    risk         : low",
        f"    inputs       : {_format_field_list(input_fields)}",
        f"    fixed_inputs : {fixed_desc or '(none)'}",
        f"    base_inputs  : {base_desc}",
        f"    base samples : {len(base_patterns)} seed={seed}",
        "",
        "== Nonzero bit deltas ==",
    ]
    if not nonzero_bits:
        lines.append("  (none)")
    else:
        for label in sorted(nonzero_bits):
            effects = nonzero_bits[label]
            signed_values = sorted({signed for _base, _delta, signed in effects})
            unsigned_values = sorted({delta for _base, delta, _signed in effects})
            signed_preview = ", ".join(str(v) for v in signed_values[:8])
            unsigned_preview = ", ".join(str(v) for v in unsigned_values[:8])
            if len(signed_values) > 8:
                signed_preview += f", ... (+{len(signed_values) - 8} more)"
            if len(unsigned_values) > 8:
                unsigned_preview += f", ... (+{len(unsigned_values) - 8} more)"
            lines.append(
                f"  {label}: signed_delta={{{signed_preview}}} "
                f"unsigned_delta={{{unsigned_preview}}}")
            if detail:
                for base_index, delta, signed in effects[:8]:
                    lines.append(
                        f"      base#{base_index}: delta_unsigned={delta} "
                        f"delta_signed={signed}")
    lines.append("")
    lines.append("== Nonzero word deltas ==")
    if not nonzero_words:
        lines.append("  (none)")
    else:
        for label in sorted(nonzero_words):
            effects = nonzero_words[label]
            signed_values = sorted({signed for _base, _before, _after, signed in effects})
            signed_preview = ", ".join(str(v) for v in signed_values[:8])
            if len(signed_values) > 8:
                signed_preview += f", ... (+{len(signed_values) - 8} more)"
            lines.append(f"  {label}: signed_delta={{{signed_preview}}}")
            if detail:
                for base_index, before, after, signed in effects[:8]:
                    lines.append(
                        f"      base#{base_index}: {before}->{after} "
                        f"delta_signed={signed}")
    lines.append("")
    lines.append("== Zero influence ==")
    lines.append("  bits : " + (", ".join(zero_bits) if zero_bits else "(none)"))
    lines.append("  words: " + (", ".join(zero_words) if zero_words else "(none)"))
    lines.append("")
    lines.append("== Interpretation ==")
    lines.append(
        "  Nonzero deltas identify support bits that still affect the target under "
        "the fixed branch; do not omit them from full-support validation.")
    lines.append(
        "  Zero influence here is branch/base-specific evidence, not a global proof; "
        "single-base profiles can miss multiplicative or gated interactions when "
        "the paired input is 0.")
    if base_inputs is None and pattern_num < 2:
        lines.append(
            "  For interaction-heavy branches, rerun with pattern_num>=2 or explicit "
            "nonzero base_inputs before concluding an input is irrelevant.")
    return "\n".join(lines)


def infer_native_expr_data(circuit: Circuit, target_ref: str,
                           inputs: list[str] | None = None,
                           *, mode: str = "auto",
                           max_cost: int | None = None,
                           pattern_num: int = 512,
                           validation_num: int = 2048,
                           seed: int | None = 0,
                           detail: bool = False) -> RecoveryReport:
    """Run GateSpy native TABLE-I AST generation only; never invokes PySR."""
    if mode not in {"auto", "word", "bit", "mixed"}:
        raise ValueError("mode must be one of: auto, word, bit, mixed")
    target, input_fields = _resolve_context(circuit, target_ref, inputs)
    candidates = generate_candidates(
        input_fields, target.width, mode=mode,
        max_cost=_default_max_cost(target, max_cost))
    profile = get_tool_profile("infer_native_expr")
    notes = [
        f"tool effort={profile.effort} search_space={profile.search_space} risk={profile.risk}",
        "if an explicit formula hypothesis exists, validate_expr is cheaper than native search",
        f"native TABLE-I generator produced {len(candidates)} candidate(s)",
    ]
    return _candidate_report(
        circuit, target_ref, inputs, candidates, notes,
        pattern_num=pattern_num, validation_num=validation_num,
        seed=seed, detail=detail)


def check_polynomial_lowbits_data(circuit: Circuit, target_ref: str,
                                  inputs: list[str] | None = None,
                                  *, max_low_bits: int = 8,
                                  detail: bool = False) -> RecoveryReport:
    """Run bounded low-bit polynomial rewriting only."""
    target, input_fields = _resolve_context(circuit, target_ref, inputs)
    candidates, notes = polynomial_candidates(
        circuit, input_fields, target,
        max_low_bits=max_low_bits,
        max_cost=_default_max_cost(target, None),
    )
    profile = get_tool_profile("check_polynomial_lowbits")
    notes.insert(0, f"tool effort={profile.effort} search_space={profile.search_space} risk={profile.risk}")
    validation_num = 2048
    pattern_num = 512
    return _candidate_report(
        circuit, target_ref, inputs, candidates, notes,
        pattern_num=pattern_num, validation_num=validation_num,
        seed=0, detail=detail)


def split_control_cases_data(circuit: Circuit, target_ref: str,
                             controls: list[str] | None = None,
                             inputs: list[str] | None = None,
                             *, pattern_num: int = 128,
                             seed: int | None = 0,
                             detail: bool = False) -> str:
    """Inspect scalar control cases without composing branch expressions."""
    target, input_fields = _resolve_context(circuit, target_ref, inputs)
    profile = get_tool_profile("split_control_cases")
    net_to_node = build_net_to_node(circuit)
    if controls is None:
        control_fields = [field for field in input_fields if field.width == 1]
    else:
        control_fields = [
            resolve_field(circuit, control, net_to_node, role="input")
            for control in controls
        ]
        labels = {field.label for field in input_fields}
        input_fields = input_fields + [
            field for field in control_fields if field.label not in labels
        ]
    control_fields = [field for field in control_fields if field.width == 1]
    control_labels = {field.label for field in control_fields}
    data_fields = [field for field in input_fields if field.label not in control_labels]
    patterns: list[dict[str, int]] = []
    expected_per_assignment = pattern_num
    if control_fields:
        data_bit_count = support_bit_count(data_fields)
        if data_bit_count <= EXHAUSTIVE_MAX_BITS:
            expected_per_assignment = min(pattern_num, 1 << data_bit_count)
        labels = [field.label for field in control_fields]
        for assignment_index, values in enumerate(
            itertools.product((0, 1), repeat=len(control_fields))
        ):
            fixed_values = dict(zip(labels, values))
            fixed_pattern = _pattern_from_field_values(control_fields, fixed_values)
            branch_seed = None if seed is None else seed + assignment_index
            data_patterns = directed_random_patterns(
                data_fields, pattern_num, branch_seed)
            patterns.extend({**pattern, **fixed_pattern} for pattern in data_patterns)
    else:
        patterns = directed_random_patterns(input_fields, pattern_num, seed)
    samples = simulate_samples(circuit, input_fields, target, patterns)
    lines = [
        f"Control split probe — {target.label}",
        f"    effort       : {profile.effort}",
        f"    search space : {profile.search_space}",
        f"    risk         : {profile.risk}",
        f"    inputs       : {_format_field_list(input_fields)}",
        f"    controls     : {_format_field_list(control_fields)}",
        f"    samples      : {len(samples)} seed={seed}",
        f"    assignment coverage: balanced full-control assignments"
        if control_fields else
        "    assignment coverage: no scalar controls selected",
        "",
        "== Marginal case profile ==",
        "  These rows vary other controls freely; do not treat them as full branch truth tables.",
    ]
    if not control_fields:
        lines.append("  No scalar controls found in the selected support.")
        lines.append("")
        lines.append("== Next-step suggestions ==")
        lines.append("  - Re-run probe_target or pass explicit controls if the cone has internal control signals.")
        return "\n".join(lines)

    for control in control_fields:
        for value in (0, 1):
            branch = [
                sample for sample in samples
                if sample.env[control.label].unsigned == value
            ]
            unique_targets = sorted({sample.target.unsigned for sample in branch})
            preview = ", ".join(str(v) for v in unique_targets[:12])
            if len(unique_targets) > 12:
                preview += f", ... (+{len(unique_targets) - 12} more)"
            lines.append(
                f"  {control.label}={value}: samples={len(branch)} "
                f"unique_targets={len(unique_targets)}"
                + (f" ({preview})" if preview else "")
            )
            if detail:
                rest = [field for field in input_fields if field.label != control.label]
                lines.extend(_sample_rows(branch, rest, limit=6))
    if len(control_fields) > 1:
        lines.append("")
        lines.append("== Control combination profile ==")
        lines.append(
            "  Use these full assignments for branch hypotheses; validate each "
            "with fixed_inputs before combining cases.")
        labels = [field.label for field in control_fields]
        for values in itertools.product((0, 1), repeat=len(control_fields)):
            fixed = dict(zip(labels, values))
            branch = [
                sample for sample in samples
                if all(sample.env[label].unsigned == value
                       for label, value in fixed.items())
            ]
            unique_targets = sorted({sample.target.unsigned for sample in branch})
            preview = ", ".join(str(v) for v in unique_targets[:12])
            if len(unique_targets) > 12:
                preview += f", ... (+{len(unique_targets) - 12} more)"
            fixed_desc = ", ".join(f"{k}={v}" for k, v in fixed.items())
            lines.append(
                f"  {fixed_desc}: samples={len(branch)} "
                f"unique_targets={len(unique_targets)}"
                + (f" ({preview})" if preview else ""))
            if len(branch) < expected_per_assignment:
                lines.append(
                    f"      low coverage: expected at least {expected_per_assignment} "
                    "pattern(s) for this full assignment")
            if detail:
                rest = [
                    field for field in input_fields
                    if field.label not in set(labels)
                ]
                lines.extend(_sample_rows(branch, rest, limit=4))
    elif control_fields:
        lines.append("")
        lines.append("== Control combination profile ==")
        control = control_fields[0]
        for value in (0, 1):
            branch = [
                sample for sample in samples
                if sample.env[control.label].unsigned == value
            ]
            if len(branch) < expected_per_assignment:
                lines.append(
                    f"  {control.label}={value}: low coverage "
                    f"({len(branch)}/{expected_per_assignment})")
    lines.append("")
    lines.append("== Branch next steps ==")
    if len(control_fields) == 1:
        for control in control_fields:
            rest = [field.label for field in input_fields if field.label != control.label]
            lines.append(
                f"  - For {control.label}=0/1, probe branch-specific behavior with "
                f"fixed {control.label}; then fit_template or infer_native_expr using inputs={rest}.")
    else:
        rest = [
            field.label for field in input_fields
            if field.label not in {control.label for control in control_fields}
        ]
        controls_desc = ", ".join(field.label for field in control_fields)
        lines.append(
            f"  - For controls {{{controls_desc}}}, validate hypotheses under full "
            "control assignments, e.g. fixed_inputs includes every listed control.")
        lines.append(
            f"  - Use inputs={rest} plus the fixed controls for each branch; "
            "do not infer a branch from one marginal control row.")
    return "\n".join(lines)


def infer_pysr_expr_data(circuit: Circuit, target_ref: str,
                         inputs: list[str] | None = None,
                         *, timeout_s: int = 30,
                         niterations: int = 50,
                         detail: bool = False) -> RecoveryReport:
    """Run PySR candidate generation only, then GateSpy bit-vector validation."""
    target, input_fields = _resolve_context(circuit, target_ref, inputs)
    train_samples, _validation_samples, _ = _samples(
        circuit, input_fields, target, 512, 2048, 0)
    candidates, status = pysr_candidates(
        input_fields, target, train_samples,
        niterations=niterations,
        timeout_s=timeout_s,
        maxsize=32,
    )
    notes = [
        "tool effort=heavy search_space=external symbolic-regression search risk=high",
        "PySR forced explicitly; use only after probe/template/native failed on a narrowed target.",
        status,
    ]
    return _candidate_report(
        circuit, target_ref, inputs, candidates, notes,
        pattern_num=512, validation_num=2048,
        seed=0, detail=detail)
