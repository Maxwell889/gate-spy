"""Verilog structural-netlist parser.

Converts yosys-flattened gate-level Verilog into the nodes and edges of a
:class:`~src.circuit.circuit.Circuit` DAG.
"""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

from .node import _CONST_NET

if TYPE_CHECKING:
    from .circuit import Circuit

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# A sized literal such as 4'hF, 2'b0x, 11'h000.
_LITERAL = re.compile(r"^(\d+)'([bBoOdDhH])([0-9a-fA-FxXzZ_]+)$")

# One signal term: an (escaped) identifier with an optional bit/part select.
_TERM = re.compile(
    r"""\s*
        (?:(?P<esc>\\\S+)|(?P<name>[A-Za-z_][\w$.]*))
        \s*(?:\[\s*(?P<sel>\d+(?:\s*:\s*\d+)?)\s*\])?
        \s*$""",
    re.VERBOSE,
)


# ---------------------------------------------------------------------------
# Verilog text helpers
# ---------------------------------------------------------------------------


def _clean_id(name: str) -> str:
    r"""Normalise an identifier: drop the leading escape ``\`` and surrounding space."""
    name = name.strip()
    return name[1:].strip() if name.startswith("\\") else name


def _parse_range(text: str) -> tuple[int, int]:
    """Parse a ``[msb:lsb]`` declaration into an ``(msb, lsb)`` tuple."""
    msb, lsb = re.findall(r"\d+", text)
    return int(msb), int(lsb)


