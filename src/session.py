"""Stateful, framework-agnostic service layer.

:class:`CircuitSession` holds the "current circuit" and parses file paths into
:class:`Circuit` objects (dispatching on extension).  It takes plain strings and
returns plain dicts, so any frontend (MCP, CLI, HTTP) can sit on top.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .circuit import Circuit, extract_adders, extract_xor


# ---------------------------------------------------------------------------
#  Modification record
# ---------------------------------------------------------------------------

@dataclass
class ModificationRecord:
    """A single code-modification step.  Only the *forward* diff is stored
    (matches -> replacements); the full code is never duplicated.
    Reverts replay from the original source.

    Three edit modes are supported:
    - ``"rewrite"`` — entire source replaced; *rewrite* holds the new content.
    - ``"region"``  — *begin*/*end* regex delimit a region to replace.
    - ``"match"``   — exact-string *matches* → *replacements* (default).
    """
    id: int
    matches: list[str]
    replacements: list[str]
    cost_before: int
    cost_after: int
    success: bool = False
    error: str = ""
    timestamp: float = 0.0
    # Region / rewrite helpers
    mode: str = "match"
    rewrite: str = ""
    begin: str = ""
    end: str = ""

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


# File extensions we know how to parse.
_VERILOG_EXTS = {".v", ".sv", ".verilog"}
_AIGER_EXTS = {".aig", ".aag"}

# Matches a bit-slice net name like ``IN1[3]`` -> ("IN1", 3).
_BIT_RE = re.compile(r"^(.*)\[(\d+)\]$")

# Upper bound on random patterns per call, to keep a runaway request bounded.
_MAX_PATTERNS = 100_000


def _word_pattern(pat: str) -> re.Pattern:
    """Compile *pat* into a regex anchored with ``\\b`` where it touches word chars."""
    esc = re.escape(pat)
    if pat and (pat[0].isalnum() or pat[0] == "_"):
        esc = r"\b" + esc
    if pat and (pat[-1].isalnum() or pat[-1] == "_"):
        esc = esc + r"\b"
    return re.compile(esc)


def _unescape(s: str) -> str:
    """Convert literal backslash escapes (``\\n``, ``\\t``) to real chars."""
    return s.encode().decode("unicode_escape")


def _truncate(s: str, n: int) -> str:
    """Return *s* truncated to at most *n* chars, with ``...`` if cut."""
    s = s.replace("\n", "\\n")
    if len(s) <= n:
        return s
    return s[:n-1] + "..."


def _split_bit(net: str) -> tuple[str, int | None]:
    """Split ``base[idx]`` into ``(base, idx)``; scalars give ``(net, None)``."""
    m = _BIT_RE.match(net)
    if m:
        return m.group(1), int(m.group(2))
    return net, None


def _bus_fields(nets: list[str], net_to_node: dict[str, int]) -> list[dict]:
    """Group a net list into display fields (one per bus / scalar).

    Preserves first-appearance order of bus bases.  Each field carries the
    ``(bit_index, node_id)`` pairs needed to reconstruct an integer value.
    """
    order: list[str] = []
    buses: dict[str, list[tuple[int, int, str]]] = {}
    scalars: set[str] = set()
    for net in nets:
        base, i = _split_bit(net)
        if base not in buses:
            order.append(base)
            buses[base] = []
        if i is None:
            scalars.add(base)
            buses[base].append((0, net_to_node[net], net))
        else:
            buses[base].append((i, net_to_node[net], net))

    fields: list[dict] = []
    for base in order:
        triples = sorted(buses[base])
        width = 1 if base in scalars else (triples[-1][0] + 1)
        fields.append({
            "label": base, "group": "io", "width": width,
            "bits": [(idx, nid) for idx, nid, _ in triples],
            "nets": [net for _, _, net in triples],
        })
    return fields


def _field_value(field: dict, value: dict[int, int]) -> int:
    """Reconstruct a field's integer value from per-node bit values."""
    return sum((value[nid] & 1) << idx for idx, nid in field["bits"])


def _input_drive(in_fields: list[dict],
                 fixed_bits: dict[str, int]) -> tuple[str, list[str]]:
    """Describe how each input bus is driven this run.

    Returns ``(summary, partial_bases)`` where *summary* annotates every input
    bus as ``= <value>`` (fully pinned), ``partial (k/w bits fixed)`` or
    ``random``, and *partial_bases* lists the bases that are only partially
    constrained — those warrant an explicit NOTE so the caller is not misled
    into reading their columns as a chosen value.
    """
    if not in_fields:
        return "(none)", []
    parts: list[str] = []
    partials: list[str] = []
    for f in in_fields:
        nets, label, width = f["nets"], f["label"], f["width"]
        fixed_idx = [(idx, fixed_bits[net])
                     for (idx, _), net in zip(f["bits"], nets)
                     if net in fixed_bits]
        if not fixed_idx:
            parts.append(f"{label}[{width}] random")
        elif len(fixed_idx) == len(nets):
            val = sum(bit << idx for idx, bit in fixed_idx)
            parts.append(f"{label}[{width}] = {val}")
        else:
            parts.append(
                f"{label}[{width}] partial ({len(fixed_idx)}/{len(nets)} fixed)")
            partials.append(label)
    return ", ".join(parts), partials


def _field_summary(fields: list[dict]) -> str:
    """One-line ``base[width]`` listing for a group of fields."""
    if not fields:
        return "(none)"
    return ", ".join(
        f"{f['label']}[{f['width']}]" if "width" in f else f["label"]
        for f in fields
    )


def _compact_bus(names: list[str]) -> str:
    """Group bit-nets into ``base[lo..hi] (n)`` form; scalars listed as-is."""
    bus: dict[str, list[int]] = {}
    scalars: list[str] = []
    for n in names:
        m = _BIT_RE.match(n)
        if m:
            bus.setdefault(m.group(1), []).append(int(m.group(2)))
        else:
            scalars.append(n)
    parts: list[str] = []
    for b, idx in sorted(bus.items()):
        idx.sort()
        parts.append(f"{b}[{idx[0]}..{idx[-1]}] ({len(idx)})")
    parts.extend(sorted(scalars))
    return ", ".join(parts) if parts else "(none)"


def _render_table(cols: list[dict], n: int) -> str:
    """Render aligned, group-separated columns into a fixed-width table."""
    widths = [max(len(c["label"]), *(len(v) for v in c["values"]), 1)
              for c in cols]

    def line(cells: list[str]) -> str:
        parts: list[str] = []
        prev: str | None = None
        for col, w, cell in zip(cols, widths, cells):
            if prev is not None and col["group"] != prev:
                parts.append("|")
            parts.append(cell.rjust(w))
            prev = col["group"]
        return "  ".join(parts)

    header = line([c["label"] for c in cols])
    body = [line([c["values"][k] for c in cols]) for k in range(n)]
    return "\n".join([header, "-" * len(header), *body])


class NoCircuitLoadedError(RuntimeError):
    """Raised when an operation needs a circuit but none has been loaded."""


class CircuitSession:
    """Holds the currently loaded circuit and the operations over it."""

    def __init__(self) -> None:
        self.circuit: Circuit | None = None
        self.source: str | None = None

        # Code-modification workflow
        self._source_code: str = ""
        self._current_code: str = ""
        self._modifications: list[ModificationRecord] = []
        self._mod_counter: int = 0
        self._original_gate_count: int = 0

    # -- operations -----------------------------------------------------

    def load(self, path: str) -> str:
        """Parse *path* into the current circuit and return its summary.

        Dispatches on the file extension: ``.v``/``.sv``/``.verilog`` are read
        as gate-level Verilog (primitive gates only), ``.aig``/``.aag``
        as AIGER.  Raises ``FileNotFoundError`` / ``ValueError`` for a missing
        path or an unsupported extension.
        """
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no such file: {path}")

        ext = os.path.splitext(path)[1].lower()
        if ext in _VERILOG_EXTS:
            circuit = Circuit.from_file(path)
            fmt = "verilog"
        elif ext in _AIGER_EXTS:
            circuit = Circuit.from_aig_file(path)
            fmt = "aiger"
        else:
            supported = ", ".join(sorted(_VERILOG_EXTS | _AIGER_EXTS))
            raise ValueError(
                f"unsupported file type {ext!r} for {path!r}; "
                f"expected one of: {supported}"
            )

        self.circuit = circuit
        self.source = path

        # Store raw source code for the modification workflow
        if ext in _VERILOG_EXTS:
            self._source_code = open(path, encoding="utf-8").read()
            self._current_code = self._source_code
            self._original_gate_count = len(circuit.gate_nodes)
        else:
            self._source_code = ""
            self._current_code = ""
            self._original_gate_count = len(circuit.gate_nodes)
        self._modifications = []
        self._mod_counter = 0

        return self._load_report(fmt)

    def _load_report(self, fmt: str) -> str:
        c = self.circuit
        assert c is not None

        def compact(nets: list[str]) -> str:
            import re
            bus: dict[str, list[int]] = {}
            scalars: list[str] = []
            for n in nets:
                m = re.match(r"(.+)\[(\d+)\]$", n)
                if m:
                    bus.setdefault(m.group(1), []).append(int(m.group(2)))
                else:
                    scalars.append(n)
            parts = []
            for b, idx in bus.items():
                idx.sort()
                parts.append(f"{b}[{idx[0]}..{idx[-1]}] ({len(idx)})")
            parts.extend(scalars)
            return ", ".join(parts) if parts else "(none)"

        pi = compact(c.input_nets)
        po = compact(c.output_nets)
        verilog = fmt == "verilog"
        naming = (
            "    Internal signal names are Yosys-generated wires (e.g. _0001_) "
            "from the netlist source."
            if verilog else
            "    Internal signal names are synthetic (e.g. n33 for AND gates, "
            "n33_n for their inverters)."
        )
        return "\n".join([
            f"Loaded {fmt} file {self.source}",
            f"    {c.summary()}",
            f"    Input ports ({len(c.input_nets)})  : {pi}",
            f"    Output ports ({len(c.output_nets)}) : {po}",
            naming,
        ])

    def _current(self) -> Circuit:
        if self.circuit is None:
            raise NoCircuitLoadedError(
                "no circuit loaded; call read_file(path) first"
            )
        return self.circuit

    def summary(self) -> str:
        """One-line summary of the current circuit, or raise if none is loaded."""
        return self._current().summary()

    def xor_stats(self, detail: bool = False) -> str:
        """Extract XOR gates from the current circuit and report chain structure."""
        return extract_xor(self._current()).report(detail=detail)

    def adder_stats(self, detail: bool = False) -> str:
        """Extract half/full adders and report the largest adder tree."""
        return extract_adders(self._current()).report(detail=detail)

    def node_info(self, ref: str, depth: int = 2, detail: bool = False) -> str:
        return self._current().describe_node(ref, depth, detail=detail)

    # -- simulation -----------------------------------------------------

    def simulate(self, pattern_num: int = 100,
                 fixed_inputs: dict[str, int] | None = None,
                 watch: list[str] | None = None,
                 seed: int | None = None) -> str:
        """Simulate the current circuit and report bus values across patterns.

        Args:
            pattern_num: number of random input patterns to apply (default 100).
                Ignored (forced to 1) when *fixed_inputs* pins every input bit.
            fixed_inputs: input values to hold constant across all patterns.
                Keys may be a bus base (``"IN1"`` -> integer spread MSB..LSB over
                its bits), a single bit (``"IN1[0]"``), or a scalar port. Bits
                not pinned here are randomised each pattern.
            watch: extra internal signals to include in the report. Each entry
                is a single net (``"_0007_"``, ``"n33"``) shown as its raw bit,
                or an internal bus base (``"sum"``) whose bits are gathered into
                an integer.
            seed: optional RNG seed for reproducible random patterns.

        Returns:
            A text report: one row per pattern with each input/output bus (and
            any watched signal) rendered as a decimal integer. The header
            annotates each input bus as fixed/partial/random; partially
            constrained buses get an explicit NOTE.

        Raises:
            ValueError: for an unknown input name, a bit value other than 0/1,
                a bus value that overflows its width, or an out-of-bounds
                ``pattern_num``. The message names the offending input and the
                allowed range so the caller can correct the request.
        """
        c = self._current()

        # net -> driver node id (prefer the producing node over the PO sink).
        net_to_node: dict[str, int] = {}
        for nid, node in c.nodes.items():
            if not node.net:
                continue
            if node.net not in net_to_node or not node.is_po:
                net_to_node[node.net] = nid

        pi_nets = c.input_nets
        fixed_bits = self._expand_fixed(fixed_inputs or {}, pi_nets)
        all_fixed = len(fixed_bits) == len(pi_nets)

        if pattern_num < 1:
            raise ValueError(f"pattern_num must be >= 1 (got {pattern_num})")
        if pattern_num > _MAX_PATTERNS:
            raise ValueError(
                f"pattern_num {pattern_num} exceeds the cap of {_MAX_PATTERNS}; "
                f"request fewer patterns")
        n = 1 if all_fixed else pattern_num

        in_fields = _bus_fields(pi_nets, net_to_node)
        out_fields = _bus_fields(c.output_nets, net_to_node)
        watch_fields = self._watch_fields(watch or [], net_to_node)

        rng = random.Random(seed)
        idx_col = {"label": "#", "group": "idx", "values": []}
        for field in in_fields + out_fields + watch_fields:
            field["values"] = []

        for k in range(n):
            inp = {net: (fixed_bits[net] if net in fixed_bits else rng.randint(0, 1))
                   for net in pi_nets}
            value = c.simulate_values(inp)
            idx_col["values"].append(str(k))
            for field in in_fields + out_fields + watch_fields:
                field["values"].append(str(_field_value(field, value)))

        # Per-bus drive status (fixed value / partial / random) + overall mode.
        in_desc, partials = _input_drive(in_fields, fixed_bits)
        if all_fixed:
            mode = "fixed"
        elif partials or fixed_bits:
            mode = "random (partial constraints)"
        else:
            mode = "random"

        cols = [idx_col] + in_fields + out_fields + watch_fields
        lines = [
            f"Simulation report — {self.source}",
            f"    patterns : {n} ({mode})",
            f"    inputs   : {in_desc}",
            f"    outputs  : {_field_summary(out_fields)}",
        ]
        if watch_fields:
            lines.append(f"    watch    : {_field_summary(watch_fields)}")
        if seed is not None and not all_fixed:
            lines.append(f"    seed     : {seed}")
        for base in partials:
            lines.append(
                f"    NOTE: input bus {base!r} is only partially constrained — "
                f"its unspecified bits are randomised on every pattern.")
        lines.append("")
        lines.append(_render_table(cols, n))
        return "\n".join(lines)

    def _expand_fixed(self, fixed: dict[str, int],
                      pi_nets: list[str]) -> dict[str, int]:
        """Expand ``fixed`` (bus/bit/scalar keys) into a net -> bit map.

        Validates every key and value, raising :class:`ValueError` with an
        actionable message rather than silently masking or truncating: unknown
        signals, non-0/1 bit values, and bus integers that overflow the bus
        width are all rejected.
        """
        pi_set = set(pi_nets)
        buses: dict[str, dict[int, str]] = {}     # bus base -> {idx: net}
        scalars: list[str] = []
        for net in pi_nets:
            base, i = _split_bit(net)
            if i is None:
                scalars.append(net)
            else:
                buses.setdefault(base, {})[i] = net

        bits: dict[str, int] = {}
        for key, raw in fixed.items():
            try:
                val = int(raw)
            except (TypeError, ValueError):
                raise ValueError(
                    f"value {raw!r} for input {key!r} is not an integer")
            base, i = _split_bit(key)

            if i is not None:                       # explicit single bit
                if key not in pi_set:
                    raise ValueError(self._unknown_input_msg(key, buses, scalars))
                if val not in (0, 1):
                    raise ValueError(
                        f"value {val} for input bit {key!r} is not a single bit "
                        f"(allowed 0 or 1)")
                bits[key] = val
            elif key in pi_set:                     # scalar primary input
                if val not in (0, 1):
                    raise ValueError(
                        f"value {val} for scalar input {key!r} is not a single "
                        f"bit (allowed 0 or 1)")
                bits[key] = val
            elif key in buses:                      # bus base -> spread integer
                width = max(buses[key]) + 1
                hi = (1 << width) - 1
                if not (0 <= val <= hi):
                    raise ValueError(
                        f"value {val} for input bus {key!r} is out of range for "
                        f"its {width}-bit width (allowed 0..{hi})")
                for idx, net in buses[key].items():
                    bits[net] = (val >> idx) & 1
            else:
                raise ValueError(self._unknown_input_msg(key, buses, scalars))
        return bits

    @staticmethod
    def _unknown_input_msg(key: str, buses: dict[str, dict[int, str]],
                           scalars: list[str]) -> str:
        """Build an actionable 'unknown input' error listing the real inputs."""
        bus_list = ", ".join(f"{b}[{max(idx) + 1}]"
                             for b, idx in buses.items()) or "(none)"
        msg = (f"{key!r} is not a primary input of this circuit. "
               f"Available input buses: {bus_list}.")
        if scalars:
            msg += f" Scalar inputs: {', '.join(scalars)}."
        msg += (" Use a bus base (e.g. 'IN1') for a word value, or a single bit "
                "(e.g. 'IN1[0]').")
        return msg

    def _watch_fields(self, refs: list[str],
                      net_to_node: dict[str, int]) -> list[dict]:
        """Resolve watch refs (single nets or internal bus bases) to fields."""
        fields: list[dict] = []
        for ref in refs:
            _, i = _split_bit(ref)
            if ref in net_to_node:                  # single named net / bit
                fields.append({"label": ref, "group": "watch",
                               "bits": [(0, net_to_node[ref])]})
                continue
            if i is None:                           # maybe an internal bus base
                bits = {}
                for net, nid in net_to_node.items():
                    b, j = _split_bit(net)
                    if b == ref and j is not None:
                        bits[j] = nid
                if bits:
                    fields.append({"label": ref, "group": "watch",
                                   "bits": sorted(bits.items()),
                                   "width": max(bits) + 1})
                    continue
            raise KeyError(f"no signal matching {ref!r} in the current circuit")
        return fields

    def cone_report(self, signals: list[str], direction: str,
                    stop_at: list[str] | None = None,
                    depth: int = -1,
                    detail: bool = False) -> str:
        """Report the forward or backward cone of *signals* in structured layers.

        Args:
            signals: starting signal names (nets, node ids, or port names).
            direction: ``"backward"`` (trace fan-in) or ``"forward"`` (trace fan-out).
            stop_at: optional signal names where traversal halts (included but not
                expanded further).
            depth: maximum levels to traverse (-1 = unlimited).  Layer 0 is the
                start signals themselves.
            detail: when True, lists every node at every layer (with its logic
                function and full connection list) without any truncation.
                When False (default), each layer is capped at 16 entries, the
                boundary at 32 entries, and only the first 4 and last 3 layers
                are shown — intermediate layers are elided with a count line.

        Returns:
            A multiline text report structured for LLM comprehension: header,
            per-layer listings, boundary-signal breakdown, and a summary.  Each
            node line carries its gate type, fan-in/out, and a ``from:`` list of
            the neighbour(s) one hop toward the start signals (so connection
            paths can be rebuilt); each layer is prefixed with a gate-type
            histogram.
        """
        c = self._current()
        data = c.find_cone(signals, direction, stop_at=stop_at, depth=depth)

        LIMIT = 16
        LAYER_HEAD = 4      # show first N layers in full
        LAYER_TAIL = 3      # show last N layers in full

        def _label(nid: int) -> str:
            n = c.nodes[nid]
            net = n.net or f"#{nid}"
            return f"{net} ({n.kind})"

        def _compact_nets(nids: list[int]) -> str:
            """Group nets like ``io_a[0..10] (11)`` for readability."""
            bus: dict[str, list[int]] = {}
            scalars: list[str] = []
            for nid in nids:
                net = c.nodes[nid].net or ""
                m = re.match(r"(.+)\[(\d+)\]$", net)
                if m:
                    bus.setdefault(m.group(1), []).append(int(m.group(2)))
                else:
                    scalars.append(net)
            parts: list[str] = []
            for b, idx in sorted(bus.items()):
                idx.sort()
                parts.append(f"{b}[{idx[0]}..{idx[-1]}] ({len(idx)})")
            parts.extend(sorted(scalars))
            return ", ".join(parts) if parts else "(none)"

        # -- header -------------------------------------------------------
        start_names = [c.nodes[i].net or c.nodes[i].kind for i in data["start"]]
        start_list = ", ".join(start_names[:8])
        if len(start_names) > 8:
            start_list += f", ... (+{len(start_names) - 8} more)"

        lines = [
            f"Cone traversal report — {self.source}",
            f"    direction  : {data['direction']}",
            f"    start from : {start_list} ({len(data['start'])} signals)",
        ]
        if stop_at:
            stop_names = [c.nodes[i].net or c.nodes[i].kind for i in data["stop"]]
            lines.append(
                f"    stop at    : {', '.join(stop_names[:8])}"
                + (f", ... (+{len(stop_names) - 8} more)"
                   if len(stop_names) > 8 else "")
                + f" ({len(data['stop'])} signals)"
            )
        else:
            direction_stop = "PIs" if direction == "backward" else "POs"
            lines.append(
                f"    stop at    : (none — trace until {direction_stop})"
            )
        cap_str = str(depth) if depth >= 0 else "unlimited"
        lines.append(f"    depth cap  : {cap_str}")
        lines.append(
            "    edges      : every node lists 'from:' its neighbour(s) one step "
            "toward the start signals (follow them to rebuild a path)")
        lines.append("")

        # -- per-node / per-layer rendering helpers -----------------------
        parents = data["parents"]

        def _net(nid: int) -> str:
            return c.nodes[nid].net or f"#{nid}"

        def _from(nid: int) -> str:
            """The node's neighbour(s) one hop toward the start signals."""
            ps = parents.get(nid, [])
            if not ps:
                return "(start-adjacent)"
            lim = ps if detail else ps[:3]
            s = ", ".join(_net(p) for p in lim)
            if len(ps) > len(lim):
                s += f", +{len(ps) - len(lim)}"
            return s

        def _hist(nids: list[int]) -> str:
            """Gate-type histogram for a layer, most frequent first."""
            h: dict[str, int] = {}
            for nid in nids:
                k = c.nodes[nid].kind
                h[k] = h.get(k, 0) + 1
            return ", ".join(f"{k}×{v}"
                             for k, v in sorted(h.items(), key=lambda kv: -kv[1]))

        def _emit(nid: int, indent: str) -> None:
            node = c.nodes[nid]
            fn = f" [{node.cell.function}]" if (detail and node.cell) else ""
            lines.append(
                f"{indent}{_label(nid):<34s}{fn}  "
                f"fanin={len(node.inputs)} fanout={len(node.fanouts)}"
                f"  from: {_from(nid)}"
            )

        # -- layers -------------------------------------------------------
        all_layers = data["layers"]
        n_layers = len(all_layers)

        # Decide which layers to show.  detail=True → all; otherwise head + tail.
        if detail or n_layers <= LAYER_HEAD + LAYER_TAIL + 2:
            show_layers = list(all_layers)
            skipped_start = n_layers  # never skip
        else:
            show_layers = all_layers[:LAYER_HEAD] + all_layers[-LAYER_TAIL:]
            skipped_start = LAYER_HEAD
            skipped_end = n_layers - LAYER_TAIL
            skipped_count = skipped_end - skipped_start

        for layer in show_layers:
            d = layer["depth"]
            nids = layer["node_ids"]
            shown = nids if detail else nids[:LIMIT]
            lines.append(
                f"== Layer {d} (distance {d}) — {layer['count']} signals =="
            )
            lines.append(f"   gates: {_hist(nids)}")
            for nid in shown:
                _emit(nid, "  ")
            if not detail and len(nids) > LIMIT:
                lines.append(
                    f"  (+{len(nids) - LIMIT} more — use detail=True for full list)"
                )
            lines.append("")

        # Elide middle layers when compressed.
        if not detail and skipped_start < n_layers:
            skipped_mid = all_layers[skipped_start:skipped_end]
            skipped_nodes = sum(la["count"] for la in skipped_mid)
            lines.append(
                f"...  ({skipped_count} intermediate layers skipped, "
                f"{skipped_nodes} nodes — use detail=True for full output)  ..."
            )
            lines.append("")

        if not data["layers"]:
            lines.append("== (no internal layers traversed) ==\n")

        # -- boundary -----------------------------------------------------
        # Boundary signals are the conclusion of the trace, so show more of them
        # (BLIMIT) than the per-layer body before truncating.
        BLIMIT = 32
        bdry = data["boundary"]
        if bdry:
            lines.append("== Boundary signals ==")
            for kind, nids in bdry.items():
                if kind == "pi_po":
                    label = "primary inputs" if direction == "backward" else "primary outputs"
                    lines.append(f"  Reached {label} ({len(nids)}):")
                elif kind == "stop_at":
                    lines.append(f"  Reached stop_at ({len(nids)}):")
                shown = nids if detail else nids[:BLIMIT]
                for nid in shown:
                    _emit(nid, "    ")
                if not detail and len(nids) > BLIMIT:
                    lines.append(
                        f"    (+{len(nids) - BLIMIT} more — use detail=True for full list)"
                    )
                # Compact ranges
                lines.append(f"    Compact  : {_compact_nets(nids)}")
            lines.append("")

        # -- summary ------------------------------------------------------
        if data["truncated"]:
            lines.append(f"== ⚠ Depth cap ({depth} levels) reached — cone may extend further ==")
        lines.append(f"== Summary ==")

        total = data["total_nodes"]
        nlayers = len(data["layers"])
        nboundary = sum(len(v) for v in bdry.values())
        lines.append(
            f"  Traversed {nlayers} layers, {total} internal nodes, "
            f"{nboundary} boundary signals."
        )

        if bdry:
            for kind, nids in bdry.items():
                if kind == "pi_po":
                    label = "primary inputs" if direction == "backward" else "primary outputs"
                elif kind == "stop_at":
                    label = "stop_at"
                lines.append(f"  {label}: {_compact_nets(nids)}")

        return "\n".join(lines)


    # ------------------------------------------------------------------
    # Code-modification workflow: edit / revert / show / dump
    # ------------------------------------------------------------------

    def edit(
        self,
        matches: list[str] | None = None,
        replacements: list[str] | None = None,
        rewrite: str = "",
        begin: str = "",
        end: str = "",
    ) -> str:
        """Apply transformations to the current Verilog source code.

        Three modes are supported, evaluated in priority order:

        1. **Full rewrite** — when *rewrite* is non-empty.
           The entire source code is replaced with *rewrite*.
           No matching required.

        2. **Region replace** — when both *begin* and *end* are non-empty.
           Each is a regex that must match **exactly once** in the current
           code.  The span from the start of the *begin* match through the
           end of the *end* match (inclusive) is replaced with
           ``replacements[0]``.

        3. **Exact-string replace** — when *matches* / *replacements* are given.
           Each string in *matches* must appear **exactly once**.
           Replacements are applied in order (existing behaviour).

        On success the edit passes Yosys CEC, cost is computed, and the
        modification is recorded.  Failed edits are rejected and not recorded.
        """
        if not self._current_code:
            raise RuntimeError(
                "no Verilog code to edit; call read_file(path) first")

        # -- Resolve mode -------------------------------------------------------
        if rewrite:
            matches_out, replacements_out, mode = self._edit_rewrite(rewrite)
        elif begin and end:
            if not replacements:
                raise ValueError("region mode requires replacements[0]")
            matches_out, replacements_out, mode = self._edit_region(
                begin, end, replacements[0])
        elif matches and replacements:
            matches_out, replacements_out, mode = self._edit_match(
                matches, replacements)
        else:
            raise ValueError(
                "provide one of: rewrite, (begin+end), or (matches+replacements)")

        # -- Apply --------------------------------------------------------------
        old_code = self._current_code
        new_code = old_code
        for pat, repl in zip(matches_out, replacements_out):
            new_code = new_code.replace(pat, repl)

        # -- CEC verification ---------------------------------------------------
        cec = self._run_cec(old_code, new_code)
        if not cec["success"]:
            lines = [
                "EDIT REJECTED — functional equivalence check failed",
                f"    reason: {cec['reason']}",
            ]
            # Include counterexample details when available.
            cex = cec.get("counterexample")
            if cex:
                for mm in cex.get("mismatches", []):
                    lines.append(
                        f"    mismatch: {mm['po_name']} "
                        f"(old={mm['val_old']}, new={mm['val_new']})"
                    )
                # -- word-level values -----------------------------------------
                wv = cex.get("word_values", {})
                if wv:
                    lines.append("    word-level input values:")
                    for name in sorted(wv.keys()):
                        info = wv[name]
                        u = info["unsigned"]
                        s = info["signed"]
                        w = info["width"]
                        if w == 1:
                            lines.append(f"      {name} = {u}")
                        else:
                            lines.append(
                                f"      {name} = {u} (unsigned)"
                                f" / {s} (signed, {w}-bit)"
                            )
                # -- raw bit pattern (compact) ---------------------------------
                pat = cex.get("pattern", {})
                if pat:
                    lines.append(
                        f"    bit-level input pattern: "
                        + " ".join(f"{k}={v}" for k, v in sorted(pat.items()))
                    )
            lines.append("    details:")
            lines.extend("      " + l for l in cec["output"].splitlines()[-5:])
            return "\n".join(lines)

        # -- Cost ---------------------------------------------------------------
        cost_before = self._compute_cost(old_code)
        cost_after = self._compute_cost(new_code)

        # -- Record -------------------------------------------------------------
        self._mod_counter += 1
        rec = ModificationRecord(
            id=self._mod_counter,
            matches=list(matches_out),
            replacements=list(replacements_out),
            cost_before=cost_before, cost_after=cost_after,
            success=True, mode=mode,
            rewrite=rewrite, begin=begin, end=end,
        )
        self._modifications.append(rec)
        self._current_code = new_code

        # -- Report -------------------------------------------------------------
        reduction = (
            (1 - cost_after / self._original_gate_count) * 100
            if self._original_gate_count > 0 else 0
        )
        lines = [
            "EDIT ACCEPTED — equivalence verified",
            f"    modification #{rec.id}",
            f"    mode        : {mode}",
            f"    cost before : {cost_before}",
            f"    cost after  : {cost_after}",
            f"    reduction   : {reduction:.1f}%",
        ]
        if cost_after < cost_before:
            lines.append(f"    improvement : -{cost_before - cost_after} points")
        elif cost_after > cost_before:
            lines.append(f"    cost increased by +{cost_after - cost_before} points")
        else:
            lines.append(f"    cost unchanged")
        return "\n".join(lines)

    # -- Mode helpers -----------------------------------------------------------

    def _edit_rewrite(
        self, new_code: str
    ) -> tuple[list[str], list[str], str]:
        """Replace the entire source code."""
        old = self._current_code
        new_code = _unescape(new_code)
        return [old], [new_code], "rewrite"

    def _edit_region(
        self, begin_pat: str, end_pat: str, replacement: str
    ) -> tuple[list[str], list[str], str]:
        """Replace the region between *begin_pat* and *end_pat* regex matches.

        Each pattern must match exactly once.  The span from the start of the
        *begin_pat* match through the end of the *end_pat* match (inclusive)
        is captured and replaced.
        """
        begin_match = list(re.finditer(begin_pat, self._current_code))
        if not begin_match:
            raise ValueError(
                f"begin regex {begin_pat!r} not found in current code")
        if len(begin_match) > 1:
            raise ValueError(
                f"begin regex {begin_pat!r} matches {len(begin_match)} times; "
                f"must be unique")
        bm = begin_match[0]

        end_match = list(re.finditer(end_pat, self._current_code))
        if not end_match:
            raise ValueError(
                f"end regex {end_pat!r} not found in current code")
        if len(end_match) > 1:
            raise ValueError(
                f"end regex {end_pat!r} matches {len(end_match)} times; "
                f"must be unique")
        em = end_match[0]

        if em.end() <= bm.start():
            raise ValueError(
                f"end regex matched before begin regex "
                f"(end @{em.start()}-{em.end()}, begin @{bm.start()}-{bm.end()})")

        old_region = self._current_code[bm.start():em.end()]
        return [old_region], [replacement], "region"

    def _edit_match(
        self, matches: list[str], replacements: list[str]
    ) -> tuple[list[str], list[str], str]:
        """Exact-string match-and-replace (original behaviour)."""
        if len(matches) != len(replacements):
            raise ValueError(
                f"matches and replacements must have the same length "
                f"({len(matches)} vs {len(replacements)})")

        # Unescape \n \t etc
        matches = [_unescape(m) for m in matches]
        replacements = [_unescape(r) for r in replacements]

        # No-op check
        if all(m == r for m, r in zip(matches, replacements)):
            raise ValueError(
                "EDIT REJECTED — all matches equal their replacements (no-op).")

        # Uniqueness check
        for i, pat in enumerate(matches):
            p = _word_pattern(pat)
            found = p.findall(self._current_code)
            if not found:
                raise ValueError(
                    f"match[{i}] {pat!r} not found in current code")
            if len(found) > 1:
                raise ValueError(
                    f"match[{i}] {pat!r} appears {len(found)} times; "
                    f"must be unique")

        return matches, replacements, "match"

    # -- ABC CEC ---------------------------------------------------------------

    def _run_cec(self, old_code: str, new_code: str) -> dict:
        """Run ABC CEC via the ``abc_cec.py`` script.

        Returns ``{success, reason, output, counterexample?}`` compatible with
        the edit() flow.  When ABC finds a counterexample, the returned dict
        includes a ``counterexample`` key with a human-readable description
        that is fed back to the LLM.

        Timeout is treated as **success** — the SAT-based equivalence check
        only times out when it cannot find a counterexample (UNSAT).
        """
        old_fd, old_path = tempfile.mkstemp(suffix=".v", prefix="cec_old_")
        new_fd, new_path = tempfile.mkstemp(suffix=".v", prefix="cec_new_")
        try:
            os.write(old_fd, old_code.encode())
            os.close(old_fd)
            os.write(new_fd, new_code.encode())
            os.close(new_fd)

            abc_cec = str(
                Path(__file__).resolve().parents[1] / "scripts" / "abc_cec.py"
            )
            proc = subprocess.run(
                ["python", abc_cec, old_path, new_path, "--json",
                 "--timeout", "310"],
                capture_output=True, text=True, timeout=320,
            )
            return json.loads(proc.stdout.strip())
        except subprocess.TimeoutExpired:
            return {
                "success": True,
                "reason": "timeout_assumed_equivalent",
                "output": "ABC CEC timed out after 320s — assumed equivalent",
            }
        except json.JSONDecodeError:
            return {
                "success": False,
                "reason": "cec_error",
                "output": (proc.stdout or "") + "\n" + (proc.stderr or ""),
            }
        finally:
            for p in (old_path, new_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    # -- cost helper ----------------------------------------------------------

    def _compute_cost(self, code: str) -> int:
        fd, path = tempfile.mkstemp(suffix=".v", prefix="cost_")
        try:
            os.write(fd, code.encode()); os.close(fd)
            cost_script = Path(__file__).resolve().parents[1] / "scripts" / "cost.py"
            proc = subprocess.run(
                ["python", str(cost_script), str(path)],
                capture_output=True, text=True, timeout=30,
            )
            return int(proc.stdout.strip())
        finally:
            try: os.unlink(path)
            except OSError: pass

    # -- replay ---------------------------------------------------------------

    def _replay_code(self, up_to: int) -> str:
        """Replay modifications 0..up_to-1 on _source_code."""
        code = self._source_code
        for rec in self._modifications[:up_to]:
            for pat, repl in zip(rec.matches, rec.replacements):
                code = code.replace(pat, repl)
        return code

    # -- revert ---------------------------------------------------------------

    def revert(self, depth: int = 1, id: int = 0,
               help: bool = False) -> str:
        """Revert previous edits or show modification history.

        help=True : show history table with compact diffs.
        depth=N   : revert N steps (default 1).
        id=X      : revert to before modification id X.
        """
        if help or (depth == 1 and id == 0 and not self._modifications):
            return self._format_history()

        if not self._modifications:
            return "No modifications to revert."

        n = len(self._modifications)
        if id > 0:
            if id > self._mod_counter:
                return f"Invalid id {id}: highest is {self._mod_counter}"
            target = None
            for i, rec in enumerate(self._modifications):
                if rec.id >= id:
                    target = i; break
            if target is None:
                return f"Modification id {id} not found."
            keep = target
        else:
            depth = min(depth, n)
            keep = n - depth

        self._current_code = self._replay_code(keep)
        dropped = self._modifications[keep:]
        self._modifications = self._modifications[:keep]
        self._mod_counter = self._modifications[-1].id if self._modifications else 0

        if not dropped:
            return "No modifications to revert."
        ids = ", ".join(str(r.id) for r in dropped)
        return (f"Reverted {len(dropped)} modification(s) [#{ids}].\n"
                f"Current code is now at modification "
                f"#{self._modifications[-1].id if self._modifications else 0}.")

    def _format_history(self) -> str:
        """Format the modification history with compact diffs."""
        if not self._modifications:
            return "No modifications yet. Use edit() to make changes."
        import datetime
        lines = ["Modification history:"]
        for rec in self._modifications:
            ts = datetime.datetime.fromtimestamp(rec.timestamp).strftime("%H:%M:%S")
            delta = rec.cost_after - rec.cost_before
            sign = "+" if delta > 0 else ""
            lines.append(
                f"  #{rec.id} {ts}  cost: {rec.cost_before}->{rec.cost_after} "
                f"({sign}{delta})  patterns: {len(rec.matches)}"
            )
            for j, (m, r) in enumerate(zip(rec.matches, rec.replacements)):
                ms = _truncate(m, 80)
                rs = _truncate(r, 80)
                if m == r:
                    lines.append(f"    [{j}] no-op: {ms}")
                else:
                    lines.append(f"    [{j}] - {ms}")
                    lines.append(f"         + {rs}")
        return "\n".join(lines)

    # -- show -----------------------------------------------------------------

    def show(self, detail: bool = False, grep: str = "") -> str:
        """Return the current Verilog code, with optional filtering.

        detail=False : uniformly-sampled abridged view.
        detail=True  : complete source.
        grep=<regex> : show matching lines +/-5 context, merging overlaps.
        """
        if not self._current_code:
            raise RuntimeError("no code to show; call read_file(path) first")

        code = self._current_code
        lines_all = code.splitlines()

        if grep:
            try:
                pat = re.compile(grep)
            except re.error as e:
                return f"Invalid regex {grep!r}: {e}"
            matched = [i for i, ln in enumerate(lines_all) if pat.search(ln)]
            if not matched:
                return f"No lines matched {grep!r}"
            ctx = 5
            windows: list[tuple[int, int]] = []
            for mi in matched:
                lo, hi = max(0, mi - ctx), min(len(lines_all) - 1, mi + ctx)
                if windows and lo <= windows[-1][1] + 1:
                    windows[-1] = (windows[-1][0], max(windows[-1][1], hi))
                else:
                    windows.append((lo, hi))
            out: list[str] = []
            for wi, (lo, hi) in enumerate(windows):
                if wi > 0:
                    out.append(f"... ({lo - windows[wi-1][1] - 1} lines skipped) ...")
                for i in range(lo, hi + 1):
                    marker = ">>>" if i in matched else "   "
                    out.append(f"{marker} {i+1:6d}: {lines_all[i]}")
            return "\n".join(out)

        if detail:
            return code

        n = len(lines_all)
        if n <= 60:
            return code
        show_block, skip_block = 10, 20
        out: list[str] = []
        i = 0
        while i < n:
            end = min(i + show_block, n)
            for j in range(i, end):
                out.append(f"{j+1:6d}: {lines_all[j]}")
            i = end
            if i >= n:
                break
            skip_end = min(i + skip_block, n)
            out.append(f"  ...  ({skip_end - i} lines skipped, {skip_end}/{n})  ...")
            i = skip_end
        return "\n".join(out)

    # -- dump -----------------------------------------------------------------

    def dump(self, path: str) -> str:
        """Write the current code to *path*."""
        if not self._current_code:
            raise RuntimeError("no code to dump; call read_file(path) first")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._current_code)
        return f"Dumped {len(self._current_code)} bytes to {path}"
