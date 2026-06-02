"""Liberty (``.lib``) parsing and gate-logic generation.

A :class:`Library` keeps only each cell's name, pins, and boolean function.
Every cell derives a *logic* callable mapping input bits to its output bit.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping

# A logic function takes the input bits (in pin-declaration order) and returns
# the output bit.
LogicFn = Callable[..., int]


def _eval_boolean(expr: str, values: Mapping[str, int]) -> int:
    """Evaluate a Liberty boolean ``function`` string for one input assignment.

    Supported operators, in increasing precedence:

    * ``+`` / ``|`` — OR
    * ``^``         — XOR
    * ``*`` / ``&`` — AND
    * ``!`` (prefix) / ``'`` (postfix) — NOT
    * ``(`` ``)``   — grouping

    All operands and results are single bits (``0`` or ``1``).  This runs only
    while a cell's truth table is being built, never in the simulation hot path.
    """
    pos = 0
    n = len(expr)

    def skip_ws() -> None:
        nonlocal pos
        while pos < n and expr[pos].isspace():
            pos += 1

    def parse_or() -> int:
        skip_ws()
        val = parse_xor()
        while True:
            skip_ws()
            if pos < n and expr[pos] in "+|":
                pos_advance()
                val |= parse_xor()
            else:
                return val

    def parse_xor() -> int:
        skip_ws()
        val = parse_and()
        while True:
            skip_ws()
            if pos < n and expr[pos] == "^":
                pos_advance()
                val ^= parse_and()
            else:
                return val

    def parse_and() -> int:
        skip_ws()
        val = parse_not()
        while True:
            skip_ws()
            # AND may be explicit (`*`/`&`) or implicit (juxtaposition).
            if pos < n and expr[pos] in "*&":
                pos_advance()
                val &= parse_not()
            elif pos < n and (expr[pos].isalnum() or expr[pos] in "(!"):
                val &= parse_not()
            else:
                return val

    def parse_not() -> int:
        nonlocal pos
        skip_ws()
        negate = False
        while pos < n and expr[pos] == "!":
            negate = not negate
            pos += 1
        val = parse_primary()
        skip_ws()
        while pos < n and expr[pos] == "'":  # postfix NOT
            negate = not negate
            pos += 1
            skip_ws()
        return (1 - val) if negate else val

    def parse_primary() -> int:
        nonlocal pos
        skip_ws()
        if pos < n and expr[pos] == "(":
            pos += 1  # consume '('
            val = parse_or()
            skip_ws()
            if pos < n and expr[pos] == ")":
                pos += 1
            return val
        # An identifier: variable name composed of word characters.
        start = pos
        while pos < n and (expr[pos].isalnum() or expr[pos] == "_"):
            pos += 1
        name = expr[start:pos]
        if not name:
            raise ValueError(f"cannot parse boolean function near: {expr[pos:]!r}")
        return values[name]

    def pos_advance() -> None:
        nonlocal pos
        pos += 1

    result = parse_or()
    skip_ws()
    if pos != n:
        raise ValueError(f"trailing characters in boolean function: {expr!r}")
    return result


def _extract_blocks(text: str, keyword: str) -> list[tuple[str, str]]:
    """Return ``(name, body)`` for every ``keyword(name) { ... }`` block.

    Brace counting is used so nested groups (e.g. ``pin`` inside ``cell``) are
    handled correctly.
    """
    blocks: list[tuple[str, str]] = []
    header = re.compile(rf"{keyword}\s*\(\s*([^)]*?)\s*\)\s*{{")
    for m in header.finditer(text):
        name = m.group(1).strip()
        depth = 1
        i = m.end()
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        blocks.append((name, text[m.end() : i - 1]))
    return blocks


@dataclass
class Cell:
    """A single combinational gate type from the library."""

    name: str
    inputs: list[str] = field(default_factory=list)
    output: str = ""
    function: str = ""

    def make_logic(self) -> LogicFn:
        """Build the gate's logic function as a positional callable.

        The boolean ``function`` is evaluated for every input combination once,
        producing a truth table; the returned callable is then a dict lookup
        accepting the input bits in :attr:`inputs` order.
        """
        if not self.function:
            raise ValueError(f"cell {self.name!r} has no output function")

        table: dict[tuple[int, ...], int] = {}
        for combo in itertools.product((0, 1), repeat=len(self.inputs)):
            assignment = dict(zip(self.inputs, combo))
            table[combo] = _eval_boolean(self.function, assignment)

        def logic(*args: int) -> int:
            return table[args]

        logic.__name__ = f"{self.name}_logic"
        return logic


class Library:
    """A logic library parsed from a Liberty file.

    Only cell names, pin directions and boolean functions are retained.
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.cells: dict[str, Cell] = {}

    # -- construction ---------------------------------------------------

    @classmethod
    def from_file(cls, path: str) -> Library:
        with open(path) as f:
            return cls.from_string(f.read())

    @classmethod
    def from_string(cls, text: str) -> Library:
        # Strip comments before structural parsing.
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
        text = re.sub(r"//[^\n]*", "", text)

        m = re.search(r"library\s*\(\s*([^)]*?)\s*\)", text)
        lib = cls(name=m.group(1).strip() if m else "")

        for cell_name, cell_body in _extract_blocks(text, "cell"):
            cell = Cell(name=cell_name)
            for pin_name, pin_body in _extract_blocks(cell_body, "pin"):
                direction = re.search(r"direction\s*:\s*(\w+)", pin_body)
                if direction and direction.group(1) == "input":
                    cell.inputs.append(pin_name)
                elif direction and direction.group(1) == "output":
                    cell.output = pin_name
                fn = re.search(r'function\s*:\s*"([^"]*)"', pin_body)
                if fn:
                    cell.function = fn.group(1)
            lib.cells[cell_name] = cell

        return lib

    # -- access ---------------------------------------------------------

    def __getitem__(self, name: str) -> Cell:
        return self.cells[name]

    def __contains__(self, name: str) -> bool:
        return name in self.cells

    def __len__(self) -> int:
        return len(self.cells)

    def __repr__(self) -> str:
        return f"Library({self.name!r}, cells={list(self.cells)})"
