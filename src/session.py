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
from typing import Any

from .circuit import Circuit, extract_adders, extract_xor
from .circuit.infer import (
    HypothesisRecord,
    PolynomialBudget,
    SymbolicBudget,
    check_samples,
    collect_words,
    fit_builtin_candidates,
    fit_basis_candidates,
    fit_custom_template,
    format_strategy_report,
    format_words,
    io_words,
    optimise_shared_wires,
    polynomial_rewrite_word,
    propose_output_strategy,
    render_hypothesis_rtl,
    simulate_samples,
    support_words,
    symbolic_regression_candidates,
    trace_hypothesis_counterexample,
)
from .circuit.infer.words import cone_gate_histogram


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
        self._hypotheses: list[HypothesisRecord] = []
        self._hyp_counter: int = 0

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
        self._hypotheses = []
        self._hyp_counter = 0

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
    # Word-level hypothesis workflow
    # ------------------------------------------------------------------

    def _io_words(self):
        return io_words(self._current())

    def _next_hypothesis(self, assignments: dict[str, str],
                         declarations: str = "", rtl: str = "",
                         note: str = "") -> HypothesisRecord:
        self._hyp_counter += 1
        rec = HypothesisRecord(
            id=self._hyp_counter,
            assignments=dict(assignments),
            declarations=declarations or "",
            rtl=rtl,
            note=note,
        )
        self._hypotheses.append(rec)
        return rec

    def _find_hypothesis(self, hypothesis_id: int | str | None = None
                         ) -> HypothesisRecord | None:
        if hypothesis_id:
            hid = int(hypothesis_id)
            for rec in self._hypotheses:
                if rec.id == hid:
                    return rec
            return None
        for rec in reversed(self._hypotheses):
            if rec.sample_mismatches or rec.cec_status in {"counterexample", "error"}:
                return rec
        return self._hypotheses[-1] if self._hypotheses else None

    @staticmethod
    def _cec_status(cec: dict) -> str:
        reason = cec.get("reason", "")
        if reason == "equivalent":
            return "proved"
        if reason == "counterexample":
            return "counterexample"
        if reason == "timeout_assumed_equivalent":
            return "timeout_assumed"
        return "error"

    def _format_hypothesis(self, rec: HypothesisRecord) -> str:
        lines = [
            f"Hypothesis #{rec.id}",
            f"    sample_status : {rec.sample_status}",
            f"    cec_status    : {rec.cec_status}",
        ]
        if rec.cost is not None:
            lines.append(f"    cost          : {rec.cost}")
        if rec.note:
            lines.append(f"    note          : {rec.note}")
        if rec.declarations.strip():
            lines.append("    declarations:")
            for line in rec.declarations.strip().splitlines():
                lines.append(f"      {line}")
        lines.append("    assignments:")
        for out, expr in rec.assignments.items():
            lines.append(f"      {out} = {expr}")
        if rec.sample_mismatches:
            lines.append("    sample mismatches:")
            for mm in rec.sample_mismatches[:4]:
                if "error" in mm:
                    lines.append(f"      error: {mm['error']}")
                else:
                    lines.append(
                        f"      sample {mm['sample']} {mm['output']}: "
                        f"expected {mm['expected']}, got {mm['actual']}, "
                        f"inputs={mm['inputs']}"
                    )
        if rec.cec:
            reason = rec.cec.get("reason", "(none)")
            elapsed = rec.cec.get("elapsed")
            suffix = f" in {elapsed:.1f}s" if isinstance(elapsed, (int, float)) else ""
            lines.append(f"    cec_reason    : {reason}{suffix}")
            cex = rec.cec.get("counterexample")
            if cex:
                for mm in cex.get("mismatches", [])[:4]:
                    lines.append(
                        f"      cex mismatch: {mm['po_name']} "
                        f"old={mm['val_old']} new={mm['val_new']}"
                    )
        return "\n".join(lines)

    @staticmethod
    def _poly_budget_from_dict(budget: dict[str, Any] | None) -> PolynomialBudget:
        budget = budget or {}
        return PolynomialBudget(
            max_nodes=int(budget.get("max_nodes", PolynomialBudget.max_nodes)),
            max_expr_chars=int(
                budget.get("max_expr_chars", PolynomialBudget.max_expr_chars)
            ),
            max_intermediate_chars=int(
                budget.get(
                    "max_intermediate_chars",
                    PolynomialBudget.max_intermediate_chars,
                )
            ),
        )

    @staticmethod
    def _symbolic_budget_from_dict(budget: dict[str, Any] | None) -> SymbolicBudget:
        budget = budget or {}
        return SymbolicBudget(
            max_expr_size=int(
                budget.get("max_expr_size", SymbolicBudget.max_expr_size)
            ),
            beam_width=int(budget.get("beam_width", SymbolicBudget.beam_width)),
            max_expr_chars=int(
                budget.get("max_expr_chars", SymbolicBudget.max_expr_chars)
            ),
            max_inputs=int(budget.get("max_inputs", SymbolicBudget.max_inputs)),
            max_support_bits=int(
                budget.get(
                    "max_support_bits",
                    SymbolicBudget.max_support_bits,
                )
            ),
            max_pair_candidates=int(
                budget.get(
                    "max_pair_candidates",
                    SymbolicBudget.max_pair_candidates,
                )
            ),
            max_shift=int(budget.get("max_shift", SymbolicBudget.max_shift)),
        )

    def propose_strategy(self, output: str = "",
                         detail: bool = False,
                         budget: dict[str, Any] | None = None) -> str:
        """Rank template / polynomial / symbolic lanes for output recovery."""
        c = self._current()
        input_words, output_words = self._io_words()
        if output and output not in output_words:
            raise KeyError(
                f"{output!r} is not an output word; available: "
                + ", ".join(output_words)
            )
        targets = [output_words[output]] if output else list(output_words.values())
        poly_budget = self._poly_budget_from_dict(budget)
        strategies = [
            propose_output_strategy(c, out_word, input_words, poly_budget)
            for out_word in targets
        ]
        return format_strategy_report(strategies, detail=detail)

    def run_method(self, output: str,
                   method: str,
                   sample_num: int = 256,
                   budget: dict[str, Any] | None = None,
                   detail: bool = False) -> str:
        """Run one recovery lane without directly editing current source."""
        method_key = method.strip().lower().replace("-", "_")
        if method_key in {"template", "templates", "fit", "candidate"}:
            return self.infer_candidates(
                output=output, sample_num=sample_num, detail=detail)

        c = self._current()
        input_words, output_words = self._io_words()
        if not output:
            raise ValueError("run_method requires output for non-template lanes")
        if output not in output_words:
            raise KeyError(
                f"{output!r} is not an output word; available: "
                + ", ".join(output_words)
            )
        out_word = output_words[output]
        support = support_words(c, out_word, input_words)

        if method_key in {"polynomial", "poly", "polynomial_rewrite", "rewrite"}:
            poly_budget = self._poly_budget_from_dict(budget)
            estimate = propose_output_strategy(c, out_word, input_words, poly_budget)[
                "polynomial_estimate"
            ]
            lines = [
                f"Method run: polynomial_rewrite for {output}",
                f"    estimate feasible : {estimate['feasible']}",
                f"    estimated chars   : {estimate['estimated_chars']}",
                f"    reconvergence     : {estimate['reconvergence']}",
            ]
            if not estimate["feasible"] and not (budget or {}).get("force"):
                lines.extend([
                    "    status            : skipped_budget",
                    "    message           : polynomial rewrite estimate is over budget",
                    "    next              : cut the cone, use fit_basis, try template, "
                    "or rerun with budget={\"force\": true, ...}",
                ])
                for reason in estimate.get("reasons", [])[:4]:
                    lines.append(f"      reason: {reason}")
                return "\n".join(lines)

            fit = polynomial_rewrite_word(c, out_word, poly_budget)
            lines.append(f"    status            : {fit.get('status')}")
            if fit.get("status") != "fit":
                lines.append(f"    message           : {fit.get('message')}")
                lines.append(
                    "    next              : cut the cone, use fit_basis, or "
                    "try symbolic with a tighter support set"
                )
                return "\n".join(lines)

            samples = simulate_samples(
                c, input_words, {output: out_word}, sample_num=sample_num)
            sample_status, mismatches = check_samples(
                {output: fit["expr"]}, "", samples, {output: out_word})
            rec = self._next_hypothesis(
                {output: fit["expr"]},
                note="run_method:polynomial_rewrite",
            )
            rec.sample_status = sample_status
            rec.sample_mismatches = mismatches
            rec.cec_status = (
                "not_run_candidate_only"
                if sample_status == "pass"
                else "skipped_sample_failed"
            )
            lines.extend([
                f"    hypothesis #{rec.id}: {output} = {fit['expr']}",
                f"    expr_chars        : {fit.get('expr_chars')}",
                f"    expanded_nodes    : {fit.get('expanded_nodes')}",
                f"    sample_status     : {sample_status}",
                f"    cec_status        : {rec.cec_status}",
            ])
            if mismatches:
                lines.append("    sample mismatches:")
                for mm in mismatches[:4]:
                    if "error" in mm:
                        lines.append(f"      error: {mm['error']}")
                    else:
                        lines.append(
                            f"      sample {mm['sample']} {mm['output']}: "
                            f"expected={mm['expected']} actual={mm['actual']} "
                            f"inputs={mm['inputs']}"
                        )
            return "\n".join(lines)

        if method_key in {"symbolic", "symbolic_regression", "sr"}:
            sym_budget = self._symbolic_budget_from_dict(budget)
            samples = simulate_samples(
                c, input_words, {output: out_word}, sample_num=sample_num)
            fit = symbolic_regression_candidates(
                samples, out_word, support, budget=sym_budget)
            lines = [
                f"Method run: symbolic_regression for {output}",
                f"    support : {format_words(support)}",
                f"    status  : {fit.get('status')}",
                f"    samples : {fit.get('samples', sample_num)}",
            ]
            if fit.get("status") == "fit":
                rec = self._next_hypothesis(
                    {output: fit["expr"]},
                    note="run_method:symbolic_regression",
                )
                rec.sample_status = "pass"
                rec.cec_status = "not_run_candidate_only"
                lines.extend([
                    f"    hypothesis #{rec.id}: {output} = {fit['expr']}",
                    f"    expr_size : {fit.get('expr_size')}",
                    "    next      : combine with other outputs and run "
                    "check_hypothesis for CEC refinement",
                ])
            else:
                msg = fit.get("message")
                if msg:
                    lines.append(f"    message : {msg}")
                if fit.get("nearest"):
                    lines.append("    nearest sample matches:")
                    for item in fit["nearest"]:
                        lines.append(
                            f"      {item['matching_samples']}/{fit.get('samples', sample_num)} "
                            f"size={item['expr_size']} expr={item['expr']}"
                        )
                lines.append(
                    "    next    : add LLM-designed basis terms, increase budget, "
                    "or use trace_counterexample after a failed full hypothesis"
                )
            return "\n".join(lines)

        raise ValueError(
            "unknown method; use template, polynomial, or symbolic"
        )

    def explain_failure(self, hypothesis_id: int | str | None = None,
                        output: str = "",
                        depth: int = 3) -> str:
        """Explain the latest failed hypothesis and suggest next method steps."""
        rec = self._find_hypothesis(hypothesis_id)
        if rec is None:
            return "No hypotheses have been checked yet."
        lines = [
            f"Failure analysis for hypothesis #{rec.id}",
            f"    sample_status : {rec.sample_status}",
            f"    cec_status    : {rec.cec_status}",
        ]
        if rec.note:
            lines.append(f"    note          : {rec.note}")
        if rec.sample_status == "mismatch":
            lines.append(
                "    diagnosis     : the expression is already contradicted by "
                "samples; inspect missing terms, selector polarity, constants, "
                "and output width before running CEC again"
            )
        elif rec.cec_status == "counterexample":
            lines.append(
                "    diagnosis     : samples passed but formal CEC found a "
                "counterexample; use the CEX as a new targeted sample and "
                "adjust basis/template around the mismatching bit"
            )
        elif rec.cec_status == "timeout_assumed":
            lines.append(
                "    diagnosis     : timeout is not proof; reduce expression "
                "complexity, split outputs, or seek a cheaper shared form"
            )
        elif rec.cec_status == "error":
            lines.append(
                "    diagnosis     : verification or RTL rendering failed; "
                "check syntax, widths, and unsupported operators first"
            )
        else:
            lines.append(
                "    diagnosis     : no hard failure is recorded; use cost audit "
                "or a stricter full-output check if the expression looks bloated"
            )

        lines.append("")
        lines.append(trace_hypothesis_counterexample(
            self._current(),
            rec,
            *self._io_words(),
            output=output,
            depth=depth,
        ))
        lines.extend([
            "",
            "Suggested next actions:",
            "  1. If mismatch is a constant/weight error, use fit_basis with the missing term.",
            "  2. If mismatch follows a selector, add explicit MUX basis terms or run polynomial on the selected output.",
            "  3. If polynomial explodes, cut the cone or use symbolic regression on a smaller support set.",
            "  4. After any correction, rerun check_hypothesis with CEC before edit.",
        ])
        return "\n".join(lines)

    def infer_candidates(self, output: str = "",
                         methods: list[str] | None = None,
                         sample_num: int = 256,
                         detail: bool = False) -> str:
        """Generate structure- and sample-guided word-level candidates."""
        c = self._current()
        input_words, output_words = self._io_words()
        targets = [output_words[output]] if output else list(output_words.values())
        if output and output not in output_words:
            raise KeyError(
                f"{output!r} is not an output word; available: "
                + ", ".join(output_words)
            )

        lines = [
            "Word-level candidate inference",
            f"    inputs  : {format_words(list(input_words.values()))}",
            f"    outputs : {format_words(list(output_words.values()))}",
            f"    samples : {sample_num}",
        ]
        fit_methods = methods
        if not output and methods is None and len(targets) > 12:
            fit_methods = [
                "linear",
                "affine",
                "scale_shift",
                "bitselect",
                "compare",
                "mux",
            ]
            lines.append(
                "    batch_methods : fast "
                "(linear, affine, scale_shift, bitselect, compare, mux)"
            )
        batch_assignments: dict[str, str] = {}
        missing_batch_outputs: list[str] = []
        shared_samples = (
            simulate_samples(c, input_words, output_words, sample_num=sample_num)
            if not output else None
        )
        for out_word in targets:
            support = support_words(c, out_word, input_words)
            hist = cone_gate_histogram(c, out_word)
            samples = shared_samples or simulate_samples(
                c, input_words, {out_word.name: out_word}, sample_num=sample_num)
            candidates = fit_builtin_candidates(samples, out_word, support, fit_methods)
            hist_text = ", ".join(f"{k}:{v}" for k, v in hist.items()) or "(none)"
            lines.extend([
                "",
                f"== {out_word.name}[{out_word.width}] ==",
                f"support : {format_words(support)}",
                f"cone    : {hist_text}",
            ])
            if not candidates:
                lines.append("candidates: none fitted; try check_hypothesis or fit_hypothesis with a custom template")
                if not output:
                    missing_batch_outputs.append(out_word.name)
                continue
            if not output:
                batch_assignments[out_word.name] = candidates[0]["expr"]
            for cand in candidates[:6 if detail else 3]:
                rec = self._next_hypothesis(
                    {out_word.name: cand["expr"]},
                    note=f"infer_candidates:{cand['method']}",
                )
                rec.sample_status = "pass"
                rec.cec_status = "not_run_candidate_only"
                score = cand.get("score", "?")
                lines.append(
                    f"candidate #{rec.id}: {out_word.name} = {cand['expr']} "
                    f"(method={cand['method']}, score={score}, cec=not_run_candidate_only)"
                )
                if detail:
                    for key, value in cand.items():
                        if key not in {"expr", "method", "output"}:
                            lines.append(f"  {key}: {value}")
        if not output and batch_assignments:
            samples = shared_samples or simulate_samples(
                c, input_words, output_words, sample_num=sample_num)
            sample_status, mismatches = check_samples(
                batch_assignments, "", samples, output_words)
            rec = self._next_hypothesis(
                batch_assignments,
                note="infer_candidates:batch_top",
            )
            rec.sample_status = sample_status
            rec.sample_mismatches = mismatches
            rec.cec_status = (
                "not_run_candidate_only"
                if sample_status == "pass"
                else "skipped_sample_failed"
            )
            if missing_batch_outputs:
                rec.note = (
                    "batch top candidates missing outputs: "
                    + ", ".join(missing_batch_outputs)
                )
            lines.extend([
                "",
                "== batch top assignments ==",
                f"hypothesis #{rec.id}",
                f"assigned_outputs : {len(batch_assignments)}/{len(output_words)}",
                f"sample_status    : {sample_status}",
                f"cec_status       : {rec.cec_status}",
            ])
            if missing_batch_outputs:
                lines.append("missing_outputs  : " + ", ".join(missing_batch_outputs))
            if mismatches:
                lines.append("sample mismatches:")
                for mm in mismatches[:4]:
                    if "error" in mm:
                        lines.append(f"  error: {mm['error']}")
                    else:
                        lines.append(
                            f"  sample {mm['sample']} {mm['output']}: "
                            f"expected={mm['expected']} actual={mm['actual']} "
                            f"inputs={mm['inputs']}"
                        )
            lines.append("assignments_json:")
            lines.append(json.dumps(batch_assignments, indent=2, sort_keys=True))
        return "\n".join(lines)

    def check_hypothesis(self, assignments: dict[str, str],
                         declarations: str = "",
                         sample_num: int = 256,
                         run_cec: bool = True,
                         share_common: bool = True) -> str:
        """Check an LLM-proposed hypothesis without modifying current source."""
        if not self._current_code:
            raise RuntimeError("no Verilog code loaded; call read_file(path) first")
        if not assignments:
            raise ValueError("assignments must name at least one output word")

        c = self._current()
        input_words, output_words = self._io_words()
        unknown = [name for name in assignments if name not in output_words]
        if unknown:
            raise KeyError(
                "unknown output assignment(s): "
                + ", ".join(unknown)
                + f"; available outputs: {', '.join(output_words)}"
            )

        samples = simulate_samples(
            c, input_words, output_words, sample_num=sample_num)
        sample_status, mismatches = check_samples(
            assignments, declarations, samples, output_words)

        rec = self._next_hypothesis(assignments, declarations)
        rec.sample_status = sample_status
        rec.sample_mismatches = mismatches

        missing_outputs = [name for name in output_words if name not in assignments]
        if missing_outputs:
            rec.cec_status = "skipped_partial_outputs"
            rec.note = "CEC skipped because assignments do not cover: " + ", ".join(missing_outputs)
            return self._format_hypothesis(rec)

        if sample_status != "pass":
            rec.cec_status = "skipped_sample_failed"
            rec.note = "CEC skipped because sample checking did not pass"
            return self._format_hypothesis(rec)

        use_compact_widths = False
        if share_common:
            opt_declarations, opt_assignments, opt_stats = optimise_shared_wires(
                assignments, declarations, input_words, output_words)
            if opt_stats.get("shared_count"):
                opt_status, opt_mismatches = check_samples(
                    opt_assignments, opt_declarations, samples, output_words)
                if opt_status == "pass":
                    rec.declarations = opt_declarations
                    rec.assignments = opt_assignments
                    shared = ", ".join(
                        f"{item['name']}={item['expr']}"
                        for item in opt_stats.get("shared_wires", [])[:8]
                    )
                    suffix = "" if len(opt_stats.get("shared_wires", [])) <= 8 else ", ..."
                    rec.note = (
                        f"shared {opt_stats['shared_count']} common expression(s): "
                        + shared
                        + suffix
                    )
                    assignments = opt_assignments
                    declarations = opt_declarations
                    use_compact_widths = True
                else:
                    rec.note = "shared-wire optimization skipped because sample check failed"
                    rec.sample_mismatches = opt_mismatches

        rec.rtl = render_hypothesis_rtl(
            c.name or "top", input_words, output_words, assignments, declarations,
            extend_widths=not use_compact_widths)
        cost_rtl = render_hypothesis_rtl(
            c.name or "top", input_words, output_words, assignments, declarations,
            extend_widths=False)
        rec.cost = self._compute_cost(cost_rtl)
        if not use_compact_widths:
            proof_cost = self._compute_cost(rec.rtl)
            if proof_cost != rec.cost:
                cost_note = (
                    "cost computed on compact RTL; CEC RTL with explicit "
                    f"width extensions would cost {proof_cost}"
                )
                rec.note = f"{rec.note}; {cost_note}" if rec.note else cost_note

        if run_cec:
            rec.cec = self._run_cec(self._current_code, rec.rtl)
            rec.cec_status = self._cec_status(rec.cec)
        else:
            rec.cec_status = "not_run"
        return self._format_hypothesis(rec)

    def fit_hypothesis(self, output: str, template: str,
                       unknowns: list[str] | dict | None = None,
                       sample_num: int = 256) -> str:
        """Fit integer parameters for an LLM-proposed output template."""
        c = self._current()
        input_words, output_words = self._io_words()
        if output not in output_words:
            raise KeyError(
                f"{output!r} is not an output word; available: "
                + ", ".join(output_words)
            )
        samples = simulate_samples(
            c, input_words, {output: output_words[output]}, sample_num=sample_num)
        fit = fit_custom_template(samples, output_words[output], template, unknowns)
        lines = [
            f"Template fit for {output}",
            f"    status  : {fit.get('status')}",
            f"    samples : {fit.get('samples', sample_num)}",
        ]
        if fit.get("status") == "fit":
            rec = self._next_hypothesis(
                {output: fit["expr"]},
                note="fit_hypothesis:custom_template",
            )
            rec.sample_status = "pass"
            rec.cec_status = "not_run_candidate_only"
            lines.append(f"    hypothesis #{rec.id}: {output} = {fit['expr']}")
            lines.append(f"    coefficients : {fit.get('coefficients')}")
        else:
            msg = fit.get("message")
            if msg:
                lines.append(f"    message : {msg}")
        return "\n".join(lines)

    def fit_basis(self, output: str, basis: list[str],
                  include_constant: bool = True,
                  coefficient_limit: int = 4096,
                  sample_num: int = 256) -> str:
        """Fit an LLM-supplied linear combination of arbitrary basis terms."""
        c = self._current()
        input_words, output_words = self._io_words()
        if output not in output_words:
            raise KeyError(
                f"{output!r} is not an output word; available: "
                + ", ".join(output_words)
            )
        samples = simulate_samples(
            c, input_words, {output: output_words[output]}, sample_num=sample_num)
        fit = fit_basis_candidates(
            samples,
            output_words[output],
            basis,
            include_constant=include_constant,
            coefficient_limit=coefficient_limit,
        )
        lines = [
            f"Basis fit for {output}",
            f"    status  : {fit.get('status')}",
            f"    samples : {fit.get('samples', sample_num)}",
        ]
        if fit.get("status") == "fit":
            rec = self._next_hypothesis(
                {output: fit["expr"]},
                note="fit_basis:llm_basis",
            )
            rec.sample_status = "pass"
            rec.cec_status = "not_run_candidate_only"
            lines.append(f"    hypothesis #{rec.id}: {output} = {fit['expr']}")
            lines.append(f"    constant : {fit.get('constant')}")
            lines.append("    coefficients:")
            for basis_expr, coeff in fit.get("coefficients", {}).items():
                if coeff:
                    lines.append(f"      {coeff} * ({basis_expr})")
        else:
            msg = fit.get("message")
            if msg:
                lines.append(f"    message : {msg}")
        return "\n".join(lines)

    def trace_counterexample(self, hypothesis_id: int | str | None = None,
                             output: str = "",
                             bits: list[int] | None = None,
                             depth: int = 3) -> str:
        """Replay the latest failed hypothesis or a specific hypothesis id."""
        rec = self._find_hypothesis(hypothesis_id)
        if rec is None:
            return "No hypotheses have been checked yet."
        input_words, output_words = self._io_words()
        return trace_hypothesis_counterexample(
            self._current(), rec, input_words, output_words,
            output=output, bits=bits, depth=depth,
        )


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
        accept_timeout: bool = False,
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

        On success the edit passes ABC CEC, cost is computed, and the
        modification is recorded.  Failed edits are rejected and not recorded.
        ABC timeout is not accepted unless *accept_timeout* is True.
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
        cec_status = self._cec_status(cec)
        if cec_status != "proved" and not (
            cec_status == "timeout_assumed" and accept_timeout
        ):
            lines = [
                "EDIT REJECTED — functional equivalence check failed",
                f"    reason: {cec['reason']}",
                f"    status: {cec_status}",
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
            lines.extend("      " + l for l in cec.get("output", "").splitlines()[-5:])
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
            "EDIT ACCEPTED — equivalence verified"
            if cec_status == "proved"
            else "EDIT ACCEPTED — CEC timeout explicitly accepted",
            f"    modification #{rec.id}",
            f"    mode        : {mode}",
            f"    cec status  : {cec_status}",
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

        The underlying script reports timeout as ``success=True`` with reason
        ``timeout_assumed_equivalent``.  Callers must classify that reason
        separately; ``edit`` does not accept it by default.
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
            result = json.loads(proc.stdout.strip())
            if result.get("reason") == "counterexample":
                yosys_cec = str(
                    Path(__file__).resolve().parents[1]
                    / "scripts"
                    / "yosys_cec.py"
                )
                yproc = subprocess.run(
                    ["python", yosys_cec, old_path, new_path, "--json",
                     "--timeout", "120"],
                    capture_output=True, text=True, timeout=130,
                )
                try:
                    yresult = json.loads(yproc.stdout.strip())
                except json.JSONDecodeError:
                    yresult = {
                        "success": False,
                        "reason": "yosys_cec_error",
                        "output": (yproc.stdout or "") + "\n" + (yproc.stderr or ""),
                        "elapsed": 0.0,
                    }
                result["crosscheck"] = {
                    "yosys_success": yresult.get("success"),
                    "yosys_reason": yresult.get("reason"),
                }
                if yresult.get("success"):
                    return {
                        "success": True,
                        "reason": "equivalent",
                        "output": (
                            "ABC reported a counterexample, but Yosys equiv "
                            "proved equivalence.\n\n"
                            "ABC output:\n"
                            + result.get("output", "")
                            + "\n\nYosys output:\n"
                            + yresult.get("output", "")
                        ),
                        "elapsed": (
                            float(result.get("elapsed") or 0)
                            + float(yresult.get("elapsed") or 0)
                        ),
                        "crosscheck": {
                            "abc_reason": result.get("reason"),
                            "yosys_reason": yresult.get("reason"),
                        },
                    }
            return result
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
