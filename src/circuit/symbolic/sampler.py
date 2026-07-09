"""Directed and random sampling for expression discovery."""

from __future__ import annotations

import itertools
import random

from .resolver import Field

EXHAUSTIVE_MAX_BITS = 16


def support_bit_count(fields: list[Field]) -> int:
    return sum(len(field.bits) for field in fields)


def _pattern_key(pattern: dict[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(pattern.items()))


def _pattern_from_values(fields: list[Field], values: dict[str, int]) -> dict[str, int]:
    pattern: dict[str, int] = {}
    for field in fields:
        value = values.get(field.label, 0)
        for bit in field.bits:
            pattern[bit.net] = (value >> bit.index) & 1
    return pattern


def _append(out: list[dict[str, int]], seen: set[tuple[tuple[str, int], ...]],
            pattern: dict[str, int], limit: int) -> None:
    if len(out) >= limit:
        return
    key = _pattern_key(pattern)
    if key in seen:
        return
    seen.add(key)
    out.append(pattern)


def exhaustive_patterns(fields: list[Field]) -> list[dict[str, int]]:
    """Generate every assignment when the support is small enough."""
    bits = [(field, bit) for field in fields for bit in field.bits]
    n = len(bits)
    if n > EXHAUSTIVE_MAX_BITS:
        raise ValueError(
            f"support has {n} bits; exhaustive limit is {EXHAUSTIVE_MAX_BITS}")
    patterns: list[dict[str, int]] = []
    for mask in range(1 << n):
        p: dict[str, int] = {}
        for i, (_field, bit) in enumerate(bits):
            p[bit.net] = (mask >> i) & 1
        patterns.append(p)
    return patterns


def directed_random_patterns(fields: list[Field], count: int,
                             seed: int | None = 0) -> list[dict[str, int]]:
    """Generate deterministic edge-case samples plus random assignments."""
    if count < 1:
        raise ValueError(f"pattern count must be >= 1, got {count}")
    bit_count = support_bit_count(fields)
    if bit_count <= EXHAUSTIVE_MAX_BITS:
        count = min(count, 1 << bit_count)
    rng = random.Random(seed)
    out: list[dict[str, int]] = []
    seen: set[tuple[tuple[str, int], ...]] = set()

    zeros = {field.label: 0 for field in fields}
    ones = {field.label: (1 << field.width) - 1 for field in fields}
    _append(out, seen, _pattern_from_values(fields, zeros), count)
    _append(out, seen, _pattern_from_values(fields, ones), count)

    # Small cross-product: useful for arithmetic recognition without waiting
    # for random sampling to hit small operands.
    small_values: list[list[int]] = []
    for field in fields:
        maxv = (1 << field.width) - 1
        vals = [0, 1, min(2, maxv), min(3, maxv), maxv]
        vals = sorted(set(vals))
        small_values.append(vals)
    product_cap = min(count, 256)
    for combo in itertools.islice(itertools.product(*small_values), product_cap):
        _append(out, seen,
                _pattern_from_values(fields, dict(zip((f.label for f in fields), combo))),
                count)
        if len(out) >= count:
            return out

    # One-hot and two-hot per bus catch shifts, slices and bitwise boundaries.
    for field in fields:
        maxv = (1 << field.width) - 1
        for i in range(field.width):
            vals = dict(zeros)
            vals[field.label] = 1 << i
            _append(out, seen, _pattern_from_values(fields, vals), count)
            vals = dict(ones)
            vals[field.label] = maxv ^ (1 << i)
            _append(out, seen, _pattern_from_values(fields, vals), count)
            if len(out) >= count:
                return out
        for i in range(min(field.width, 12)):
            for j in range(i + 1, min(field.width, 12)):
                vals = dict(zeros)
                vals[field.label] = (1 << i) | (1 << j)
                _append(out, seen, _pattern_from_values(fields, vals), count)
                if len(out) >= count:
                    return out

    while len(out) < count:
        vals = {
            field.label: rng.randrange(1 << field.width)
            for field in fields
        }
        _append(out, seen, _pattern_from_values(fields, vals), count)
    return out
