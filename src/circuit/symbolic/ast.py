"""Verilog-compatible expression AST with TABLE-I cost accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .bitvec import BitVec, align, concat, mask, reduce_value, repeat


class Expr(Protocol):
    """Common expression protocol."""

    width: int

    def render(self) -> str:
        ...

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        ...

    def cost(self) -> int:
        ...


def _paren(expr: Expr) -> str:
    if isinstance(expr, (Var, Const, Select, PartSelect, Concat, Replicate, Cast)):
        return expr.render()
    return f"({expr.render()})"


@dataclass(frozen=True)
class Var:
    name: str
    width: int
    signed: bool = False

    def render(self) -> str:
        return self.name

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        if self.name not in env:
            raise KeyError(f"missing value for {self.name!r}")
        return env[self.name].resize(self.width, signed=self.signed)

    def cost(self) -> int:
        return 0


@dataclass(frozen=True)
class Const:
    value: int
    width: int = 32
    signed: bool = False

    def render(self) -> str:
        base = "sd" if self.signed else "d"
        if self.value < 0:
            return f"-{self.width}'{base}{abs(self.value)}"
        return f"{self.width}'{base}{self.value}"

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        return BitVec(self.value, self.width, self.signed)

    def cost(self) -> int:
        return 0


@dataclass(frozen=True)
class Select:
    expr: Expr
    index: int
    width: int = 1

    def render(self) -> str:
        return f"{_paren(self.expr)}[{self.index}]"

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        return self.expr.evaluate(env).select(self.index)

    def cost(self) -> int:
        return self.expr.cost() + 1


@dataclass(frozen=True)
class PartSelect:
    expr: Expr
    hi: int
    lo: int

    @property
    def width(self) -> int:
        return abs(self.hi - self.lo) + 1

    def render(self) -> str:
        return f"{_paren(self.expr)}[{self.hi}:{self.lo}]"

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        return self.expr.evaluate(env).part_select(self.hi, self.lo)

    def cost(self) -> int:
        return self.expr.cost() + 1


@dataclass(frozen=True)
class Concat:
    parts: tuple[Expr, ...]

    def __post_init__(self) -> None:
        if not self.parts:
            raise ValueError("concat requires at least one part")

    @property
    def width(self) -> int:
        return sum(part.width for part in self.parts)

    def render(self) -> str:
        return "{" + ", ".join(part.render() for part in self.parts) + "}"

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        return concat([part.evaluate(env) for part in self.parts])

    def cost(self) -> int:
        return sum(part.cost() for part in self.parts) + len(self.parts)


@dataclass(frozen=True)
class Replicate:
    count: int
    expr: Expr

    @property
    def width(self) -> int:
        return max(1, self.count * self.expr.width)

    def render(self) -> str:
        return f"{{{self.count}{{{self.expr.render()}}}}}"

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        return repeat(self.count, self.expr.evaluate(env))

    def cost(self) -> int:
        return self.expr.cost() + 1


@dataclass(frozen=True)
class Cast:
    """Verilog signedness cast, e.g. ``$signed(x)`` or ``$unsigned(x)``."""

    signed: bool
    expr: Expr

    @property
    def width(self) -> int:
        return self.expr.width

    def render(self) -> str:
        fn = "$signed" if self.signed else "$unsigned"
        return f"{fn}({_paren(self.expr)})"

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        value = self.expr.evaluate(env)
        return value.resize(value.width, signed=self.signed)

    def cost(self) -> int:
        return self.expr.cost()


@dataclass(frozen=True)
class Unary:
    op: str
    expr: Expr

    @property
    def width(self) -> int:
        if self.op in ("!", "&", "|", "^", "~&", "~|", "^~", "~^"):
            return 1
        return self.expr.width

    def render(self) -> str:
        return f"{self.op}{_paren(self.expr)}"

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        val = self.expr.evaluate(env)
        if self.op == "~":
            return BitVec((~val.unsigned) & mask(val.width), val.width, val.signed)
        if self.op == "!":
            return BitVec(0 if val.truthy() else 1, 1, False)
        if self.op == "+":
            return val
        if self.op == "-":
            return BitVec(-val.unsigned, val.width, val.signed)
        if self.op in ("&", "|", "^", "~&", "~|", "^~", "~^"):
            return reduce_value(self.op, val)
        raise ValueError(f"unsupported unary operator {self.op!r}")

    def cost(self) -> int:
        if self.op == "~":
            return self.expr.cost() + self.expr.width
        return self.expr.cost() + 1


_ARITH = {"+", "-", "*", "/", "%", "**"}
_SHIFT = {"<<", ">>", "<<<", ">>>"}
_CMP = {">", ">=", "<", "<="}
_EQ = {"==", "!=", "===", "!=="}
_LOGICAL = {"&&", "||"}
_BITWISE = {"&", "|", "^", "^~", "~^"}


@dataclass(frozen=True)
class Binary:
    op: str
    lhs: Expr
    rhs: Expr

    @property
    def width(self) -> int:
        if self.op in _CMP or self.op in _EQ or self.op in _LOGICAL:
            return 1
        if self.op in _SHIFT:
            return self.lhs.width
        if self.op == "*":
            return max(1, self.lhs.width + self.rhs.width)
        if self.op == "**":
            return self.lhs.width
        return max(self.lhs.width, self.rhs.width)

    def render(self) -> str:
        return f"{_paren(self.lhs)} {self.op} {_paren(self.rhs)}"

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        a = self.lhs.evaluate(env)
        b = self.rhs.evaluate(env)
        if self.op in _BITWISE:
            av, bv, width = align(a, b)
            if self.op == "&":
                out = av & bv
            elif self.op == "|":
                out = av | bv
            elif self.op == "^":
                out = av ^ bv
            else:
                out = (~(av ^ bv)) & mask(width)
            return BitVec(out, width, a.signed and b.signed)

        width = self.width
        if self.op == "+":
            out = a.unsigned + b.unsigned
        elif self.op == "-":
            out = a.unsigned - b.unsigned
        elif self.op == "*":
            out = a.unsigned * b.unsigned
        elif self.op == "/":
            out = mask(width) if b.unsigned == 0 else a.unsigned // b.unsigned
        elif self.op == "%":
            out = a.unsigned if b.unsigned == 0 else a.unsigned % b.unsigned
        elif self.op == "**":
            out = pow(a.unsigned, b.unsigned, 1 << width)
        elif self.op == "<<":
            out = a.unsigned << b.unsigned
        elif self.op == ">>":
            out = a.unsigned >> b.unsigned
        elif self.op == "<<<":
            out = a.unsigned << b.unsigned
        elif self.op == ">>>":
            out = a.signed_value >> b.unsigned if a.signed else a.unsigned >> b.unsigned
        elif self.op in _CMP:
            left = a.signed_value if (a.signed and b.signed) else a.unsigned
            right = b.signed_value if (a.signed and b.signed) else b.unsigned
            out = {
                ">": left > right,
                ">=": left >= right,
                "<": left < right,
                "<=": left <= right,
            }[self.op]
        elif self.op in _EQ:
            eq = a.unsigned == b.unsigned
            out = eq if self.op in ("==", "===") else not eq
        elif self.op == "&&":
            out = a.truthy() and b.truthy()
        elif self.op == "||":
            out = a.truthy() or b.truthy()
        else:
            raise ValueError(f"unsupported binary operator {self.op!r}")
        return BitVec(int(out), width, a.signed and b.signed)

    def cost(self) -> int:
        if self.op in _BITWISE:
            return self.lhs.cost() + self.rhs.cost() + self.width
        return self.lhs.cost() + self.rhs.cost() + 1


@dataclass(frozen=True)
class Conditional:
    cond: Expr
    if_true: Expr
    if_false: Expr

    @property
    def width(self) -> int:
        return max(self.if_true.width, self.if_false.width)

    def render(self) -> str:
        return f"{_paren(self.cond)} ? {_paren(self.if_true)} : {_paren(self.if_false)}"

    def evaluate(self, env: dict[str, BitVec]) -> BitVec:
        branch = self.if_true if self.cond.evaluate(env).truthy() else self.if_false
        return branch.evaluate(env).resize(self.width)

    def cost(self) -> int:
        return self.cond.cost() + self.if_true.cost() + self.if_false.cost() + 1


def expr_key(expr: Expr) -> str:
    """Stable key for expression de-duplication."""
    return expr.render()
