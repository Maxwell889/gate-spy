"""Built-in primitive gate logic definitions, replacing Liberty library.

Only supports 8 basic primitive gates: not, buf, and, nand, or, nor, xor, xnor.
"""

from __future__ import annotations

from typing import Callable

# Logic function type: accepts any number of 0/1 integers, returns 0/1
LogicFn = Callable[..., int]


# ---------------------------------------------------------------------------
# Basic primitive gate logic (2-input)
# ---------------------------------------------------------------------------

def _not(a: int) -> int:
    return 1 - a


def _buf(a: int) -> int:
    return a


def _and(a: int, b: int) -> int:
    return a & b


def _nand(a: int, b: int) -> int:
    return 1 - (a & b)


def _or(a: int, b: int) -> int:
    return a | b


def _nor(a: int, b: int) -> int:
    return 1 - (a | b)


def _xor(a: int, b: int) -> int:
    return a ^ b


def _xnor(a: int, b: int) -> int:
    return 1 - (a ^ b)


# ---------------------------------------------------------------------------
# Multi-input gate extensions
# ---------------------------------------------------------------------------

def _and_n(*args: int) -> int:
    """N-input AND: output 1 when all inputs are 1"""
    result = 1
    for a in args:
        result &= a
    return result


def _nand_n(*args: int) -> int:
    """N-input NAND: negation of AND"""
    return 1 - _and_n(*args)


def _or_n(*args: int) -> int:
    """N-input OR: output 1 when any input is 1"""
    result = 0
    for a in args:
        result |= a
    return result


def _nor_n(*args: int) -> int:
    """N-input NOR: negation of OR"""
    return 1 - _or_n(*args)


def _xor_n(*args: int) -> int:
    """N-input XOR: output 1 when odd number of inputs are 1"""
    result = 0
    for a in args:
        result ^= a
    return result


def _xnor_n(*args: int) -> int:
    """N-input XNOR: negation of XOR"""
    return 1 - _xor_n(*args)


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

# Input count range for each gate type (min, max), None means no upper limit
PRIMITIVE_ARITY = {
    "not": (1, 1),
    "buf": (1, 1),
    "and": (2, None),
    "nand": (2, None),
    "or": (2, None),
    "nor": (2, None),
    "xor": (2, None),
    "xnor": (2, None),
}

# Basic logic function mapping (2-input version for fast lookup)
_BASIC_LOGIC = {
    "not": _not,
    "buf": _buf,
    "and": _and,
    "nand": _nand,
    "or": _or,
    "nor": _nor,
    "xor": _xor,
    "xnor": _xnor,
}

# Multi-input logic function mapping
_MULTI_LOGIC = {
    "and": _and_n,
    "nand": _nand_n,
    "or": _or_n,
    "nor": _nor_n,
    "xor": _xor_n,
    "xnor": _xnor_n,
}


def get_primitive_logic(gate_name: str, num_inputs: int) -> LogicFn:
    """Return logic function for specified gate type and input count.

    Args:
        gate_name: Gate type name (lowercase), e.g. "and", "not"
        num_inputs: Number of inputs

    Returns:
        Logic function that accepts num_inputs 0/1 integers and returns 0/1

    Raises:
        ValueError: Unsupported gate type or input count
    """
    gate_name = gate_name.lower()

    if gate_name not in PRIMITIVE_ARITY:
        raise ValueError(
            f"unsupported primitive gate: {gate_name!r}; "
            f"expected one of {sorted(PRIMITIVE_ARITY.keys())}"
        )

    min_arity, max_arity = PRIMITIVE_ARITY[gate_name]
    if num_inputs < min_arity:
        raise ValueError(
            f"gate {gate_name!r} requires at least {min_arity} input(s), got {num_inputs}"
        )
    if max_arity is not None and num_inputs > max_arity:
        raise ValueError(
            f"gate {gate_name!r} accepts at most {max_arity} input(s), got {num_inputs}"
        )

    # Single-input gates: return directly
    if gate_name in ("not", "buf"):
        return _BASIC_LOGIC[gate_name]

    # Two-input gates: use basic version
    if num_inputs == 2:
        return _BASIC_LOGIC[gate_name]

    # Multi-input gates: use extended version
    return _MULTI_LOGIC[gate_name]


def is_primitive(gate_name: str) -> bool:
    """Check if gate type is supported primitive"""
    return gate_name.lower() in PRIMITIVE_ARITY
