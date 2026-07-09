"""Small typed bit-vector runtime for Verilog-expression evaluation."""

from __future__ import annotations

from dataclasses import dataclass


def mask(width: int) -> int:
    """Return a bit mask with *width* low bits set."""
    if width <= 0:
        return 0
    return (1 << width) - 1


@dataclass(frozen=True)
class BitVec:
    """Unsigned bit-vector value with an optional signed interpretation flag."""

    value: int
    width: int
    signed: bool = False

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError(f"bit-vector width must be >= 1, got {self.width}")
        object.__setattr__(self, "value", int(self.value) & mask(self.width))

    @property
    def unsigned(self) -> int:
        return self.value & mask(self.width)

    @property
    def signed_value(self) -> int:
        val = self.unsigned
        sign = 1 << (self.width - 1)
        return val - (1 << self.width) if val & sign else val

    def truthy(self) -> bool:
        return self.unsigned != 0

    def resize(self, width: int, *, signed: bool | None = None) -> "BitVec":
        return BitVec(self.unsigned, width, self.signed if signed is None else signed)

    def select(self, index: int) -> "BitVec":
        if index < 0:
            raise ValueError(f"negative bit index {index}")
        return BitVec((self.unsigned >> index) & 1, 1, False)

    def part_select(self, hi: int, lo: int) -> "BitVec":
        if hi < lo:
            hi, lo = lo, hi
        if lo < 0:
            raise ValueError(f"negative part-select low index {lo}")
        width = hi - lo + 1
        return BitVec((self.unsigned >> lo) & mask(width), width, False)


def align(a: BitVec, b: BitVec) -> tuple[int, int, int]:
    """Return aligned unsigned operands and their common width."""
    width = max(a.width, b.width)
    return a.unsigned & mask(width), b.unsigned & mask(width), width


def concat(parts: list[BitVec]) -> BitVec:
    """Concatenate parts in Verilog order: leftmost part becomes the MSBs."""
    if not parts:
        raise ValueError("concat requires at least one part")
    value = 0
    width = 0
    for part in parts:
        value = (value << part.width) | part.unsigned
        width += part.width
    return BitVec(value, width, False)


def repeat(count: int, part: BitVec) -> BitVec:
    """Replicate *part* *count* times."""
    if count < 0:
        raise ValueError(f"replication count must be >= 0, got {count}")
    if count == 0:
        return BitVec(0, 1, False)
    return concat([part] * count)


def reduce_value(op: str, value: BitVec) -> BitVec:
    """Evaluate a Verilog reduction operator."""
    bits = [(value.unsigned >> i) & 1 for i in range(value.width)]
    if op == "&":
        out = int(all(bits))
    elif op == "|":
        out = int(any(bits))
    elif op == "^":
        out = 0
        for bit in bits:
            out ^= bit
    elif op == "~&":
        out = int(not all(bits))
    elif op == "~|":
        out = int(not any(bits))
    elif op in ("^~", "~^"):
        out = 0
        for bit in bits:
            out ^= bit
        out = 1 - out
    else:
        raise ValueError(f"unsupported reduction operator {op!r}")
    return BitVec(out, 1, False)
