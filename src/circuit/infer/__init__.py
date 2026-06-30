"""LLM-guided word-level inference helpers for gate-level circuits."""

from .words import Word, collect_words, format_words, io_words, support_words
from .sampling import Sample, simulate_samples
from .hypothesis import (
    HypothesisRecord,
    check_samples,
    render_hypothesis_rtl,
)
from .fitting import fit_builtin_candidates, fit_custom_template
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
    "render_hypothesis_rtl",
    "fit_builtin_candidates",
    "fit_custom_template",
    "trace_hypothesis_counterexample",
]
