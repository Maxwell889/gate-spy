"""LLM-guided word-level inference helpers for gate-level circuits."""

from .words import Word, collect_words, format_words, io_words, support_words
from .sampling import Sample, simulate_samples
from .hypothesis import (
    HypothesisRecord,
    check_samples,
    optimise_shared_wires,
    render_hypothesis_rtl,
)
from .fitting import fit_basis_candidates, fit_builtin_candidates, fit_custom_template
from .polynomial import (
    PolynomialBudget,
    polynomial_rewrite_estimate,
    polynomial_rewrite_word,
)
from .strategy import format_strategy_report, propose_output_strategy
from .symbolic import SymbolicBudget, symbolic_regression_candidates
from .trace import trace_hypothesis_counterexample

__all__ = [
    "Word",
    "collect_words",
    "format_words",
    "io_words",
    "support_words",
    "Sample",
    "simulate_samples",
    "HypothesisRecord",
    "check_samples",
    "optimise_shared_wires",
    "render_hypothesis_rtl",
    "fit_builtin_candidates",
    "fit_basis_candidates",
    "fit_custom_template",
    "PolynomialBudget",
    "polynomial_rewrite_estimate",
    "polynomial_rewrite_word",
    "format_strategy_report",
    "propose_output_strategy",
    "SymbolicBudget",
    "symbolic_regression_candidates",
    "trace_hypothesis_counterexample",
]
