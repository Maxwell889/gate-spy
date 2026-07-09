"""Cost-aware Verilog expression discovery."""

from .ast import (
    Binary, Cast, Conditional, Concat, Const, PartSelect, Replicate, Select, Unary, Var,
)
from .bitvec import BitVec
from .engine import infer_expression
from .resolver import Field, resolve_field, resolve_inputs
from .search import Candidate, generate_candidates
from .verify import CandidateResult, verify_candidates

__all__ = [
    "Binary", "BitVec", "Candidate", "CandidateResult", "Cast", "Concat",
    "Conditional", "Const", "Field", "PartSelect", "Replicate", "Select",
    "Unary", "Var", "generate_candidates", "infer_expression",
    "resolve_field", "resolve_inputs", "verify_candidates",
]
