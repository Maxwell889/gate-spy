"""Candidate verification against circuit simulation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .bitvec import BitVec, mask
from .resolver import Field
from .search import Candidate

if TYPE_CHECKING:
    from ..circuit import Circuit


@dataclass(frozen=True)
class Sample:
    pattern: dict[str, int]
    env: dict[str, BitVec]
    target: BitVec


@dataclass(frozen=True)
class CandidateResult:
    candidate: Candidate
    passed: bool
    status: str
    reason: str = ""
    mismatches: int = 0

    @property
    def expr(self):
        return self.candidate.expr


def simulate_samples(circuit: Circuit, input_fields: list[Field], target: Field,
                     patterns: list[dict[str, int]]) -> list[Sample]:
    samples: list[Sample] = []
    for pattern in patterns:
        values = circuit.simulate_values(pattern)
        env = {
            field.label: BitVec(field.value_from_nodes(values), field.width)
            for field in input_fields
        }
        target_value = BitVec(target.value_from_nodes(values), target.width)
        samples.append(Sample(pattern, env, target_value))
    return samples


def _check_samples(candidate: Candidate, samples: list[Sample]) -> tuple[str, int, str]:
    exact = True
    masked = True
    mismatches = 0
    first_reason = ""
    target_mask = mask(samples[0].target.width) if samples else 0
    for sample in samples:
        try:
            value = candidate.expr.evaluate(sample.env)
        except Exception as exc:  # pragma: no cover - exact exception is not important.
            return "error", len(samples), str(exc)
        target = sample.target.unsigned
        if not (value.width == sample.target.width and value.unsigned == target):
            exact = False
        if (value.unsigned & target_mask) != target:
            masked = False
            mismatches += 1
            if not first_reason:
                env_desc = ", ".join(
                    f"{name}={vec.unsigned}" for name, vec in sample.env.items())
                width_hint = ""
                if value.width < sample.target.width:
                    expr_low_mask = mask(value.width)
                    if (value.unsigned & expr_low_mask) == (target & expr_low_mask):
                        width_hint = (
                            f"; low {value.width} bit(s) match target, but "
                            f"expr_width={value.width} < target_width={sample.target.width}; "
                            "try explicit zero/sign extension or a wider literal")
                first_reason = (
                    f"inputs {{{env_desc}}} give expr={value.unsigned} "
                    f"(width={value.width}) target={target} "
                    f"(width={sample.target.width}){width_hint}")
    if exact:
        return "exact", 0, ""
    if masked:
        return "masked", 0, ""
    return "failed", mismatches, first_reason


def verify_candidates(candidates: list[Candidate],
                      train_samples: list[Sample],
                      validation_samples: list[Sample],
                      *, exhaustive: bool) -> list[CandidateResult]:
    """Filter and rank candidates by training and validation behaviour."""
    results: list[CandidateResult] = []
    prefix = "exhaustive" if exhaustive else "sample"
    for candidate in candidates:
        train_kind, train_mismatches, train_reason = _check_samples(candidate, train_samples)
        if train_kind in ("error", "failed"):
            results.append(CandidateResult(
                candidate, False, "failed-training",
                train_reason, train_mismatches))
            continue
        val_kind, val_mismatches, val_reason = _check_samples(candidate, validation_samples)
        if val_kind in ("exact", "masked"):
            results.append(CandidateResult(
                candidate, True, f"{prefix}-{val_kind}", "", 0))
        else:
            results.append(CandidateResult(
                candidate, False, "failed-validation",
                val_reason, val_mismatches))
    return sorted(
        results,
        key=lambda r: (
            0 if r.passed else 1,
            0 if r.status.endswith("exact") else 1,
            r.expr.cost(),
            r.expr.render(),
        ),
    )