def _split_statements(text: str) -> list[str]:
    """Split a module body into statements on top-level ``;``.

    Depth is tracked across ``()`` and ``{}`` so multi-line gate instances and
    concatenations stay intact.
    """
    out: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "({":
            depth += 1
        elif ch in ")}":
            depth -= 1
        elif ch == ";" and depth == 0:
            out.append(text[start:i + 1])
            start = i + 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _split_top(text: str) -> list[str]:
    """Split a comma list at top level (ignoring commas inside ``{}``/``[]``)."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    if text[start:].strip():
        parts.append(text[start:])
    return [p.strip() for p in parts if p.strip()]


def _parse_literal(tok: str) -> list[str] | None:
    """Expand a sized Verilog literal to per-bit constant nets, MSB first.

    Returns ``None`` if *tok* is not a literal.
    """
    m = _LITERAL.match(tok.strip())
    if not m:
        return None
    width, base, digits = int(m.group(1)), m.group(2).lower(), m.group(3).replace("_", "")

    if base == "b":
        bits = digits
    elif base == "d":
        bits = format(int(digits), "b")
    else:
        per = 3 if base == "o" else 4
        chunks = []
        for d in digits:
            chunks.append("x" * per if d in "xXzZ"
                          else format(int(d, 8 if base == "o" else 16), f"0{per}b"))
        bits = "".join(chunks)

    bits = bits.replace("z", "x").replace("Z", "x")
    # Normalise to exactly `width` bits, MSB first.
    bits = bits[-width:].rjust(width, "0") if len(bits) >= width else bits.rjust(width, "0")
    return [_CONST_NET.get(b, _CONST_NET["x"]) for b in bits]


def _expand(expr: str, widths: dict[str, tuple[int, int]]) -> list[str]:
    """Expand a signal expression into a flat list of bit nets (MSB first)."""
    expr = expr.strip()
    if expr.startswith("{"):
        inner = expr[1:expr.rfind("}")]
        bits: list[str] = []
        for part in _split_top(inner):
            bits.extend(_expand(part, widths))
        return bits

    literal = _parse_literal(expr)
    if literal is not None:
        return literal

    m = _TERM.match(expr)
    if not m:
        raise ValueError(f"cannot parse signal expression: {expr!r}")
    base = _clean_id(m.group("esc") or m.group("name"))
    sel = m.group("sel")

    if sel is None:
        bounds = widths.get(base)
        if bounds:  # bare bus reference -> all its bits, MSB first
            msb, lsb = bounds
            step = -1 if msb >= lsb else 1
            return [f"{base}[{i}]" for i in range(msb, lsb + step, step)]
        return [base]  # scalar

    if ":" in sel:
        msb, lsb = (int(x) for x in sel.split(":"))
        step = -1 if msb >= lsb else 1
        return [f"{base}[{i}]" for i in range(msb, lsb + step, step)]
    return [f"{base}[{int(sel)}]"]


def _single_net(expr: str, widths: dict[str, tuple[int, int]]) -> str:
    """Expand a connection expected to be one bit, returning that net name."""
    if not expr.strip():
        return ""
    bits = _expand(expr, widths)
    if len(bits) != 1:
        raise ValueError(f"expected a 1-bit connection, got {len(bits)}: {expr!r}")
    return bits[0]


# ---------------------------------------------------------------------------
# Top-level parse entry point
# ---------------------------------------------------------------------------

def parse(circuit: Circuit, text: str) -> None:
    """Parse structural Verilog into *circuit*, populating its nodes and edges.

    This is the module-level equivalent of the old ``Circuit._parse`` method.
    It accesses private internals of *circuit* (``_driver``, ``_alias``,
    ``_add_node``, etc.) deliberately — the parser has always been tightly
    coupled to the Circuit representation.
    """
    header = re.search(r"\bmodule\s+(\S+)\s*\(.*?\)\s*;", text, re.DOTALL)
    if not header:
        raise ValueError("no module declaration found")
    circuit.name = _clean_id(header.group(1))

    body_end = text.rfind("endmodule")
    body = text[header.end():body_end if body_end != -1 else len(text)]

    widths: dict[str, tuple[int, int]] = {}           # bus base name -> (msb, lsb)
    gates: list[tuple[str, str, dict[str, str]]] = []   # (cell, inst, conns)
    assigns: list[tuple[str, str]] = []                 # (lhs, rhs)
    in_ports: list[str] = []                            # raw port signal text
    out_ports: list[str] = []

    for stmt in _split_statements(body):
        stmt = stmt.strip()
        if not stmt:
            continue

        decl = re.match(r"(input|output|wire)\b\s*(\[\s*\d+\s*:\s*\d+\s*\])?\s*(.+)",
                        stmt, re.DOTALL)
        if decl:
            kind, rng, names = decl.group(1), decl.group(2), decl.group(3)
            bounds = _parse_range(rng) if rng else None
            for name in _split_top(names.rstrip(";")):
                base = _clean_id(name)
                if bounds:
                    widths[base] = bounds
                signal = name.strip()
                if kind == "input":
                    in_ports.append(signal)
                elif kind == "output":
                    out_ports.append(signal)
            continue

        assign = re.match(r"assign\s+(.+?)\s*=\s*(.+)", stmt, re.DOTALL)
        if assign:
            assigns.append((assign.group(1).strip(),
                            assign.group(2).strip().rstrip(";").strip()))
            continue

        gate = re.match(r"(\w+)\s+(\\?\S+)\s*\((.*)\)\s*;?\s*$", stmt, re.DOTALL)
        if gate:
            cell_name, inst = gate.group(1), _clean_id(gate.group(2))
            if cell_name not in circuit.lib:
                sys.exit(f"Error: gate '{cell_name}' (instance '{inst}') "
                         f"is not defined in library '{circuit.lib.name}'")
            conns = {pin: net for pin, net in
                     re.findall(r"\.(\w+)\s*\(\s*(.*?)\s*\)", gate.group(3), re.DOTALL)}
            gates.append((cell_name, inst, conns))
            continue
        # Unknown statement (e.g. stray `endmodule`) — ignore.

    # 1. Primary input bits become source nodes.
    for signal in in_ports:
        for net in _expand(signal, widths):
            circuit.input_nets.append(net)
            circuit._driver.setdefault(net, circuit._add_node("PI", net))

    # 2. Continuous assigns define net aliases, bit by bit.
    for lhs, rhs in assigns:
        lbits, rbits = _expand(lhs, widths), _expand(rhs, widths)
        if len(lbits) != len(rbits):
            print(f"Warning: width mismatch in assign {lhs!r} = {rhs!r} "
                  f"({len(lbits)} vs {len(rbits)} bits)", file=sys.stderr)
        for lbit, rbit in zip(lbits, rbits):
            circuit._alias[lbit] = rbit

    # 3. Gate instances: create the node driving each output bit.
    gate_inputs: list[tuple[int, str, dict[str, str]]] = []
    for cell_name, inst, conns in gates:
        cell = circuit.lib[cell_name]
        out_net = _single_net(conns.get(cell.output, ""), widths)
        nid = circuit._add_node(cell_name, out_net, cell=cell, logic=cell.make_logic())
        if out_net:
            circuit._driver[out_net] = nid
        gate_inputs.append((nid, cell_name, conns))

    # 4. Resolve every gate's fan-in to driver node ids.
    for nid, cell_name, conns in gate_inputs:
        cell = circuit.lib[cell_name]
        circuit.nodes[nid].inputs = [
            circuit._resolve(_single_net(conns.get(pin, ""), widths))
            for pin in cell.inputs
        ]

    # 5. Primary output bits become sink nodes fed by their driver.
    for signal in out_ports:
        for net in _expand(signal, widths):
            circuit.output_nets.append(net)
            src = circuit._resolve(net)
            circuit._add_node("PO", net, inputs=[src])

    # 6. Wire up fan-out edges.
    for node in circuit.nodes.values():
        for src in node.inputs:
            circuit.nodes[src].fanouts.append(node.node_id)
