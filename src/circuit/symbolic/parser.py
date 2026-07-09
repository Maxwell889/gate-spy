"""Small Verilog-expression parser for explicit candidate validation.

This parser is intentionally scoped to expression hypotheses an agent can write
after probing a circuit.  It builds the same typed AST used by candidate search;
it does not evaluate Python code or accept statements/modules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ast import (
    Binary,
    Cast,
    Conditional,
    Concat,
    Const,
    Expr,
    PartSelect,
    Replicate,
    Select,
    Unary,
    Var,
)


_TOKEN_RE = re.compile(
    r"\s*(?:(\d+'[sS]?[dDhHbBoO][0-9a-fA-F_xXzZ]+|\d+)|"
    r"(\$[A-Za-z_][$A-Za-z0-9_]*|[A-Za-z_][$A-Za-z0-9_]*)|"
    r"(===|!==|==|!=|>=|<=|<<<|>>>|<<|>>|&&|\|\||\*\*|~&|~\||\^~|~\^|[+\-*/%&|^~!<>()?:\[\],{}]))"
)

_PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4,
    "&": 5,
    "==": 6,
    "!=": 6,
    "===": 6,
    "!==": 6,
    ">": 7,
    ">=": 7,
    "<": 7,
    "<=": 7,
    "<<": 8,
    ">>": 8,
    "<<<": 8,
    ">>>": 8,
    "+": 9,
    "-": 9,
    "*": 10,
    "/": 10,
    "%": 10,
    "**": 11,
}


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str


def _parse_int(text: str) -> tuple[int, int | None, bool]:
    if "'" not in text:
        return int(text, 10), None, False
    width_s, body = text.split("'", 1)
    width = int(width_s)
    signed = body.lower().startswith("s")
    base_ch = body[1 if signed else 0].lower()
    digits = body[2 if signed else 1:].replace("_", "")
    base = {"d": 10, "h": 16, "b": 2, "o": 8}[base_ch]
    if "x" in digits.lower() or "z" in digits.lower():
        raise ValueError(f"unknown/high-Z constants are not supported: {text!r}")
    return int(digits, base), width, signed


def _tokenize(text: str) -> list[_Token]:
    out: list[_Token] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if not match:
            raise ValueError(f"unsupported expression syntax near {text[pos:pos + 20]!r}")
        number, ident, op = match.groups()
        pos = match.end()
        if number is not None:
            out.append(_Token("number", number))
        elif ident is not None:
            out.append(_Token("ident", ident))
        else:
            out.append(_Token("op", op))
    out.append(_Token("eof", ""))
    return out


class _Parser:
    def __init__(self, text: str, widths: dict[str, int], target_width: int) -> None:
        self.tokens = _tokenize(text)
        self.pos = 0
        self.widths = widths
        self.target_width = max(1, target_width)

    def _peek(self) -> _Token:
        return self.tokens[self.pos]

    def _pop(self) -> _Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _accept(self, value: str) -> bool:
        if self._peek().value == value:
            self._pop()
            return True
        return False

    def _expect(self, value: str) -> None:
        if not self._accept(value):
            raise ValueError(f"expected {value!r}, got {self._peek().value!r}")

    def parse(self) -> Expr:
        expr = self._expr(0)
        if self._peek().kind != "eof":
            raise ValueError(f"unexpected token {self._peek().value!r}")
        return expr

    def _expr(self, min_prec: int) -> Expr:
        lhs = self._prefix()
        while True:
            tok = self._peek()
            if tok.value == "?":
                if min_prec > 0:
                    break
                self._pop()
                if_true = self._expr(0)
                self._expect(":")
                if_false = self._expr(0)
                lhs = Conditional(lhs, if_true, if_false)
                continue
            prec = _PRECEDENCE.get(tok.value)
            if prec is None or prec < min_prec:
                break
            op = self._pop().value
            next_min = prec if op == "**" else prec + 1
            rhs = self._expr(next_min)
            lhs = Binary(op, lhs, rhs)
        return lhs

    def _prefix(self) -> Expr:
        tok = self._pop()
        if tok.value == "(":
            expr = self._expr(0)
            self._expect(")")
            return self._postfix(expr)
        if tok.value == "{":
            first = self._expr(0)
            if self._accept("{"):
                if not isinstance(first, Const):
                    raise ValueError("replication count must be a constant")
                expr = self._expr(0)
                self._expect("}")
                self._expect("}")
                return self._postfix(Replicate(first.value, expr))
            parts = [first]
            while self._accept(","):
                parts.append(self._expr(0))
            self._expect("}")
            return self._postfix(Concat(tuple(parts)))
        if tok.kind == "number":
            value, width, signed = _parse_int(tok.value)
            expr = Const(value, width or self.target_width, signed)
            return self._postfix(expr)
        if tok.kind == "ident":
            if tok.value in {"$signed", "$unsigned"}:
                self._expect("(")
                expr = self._expr(0)
                self._expect(")")
                return self._postfix(Cast(tok.value == "$signed", expr))
            if tok.value not in self.widths:
                raise ValueError(f"unknown input variable {tok.value!r}")
            return self._postfix(Var(tok.value, self.widths[tok.value]))
        if tok.value in {"+", "-", "~", "!"}:
            expr = self._expr(12)
            return Unary(tok.value, expr)
        raise ValueError(f"unexpected token {tok.value!r}")

    def _postfix(self, expr: Expr) -> Expr:
        while self._accept("["):
            hi_tok = self._pop()
            if hi_tok.kind != "number":
                raise ValueError("select/part-select index must be a constant")
            hi, _width, _signed = _parse_int(hi_tok.value)
            if self._accept(":"):
                lo_tok = self._pop()
                if lo_tok.kind != "number":
                    raise ValueError("part-select low index must be a constant")
                lo, _width, _signed = _parse_int(lo_tok.value)
                self._expect("]")
                expr = PartSelect(expr, hi, lo)
            else:
                self._expect("]")
                expr = Select(expr, hi)
        return expr


def parse_expression(text: str, *, input_widths: dict[str, int],
                     target_width: int) -> Expr:
    """Parse a bounded Verilog expression into GateSpy's typed AST."""
    return _Parser(text, input_widths, target_width).parse()


def expression_identifiers(text: str) -> tuple[str, ...]:
    """Return variable identifiers referenced by an expression string."""
    names: list[str] = []
    seen: set[str] = set()
    for token in _tokenize(text):
        if token.kind != "ident":
            continue
        if token.value.startswith("$"):
            continue
        if token.value in seen:
            continue
        seen.add(token.value)
        names.append(token.value)
    return tuple(names)
