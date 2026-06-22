"""Structural Verilog writer — emits a :class:`Circuit` as a netlist."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .node import CONST0, CONST1, CONSTX

if TYPE_CHECKING:
    from .circuit import Circuit


def write_verilog(circuit: Circuit, path: str) -> None:
    with open(path, "w") as f:
        f.write(_emit(circuit))


def _san(name: str) -> str:
    """Sanitise a net name so it is a valid Verilog identifier (no ``[`` or ``]``)."""
    return name.replace("[", "_").replace("]", "")


def _emit(circuit: Circuit) -> str:
    pi = [_san(n) for n in circuit.input_nets]
    po = [_san(n) for n in circuit.output_nets]
    lines = [f"module {circuit.name} ({', '.join(pi + po)});"]

    for net in pi:
        lines.append(f"  input {net};")
    for net in po:
        lines.append(f"  output {net};")

    port_nets = set(pi) | set(po)
    wires: set[str] = set()
    for node in circuit.nodes.values():
        if node.is_gate and node.net:
            name = _san(node.net)
            if name not in port_nets:
                wires.add(name)

    if wires:
        lines.append(f"  wire {', '.join(sorted(wires))};")

    inst_idx = 0
    for node in circuit.nodes.values():
        if not node.is_gate:
            continue
        # Output port uses Y uniformly
        out_net = _san(node.net) if node.net else f"w{node.node_id}"
        conns = [f".Y({out_net})"]

        # Input ports use A, B, C, ... standard naming
        for i, child in enumerate(node.inputs):
            pin = chr(ord("A") + i)  # A, B, C, ...
            conns.append(f".{pin}({_net_str(circuit, child)})")

        lines.append(f"  {node.kind} _{inst_idx}_ ( {', '.join(conns)} );")
        inst_idx += 1

    for node in circuit.nodes.values():
        if not node.is_po or not node.inputs:
            continue
        src = _net_str(circuit, node.inputs[0])
        if src != _san(node.net):
            lines.append(f"  assign {_san(node.net)} = {src}; ")

    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _net_str(circuit: Circuit, nid: int) -> str:
    node = circuit.nodes[nid]
    if node.kind in (CONST0, CONST1):
        return f"1'b{1 if node.kind == CONST1 else 0}"
    if node.kind == CONSTX:
        return "1'b0"
    return _san(node.net) if node.net else f"w{nid}"
