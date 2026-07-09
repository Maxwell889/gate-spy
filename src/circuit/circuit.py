"""Gate-level netlist as a DAG of single-output nodes.

:class:`Circuit` models a technology-mapped, flattened gate-level netlist at
**bit granularity**: each node drives exactly one bit (PI, PO, constant, or a
primitive gate).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable

from .node import Node, CONST0, CONST1, CONSTX, _CONST_KIND
from .parser import parse as _parse_verilog
from .aig_parser import parse_aig as _parse_aig

def _strip_meta(text: str) -> str:
    """Remove comments and ``(* ... *)`` attributes from Verilog source."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"\(\*.*?\*\)", "", text, flags=re.DOTALL)
    return text


import colorsys


def _make_palette(kinds: set[str]) -> dict[str, str]:
    """Assign a visually distinct pastel colour to each gate type.

    Hues are evenly distributed around the colour wheel.  Saturation and
    lightness are fixed to keep the palette soft and readable on white
    backgrounds.
    """
    palette: dict[str, str] = {}
    n = len(kinds)
    if n == 0:
        return palette

    s, l = 0.55, 0.82  # pastel saturation / lightness
    for i, kind in enumerate(sorted(kinds)):
        h = i / n
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        palette[kind] = f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
    return palette


# ---------------------------------------------------------------------------
# Circuit
# ---------------------------------------------------------------------------

class Circuit:
    """A bit-level gate netlist using primitive gates only."""

    def __init__(self) -> None:
        self.name = ""
        self.nodes: dict[int, Node] = {}

        # Primary I/O bit nets, in declaration order (MSB first within a bus).
        self.input_nets: list[str] = []
        self.output_nets: list[str] = []
        # Top-level module ports exactly as listed in the Verilog header.
        # AIGER inputs leave this empty and recovery falls back to I/O word order.
        self.module_ports: list[str] = []

        self._next_id = 0
        self._driver: dict[str, int] = {}   # net -> node id that drives it
        self._alias: dict[str, str] = {}     # net -> net it is defined equal to
        self._topo: list[int] | None = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, verilog_path: str) -> Circuit:
        """Parse a Verilog file containing primitive gates."""
        with open(verilog_path) as f:
            return cls.from_string(f.read())

    @classmethod
    def from_string(cls, text: str) -> Circuit:
        """Parse Verilog text containing primitive gates."""
        circuit = cls()
        _parse_verilog(circuit, _strip_meta(text))
        return circuit

    @classmethod
    def from_aig_file(cls, aig_path: str) -> Circuit:
        """Parse an AIGER file (binary ``.aig`` or ASCII ``.aag``) into a Circuit.

        The circuit name is taken from the file stem.
        """
        with open(aig_path, "rb") as f:
            circuit = cls.from_aig_bytes(f.read())
        circuit.name = Path(aig_path).stem
        return circuit

    @classmethod
    def from_aig_bytes(cls, data: bytes) -> Circuit:
        """Parse raw AIGER bytes (binary or ASCII) into a Circuit."""
        circuit = cls()
        _parse_aig(circuit, data)
        return circuit

    def _add_node(self, kind: str, net: str = "", *,
                  logic: Callable[..., int] | None = None,
                  inputs: list[int] | None = None) -> int:
        """Add a node to the circuit and return its ID."""
        nid = self._next_id
        self._next_id += 1
        self.nodes[nid] = Node(
            node_id=nid, kind=kind, net=net,
            inputs=inputs or [], cell=None, logic=logic,
        )
        return nid

    def _resolve(self, net: str) -> int:
        """Return the node id driving *net*, following alias chains.

        Constant nets materialise constant nodes on demand; a net with no
        driver at all becomes a dangling source (warned once).
        """
        canon = self._chase(net)
        if canon in self._driver:
            return self._driver[canon]
        if canon in _CONST_KIND:
            nid = self._add_node(_CONST_KIND[canon], canon)
            self._driver[canon] = nid
            return nid
        # Genuinely undriven — keep the DAG total by inserting a 0 source.
        print(f"Warning: net '{net}' has no driver; tying to 0", file=sys.stderr)
        nid = self._add_node(CONST0, canon)
        self._driver[canon] = nid
        return nid

    def _chase(self, net: str) -> str:
        seen: set[str] = set()
        while net in self._alias and net not in seen:
            seen.add(net)
            net = self._alias[net]
        return net

    # ------------------------------------------------------------------
    # Topology / queries
    # ------------------------------------------------------------------

    @property
    def pi_nodes(self) -> list[int]:
        return [n.node_id for n in self.nodes.values() if n.is_pi]

    @property
    def po_nodes(self) -> list[int]:
        return [n.node_id for n in self.nodes.values() if n.is_po]

    @property
    def gate_nodes(self) -> list[int]:
        return [n.node_id for n in self.nodes.values() if n.is_gate]

    def topological_order(self) -> list[int]:
        """Node ids in a topological order (sources first, sinks last)."""
        if self._topo is not None:
            return self._topo

        indegree = {nid: len(node.inputs) for nid, node in self.nodes.items()}
        queue = [nid for nid, d in indegree.items() if d == 0]
        order: list[int] = []
        while queue:
            nid = queue.pop()
            order.append(nid)
            for out in self.nodes[nid].fanouts:
                indegree[out] -= 1
                if indegree[out] == 0:
                    queue.append(out)

        if len(order) != len(self.nodes):
            raise ValueError("circuit is not acyclic (combinational loop detected)")
        self._topo = order
        return order

    def simulate_values(self, inputs: dict[str, int]) -> dict[int, int]:
        """Evaluate the circuit and return *every* node's bit value.

        Args:
            inputs: maps primary-input net names (e.g. ``"io_a[3]"`` or a scalar
                port name) to ``0`` / ``1``.  Unspecified inputs default to 0.

        Returns:
            Maps each node id to its computed bit value — primary outputs,
            internal gates, constants and inputs alike.  Use this when you need
            visibility into internal signals; :meth:`simulate` is the
            output-only convenience wrapper.
        """
        value: dict[int, int] = {}
        const_value = {CONST0: 0, CONST1: 1, CONSTX: 0}

        for nid in self.topological_order():
            node = self.nodes[nid]
            if node.kind in const_value:
                value[nid] = const_value[node.kind]
            elif node.is_pi:
                value[nid] = inputs.get(node.net, 0)
            elif node.is_po:
                value[nid] = value[node.inputs[0]] if node.inputs else 0
            else:  # library gate
                assert node.logic is not None
                value[nid] = node.logic(*(value[i] for i in node.inputs))

        return value

    def simulate(self, inputs: dict[str, int]) -> dict[str, int]:
        """Evaluate the circuit for the given primary-input bit values.

        Args:
            inputs: maps primary-input net names (e.g. ``"io_a[3]"`` or a scalar
                port name) to ``0`` / ``1``.  Unspecified inputs default to 0.

        Returns:
            Maps each primary-output net name to its computed bit value.
        """
        value = self.simulate_values(inputs)
        return {self.nodes[nid].net: value[nid] for nid in self.po_nodes}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def gate_histogram(self) -> dict[str, int]:
        hist: dict[str, int] = {}
        for nid in self.gate_nodes:
            kind = self.nodes[nid].kind
            hist[kind] = hist.get(kind, 0) + 1
        return hist

    def summary(self) -> str:
        parts = [f"{k}:{v}" for k, v in sorted(self.gate_histogram().items())]
        return (f"Circuit({self.name}, PIs={len(self.pi_nodes)}, "
                f"POs={len(self.po_nodes)}, gates={len(self.gate_nodes)} "
                f"[{', '.join(parts)}])")

    def __repr__(self) -> str:
        return self.summary()

    # ------------------------------------------------------------------
    # Node inspection
    # ------------------------------------------------------------------

    def _node_by_ref(self, ref) -> int | None:
        """Resolve a node id or signal/net name to a node id.

        A net drives one node, but its name is shared with the PO sink; the
        producing (non-PO) node is preferred.
        """
        s = str(ref).strip()
        if s.lstrip("-").isdigit() and int(s) in self.nodes:
            return int(s)
        fallback = None
        for nid, node in self.nodes.items():
            if node.net == s:
                if not node.is_po:
                    return nid
                fallback = nid
        return fallback

    @staticmethod
    def _net_group(net: str) -> str:
        """Extract a group label from a net name by stripping trailing digits.

        ``csa_tree_add_12_51_groupi_n_4138`` → ``csa_tree_add_12_51_groupi_n_*``
        ``add_10_21_n_51`` → ``add_10_21_n_*``
        """
        m = re.match(r'^(.*[^\d])?\d+$', net)
        if m:
            base = m.group(1) or net.rstrip('0123456789')
            return base.rstrip('_') + '_*'
        return net

    def _match_nodes(self, ref: str) -> dict | None:
        """Try to match *ref* as a bus base name, prefix, or wildcard pattern.

        Returns a dict with ``type`` and matched ``node_ids`` / ``groups``,
        or ``None`` when nothing matches.
        """
        s = ref.strip()
        has_wildcard = '*' in s or '?' in s

        if has_wildcard:
            # --- wildcard / glob match ---------------------------------
            regex = '^' + re.escape(s).replace(r'\*', '.*').replace(r'\?', '.') + '$'
            pat = re.compile(regex)
            groups: dict[str, list[int]] = {}
            for nid, node in self.nodes.items():
                if node.net and not node.is_po and pat.match(node.net):
                    groups.setdefault(self._net_group(node.net), []).append(nid)
            if not groups:
                return None
            # flatten for easy counting
            all_ids = [n for g in groups.values() for n in g]
            return {"type": "wildcard", "pattern": s,
                    "node_ids": all_ids, "groups": groups}

        # --- bus base name: "out1" → out1[0], out1[1], ... ------------
        if '[' not in s:
            bits: dict[int, int] = {}
            bus_pfx = s + "["
            for nid, node in self.nodes.items():
                if node.net.startswith(bus_pfx) and not node.is_po:
                    rest = node.net[len(bus_pfx):]
                    if rest.endswith(']'):
                        try:
                            idx = int(rest[:-1])
                            bits[idx] = nid
                        except ValueError:
                            pass
            if bits:
                lo, hi = min(bits), max(bits)
                node_ids = [bits[i] for i in range(lo, hi + 1) if i in bits]
                kinds: dict[str, int] = {}
                for nid in node_ids:
                    k = self.nodes[nid].kind
                    kinds[k] = kinds.get(k, 0) + 1
                # determine I/O role
                role = ""
                if any(n.net in self.output_nets for n in (self.nodes[i] for i in node_ids)):
                    role = "output"
                elif any(n.net in self.input_nets for n in (self.nodes[i] for i in node_ids)):
                    role = "input"
                return {"type": "bus", "name": s,
                        "width": len(node_ids), "range": f"{lo}..{hi}",
                        "node_ids": node_ids, "kinds": kinds, "role": role}

        # --- prefix match (no wildcard, no bus match) -----------------
        groups: dict[str, list[int]] = {}
        for nid, node in self.nodes.items():
            if node.net.startswith(s) and not node.is_po:
                groups.setdefault(self._net_group(node.net), []).append(nid)
        if groups:
            all_ids = [n for g in groups.values() for n in g]
            return {"type": "prefix", "pattern": s,
                    "node_ids": all_ids, "groups": groups}

        return None

    def _describe_bus(self, info: dict, detail: bool) -> str:
        """Format a bus summary string from :meth:`_match_nodes` result."""
        lines = [
            f"Bus {info['name']}[{info['range']}]"
            f" ({info['width']} bits{', ' + info['role'] if info['role'] else ''})",
            f"    driver kinds: "
            + ', '.join(f"{k}×{v}" for k, v in sorted(info['kinds'].items())),
        ]
        # show example bit nets (more when detail=True)
        limit = len(info['node_ids']) if detail else min(6, len(info['node_ids']))
        examples = [self.nodes[nid].net for nid in info['node_ids'][:limit]]
        suffix = ', ...' if limit < len(info['node_ids']) else ''
        lines.append(f"    example nets: {', '.join(examples)}{suffix}")
        lines.append(
            f'    Use "{info["name"]}[N]" for individual bit details.')
        return '\n'.join(lines)

    def _describe_search(self, info: dict, detail: bool) -> str:
        """Format a prefix / wildcard search summary."""
        groups = info['groups']
        total = len(info['node_ids'])
        lines = [f'Pattern "{info["pattern"]}" matched {total} signals'
                 f' in {len(groups)} group{"s" if len(groups) != 1 else ""}:']
        LIMIT = 24 if detail else 8
        for gname, gids in sorted(groups.items(), key=lambda x: -len(x[1])):
            gkinds: dict[str, int] = {}
            for nid in gids:
                k = self.nodes[nid].kind
                gkinds[k] = gkinds.get(k, 0) + 1
            kinds_str = ', '.join(f"{k}×{v}" for k, v in sorted(gkinds.items()))
            lines.append(f"\n  {gname} ({len(gids)} signals)")
            lines.append(f"    gates: {kinds_str}")
            shown = gids[:LIMIT]
            nets = []
            for nid in shown:
                net = self.nodes[nid].net
                nets.append(net if len(net) <= 40 else net[:37] + '...')
            lines.append(f"    example nets: {', '.join(nets)}"
                         + (', ...' if len(gids) > LIMIT else ''))
        lines.append(
            '\nUse an exact net name (e.g. from "example nets" above)'
            ' for individual inspection.')
        return '\n'.join(lines)

    def describe_node(self, ref, depth: int = 2,
                      detail: bool = False) -> str:
        """Human-/LLM-readable description of a node, bus, or signal group.

        *ref* is a node id, a signal name, a bus base name (e.g. ``"out1"``),
        or a wildcard pattern (``"csa_tree_*"``, ``"*adder*"``).

        For a single node: reports its kind, fan-in / fan-out cones up to
        *depth* levels.  For a bus: shows width and driver statistics.
        For a pattern: lists matching signal groups with example nets.

        *depth* caps the traversal for single-node mode; *detail=True* shows
        all neighbours (otherwise at most 16 per level).
        """
        nid = self._node_by_ref(ref)
        if nid is not None:
            return self._describe_single_node(nid, depth, detail)

        # try bus / prefix / wildcard match
        info = self._match_nodes(ref)
        if info is None:
            raise KeyError(f"no node matching {ref!r}")
        if info["type"] == "bus":
            return self._describe_bus(info, detail)
        else:
            return self._describe_search(info, detail)

    def _describe_single_node(self, nid: int, depth: int,
                              detail: bool) -> str:
        """Format the description of a single node (extracted for reuse)."""
        node = self.nodes[nid]

        def label(i: int) -> str:
            n = self.nodes[i]
            return f"{n.net or f'#{i}'} ({n.kind})"

        lines = [
            f"Node {label(nid)}",
            f"    id      : {nid}",
            f"    kind    : {node.kind}",
            f"    net     : {node.net or '(unnamed)'}",
        ]
        if node.cell is not None:
            lines.append(f"    function: {node.cell.function}")
        lines.append(f"    fan-in  : {len(node.inputs)}")
        lines.append(f"    fan-out : {len(node.fanouts)}")

        LIMIT = 16

        def walk(i: int, d: int, attr: str, indent: int, out: list[str]) -> None:
            if d == 0:
                return
            neighbours = getattr(self.nodes[i], attr)
            shown = neighbours if detail else neighbours[:LIMIT]
            for j in shown:
                out.append("    " * indent + f"- {label(j)}")
                walk(j, d - 1, attr, indent + 1, out)
            if not detail and len(neighbours) > LIMIT:
                out.append("    " * indent
                           + f"- (+{len(neighbours) - LIMIT} more)")

        up: list[str] = []
        walk(nid, depth, "inputs", 1, up)
        down: list[str] = []
        walk(nid, depth, "fanouts", 1, down)
        lines.append(f"\n    fan-in cone (up to {depth} levels):")
        lines += up or ["        (none - primary input or constant)"]
        lines.append(f"\n    fan-out cone (up to {depth} levels):")
        lines += down or ["        (none - primary output)"]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Cone traversal
    # ------------------------------------------------------------------

    def _cone_reachable(self, start_ids: list[int], direction: str,
                        stop_ids=frozenset(),
                        depth: int = -1) -> tuple[dict[int, int], dict[int, list[int]]]:
        """Core cone walk shared by :meth:`find_cone` and subgraph extraction.

        BFS from *start_ids* along fan-in (``backward``) or fan-out
        (``forward``).  A node is a *boundary* — reached but not expanded — when
        it is a PI/PO or in *stop_ids* (start nodes are always expanded).
        Traversal also stops past *depth* levels (-1 = unlimited).

        Returns:
            ``dist``: ``node_id -> distance`` from the start set (start = 0), in
            BFS discovery order (dict insertion order).
            ``parents``: ``node_id -> [neighbour ids]`` one hop toward the start
            (every converging path, deduped).
        """
        if direction == "backward":
            def nbr(nid: int) -> list[int]:
                return self.nodes[nid].inputs
        else:
            def nbr(nid: int) -> list[int]:
                return self.nodes[nid].fanouts

        start_set = set(start_ids)
        dist: dict[int, int] = {s: 0 for s in start_ids}
        parents: dict[int, list[int]] = {}
        frontier: list[int] = list(start_ids)
        while frontier:
            cur = frontier.pop(0)
            d = dist[cur]
            if cur not in start_set:
                node = self.nodes[cur]
                if node.is_pi or node.is_po or cur in stop_ids:
                    continue  # boundary: included but not expanded
            if depth >= 0 and d >= depth:
                continue
            for nb in nbr(cur):
                if nb not in dist:
                    dist[nb] = d + 1
                    frontier.append(nb)
                # Edge nb<-cur when cur is strictly closer to the start: cur is
                # then a toward-start neighbour of nb (capture all paths, deduped).
                if dist[nb] > d and cur not in parents.get(nb, ()):
                    parents.setdefault(nb, []).append(cur)
        return dist, parents

    def find_cone(self, signals: list[str], direction: str,
                  stop_at: list[str] | None = None,
                  depth: int = -1) -> dict:
        """BFS from *signals* through fan-in (``backward``) or fan-out
        (``forward``), returning layer-by-layer structured data.

        Traversal stops at nodes matched by *stop_at* (included but not
        expanded) or when *depth* levels have been visited (0 = start
        signals only; -1 = unlimited).

        Returns a dict with:
        - ``ok`` (bool), ``direction`` (str)
        - ``start``: resolved node ids for the start signals
        - ``stop``: resolved node ids for the stop_at signals (if any)
        - ``layers``: list of ``{depth, node_ids, count}``, one per level
        - ``boundary``: leaf nodes broken down by why traversal stopped
        - ``parents``: ``{node_id: [neighbour ids]}`` — for each visited node,
          its neighbour(s) one step *toward* the start signals (i.e. the nodes
          it was reached from).  Lets a caller rebuild the connection paths.
        - ``total_nodes``: total internal nodes visited (excludes start)
        - ``truncated``: whether the depth cap was hit
        """
        if direction not in ("backward", "forward"):
            raise ValueError(
                f"direction must be 'backward' or 'forward', got {direction!r}")

        start_ids = [self._node_by_ref(s) for s in signals]
        bad = [s for s, n in zip(signals, start_ids) if n is None]
        if bad:
            raise KeyError(f"unresolved signal(s) in 'signals': {bad}")

        stop_at = stop_at or []
        stop_ids: set[int] = set()
        for s in stop_at:
            n = self._node_by_ref(s)
            if n is None:
                raise KeyError(f"unresolved signal in 'stop_at': {s!r}")
            stop_ids.add(n)

        # Shared traversal core (also used by subgraph extraction).
        dist, parents = self._cone_reachable(start_ids, direction, stop_ids, depth)
        start_set = set(start_ids)

        # Group reachable nodes into layers by distance (layer 0 = the start
        # signals, excluded here). dict insertion order is BFS discovery order.
        layers_raw: dict[int, list[int]] = {}
        for nid, d in dist.items():
            if d > 0:
                layers_raw.setdefault(d, []).append(nid)
        layers = [{"depth": d, "node_ids": layers_raw[d], "count": len(layers_raw[d])}
                  for d in sorted(layers_raw)]
        total = sum(l["count"] for l in layers)

        # Boundary = reached-but-not-expanded nodes, grouped by why we stopped.
        pi_hit: list[int] = []
        stop_hit: list[int] = []
        for nid in dist:
            if nid in start_set:
                continue
            node = self.nodes[nid]
            if node.is_pi or node.is_po:
                pi_hit.append(nid)
            elif nid in stop_ids:
                stop_hit.append(nid)
        boundary: dict[str, list[int]] = {}
        if pi_hit:
            boundary["pi_po"] = pi_hit
        if stop_hit:
            boundary["stop_at"] = stop_hit

        truncated = depth >= 0 and any(d >= depth for d in layers_raw)

        return {
            "ok": True,
            "direction": direction,
            "start": start_ids,
            "stop": sorted(stop_ids),
            "depth_cap": depth,
            "layers": layers,
            "boundary": boundary,
            "parents": parents,
            "total_nodes": total,
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    # DOT / Graphviz export
    # ------------------------------------------------------------------

    @staticmethod
    def _dot_escape(label: str) -> str:
        """Escape special characters for a DOT quoted string."""
        return label.replace("\\", "\\\\").replace('"', '\\"')

    def to_dot(self, filename: str) -> None:
        """Write a Graphviz DOT file representing the circuit DAG.

        * PI nodes are light steel-blue **boxes**.
        * PO nodes are light salmon **boxes**.
        * Constant nodes are gray **diamonds**.
        * Library gates are coloured **ellipses** (distinct pastel per gate type).

        Render with: ``dot -Tpng filename -o out.png``
        """
        with open(filename, "w") as f:
            f.write(f"digraph {self._dot_escape(self.name)} {{\n")
            f.write("  rankdir=LR;\n")
            f.write('  node [fontname="monospace", fontsize=10];\n')
            f.write("\n")

            pi_ids: list[str] = []
            po_ids: list[str] = []
            palette = _make_palette(set(self.gate_histogram()))

            for nid, node in self.nodes.items():
                if node.is_pi:
                    pi_ids.append(f"n{nid}")
                    label = self._dot_escape(node.net or "PI")
                    f.write(
                        f'  n{nid} [label="{label}", shape=box, style=filled, '
                        f'fillcolor="#D4E6F1"];\n'
                    )
                elif node.is_po:
                    po_ids.append(f"n{nid}")
                    label = self._dot_escape(node.net or "PO")
                    f.write(
                        f'  n{nid} [label="{label}", shape=box, style=filled, '
                        f'fillcolor="#FADBD8"];\n'
                    )
                elif node.kind in (CONST0, CONST1, CONSTX):
                    f.write(
                        f'  n{nid} [label="{node.kind}", shape=diamond, '
                        f'style=filled, fillcolor="#F0F0F0"];\n'
                    )
                else:  # library gate
                    color = palette.get(node.kind, "#FFFFFF")
                    f.write(
                        f'  n{nid} [label="{node.kind}", shape=ellipse, '
                        f'style=filled, fillcolor="{color}"];\n'
                    )

            # Rank constraints — PI on the left, PO on the right.
            f.write("\n")
            if pi_ids:
                f.write(f'  {{rank=source; {" ".join(pi_ids)}}}\n')
            if po_ids:
                f.write(f'  {{rank=sink; {" ".join(po_ids)}}}\n')

            # Edges — source node -> consuming node.
            f.write("\n")
            for nid, node in self.nodes.items():
                for src in node.inputs:
                    f.write(f"  n{src} -> n{nid};\n")

            f.write("}\n")
