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
from .circuit.recovery import (
    ExpressionCandidate,
    RecoveryReport,
    RecoveryIRRecorder,
    TOOL_PROFILES,
    VerificationResult,
    analyze_words as _analyze_recovery_words,
    check_polynomial_lowbits_data,
    fit_template_data,
    format_recovery_plan,
    format_recovery_report,
    format_word_analysis,
    get_tool_profile,
    influence_profile_data,
    infer_native_expr_data,
    infer_pysr_expr_data,
    plan_recovery as _plan_recovery,
    probe_target_data,
    recover_rtl_data,
    split_control_cases_data,
    validate_expr_data,
)
from .circuit.recovery.pipeline import emit_candidate_rtl
from .circuit.recovery.rtl_opt import (
    analyze_rtl_cost as analyze_rtl_cost_data,
    propose_rtl_rewrites as propose_rtl_rewrites_data,
)
from .circuit.symbolic.parser import expression_identifiers
from .circuit.symbolic.resolver import build_net_to_node, resolve_field, resolve_inputs
from .circuit.symbolic.sampler import support_bit_count


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


def _split_port_list(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    tail = text[start:]
    if tail.strip():
        parts.append(tail)
    return parts


def _module_ports_from_code(code: str) -> list[str]:
    """Parse top-level module port names for CEC compatibility checks."""
    match = re.search(r"\bmodule\s+(?:\\\S+|[A-Za-z_]\w*)\s*\((.*?)\)\s*;",
                      code, re.DOTALL)
    if not match:
        return []
    ports: list[str] = []
    for raw in _split_port_list(match.group(1)):
        text = re.sub(r"\b(input|output|inout|wire|reg|logic|signed|unsigned)\b",
                      " ", raw)
        text = re.sub(r"\[[^\]]+\]", " ", text)
        names = re.findall(r"\\\S+|[A-Za-z_][\w$.]*", text)
        if names:
            name = names[-1]
            ports.append(name[1:].strip() if name.startswith("\\") else name)
    return ports


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

    def __init__(self, *,
                 ir_run_base: str | None = None,
                 recovery_kb_dir: str | None = None) -> None:
        self.circuit: Circuit | None = None
        self.source: str | None = None

        # Code-modification workflow
        self._source_code: str = ""
        self._current_code: str = ""
        self._modifications: list[ModificationRecord] = []
        self._mod_counter: int = 0
        self._original_gate_count: int = 0
        self._analysis_seen: bool = False
        self._recovery_plan_seen: bool = False
        self._candidate_counter: int = 0
        self._branch_counter: int = 0
        self._failed_counter: int = 0
        self._accepted_candidates: dict[str, ExpressionCandidate] = {}
        self._branch_candidates: dict[str, ExpressionCandidate] = {}
        self._failed_candidates: dict[str, ExpressionCandidate] = {}
        recorder_kwargs = {}
        if ir_run_base is not None:
            recorder_kwargs["run_base"] = ir_run_base
        if recovery_kb_dir is not None:
            recorder_kwargs["kb_dir"] = recovery_kb_dir
        self._ir_recorder = RecoveryIRRecorder(**recorder_kwargs)

    # -- operations -----------------------------------------------------

    def load(self, path: str, *,
             record_ir: bool = False,
             case_id: str | None = None,
             out_dir: str | None = None) -> str:
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
        self._analysis_seen = False
        self._recovery_plan_seen = False
        self._candidate_counter = 0
        self._branch_counter = 0
        self._failed_counter = 0
        self._accepted_candidates = {}
        self._branch_candidates = {}
        self._failed_candidates = {}
        self._ir_recorder.reset()

        report = self._load_report(fmt)
        if record_ir:
            run = self._ir_recorder.start(
                circuit, source_path=path, case_id=case_id, out_dir=out_dir)
            report += (
                f"\n    Recovery reasoning graph run       : {run.run_id}"
                f"\n    Recovery reasoning graph directory : {run.run_dir}"
            )
        return report

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

    def infer_expression(
        self,
        targets: list[str] | str,
        inputs: list[str] | None = None,
        mode: str = "auto",
        max_cost: int | None = None,
        pattern_num: int = 4096,
        validation_num: int = 16384,
        seed: int | None = 0,
        timeout_s: int = 120,
        niterations: int = 200,
        detail: bool = False,
    ) -> str:
        """Deprecated guardrail for broad expression inference."""
        return "\n".join([
            "infer_expression is deprecated for agent-facing recovery.",
            "    It is a broad symbolic search tool and can bypass the plan/probe/validate workflow.",
            "    Use recovery_tool_guide(), probe_target(), validate_expr(), fit_template(), or split_control_cases() first.",
            "    For a branch hypothesis, use validate_expr(..., fixed_inputs={...}).",
            "    PySR/native enumeration should only be reached through the narrower recovery tools.",
        ])

    def analyze_words(self, outputs: list[str] | None = None,
                      max_low_bits: int = 10,
                      detail: bool = False,
                      format: str = "text") -> str:
        """Recover word boundaries and support cuts for RTL recovery."""
        analysis = _analyze_recovery_words(
            self._current(), outputs=outputs, max_low_bits=max_low_bits)
        self._analysis_seen = True
        self._ir_recorder.log_event(
            "word_analysis",
            payload={
                "params": {
                    "outputs": outputs,
                    "max_low_bits": max_low_bits,
                    "detail": detail,
                },
                "analysis": analysis.to_dict(),
            },
            tool="analyze_words",
            status="ok")
        if format == "json":
            return json.dumps(analysis.to_dict(), indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        return format_word_analysis(analysis, detail=detail)

    def plan_recovery(self, outputs: list[str] | None = None,
                      feedback: str | dict | None = None,
                      detail: bool = False,
                      format: str = "text") -> str:
        """Plan an iterative, analysis-driven RTL recovery workflow."""
        plan = _plan_recovery(self._current(), outputs=outputs, feedback=feedback)
        self._analysis_seen = True
        self._recovery_plan_seen = True
        memory_hints: list[dict] = []
        for cluster in plan.clusters:
            target = cluster.outputs[0] if cluster.outputs else cluster.name
            inputs = [word.split("[", 1)[0] for word in cluster.support_words]
            memory_hints.extend(self._memory_hits_for(target, inputs, limit=2))
        self._ir_recorder.record_plan(
            "plan_recovery",
            {
                "outputs": outputs,
                "feedback": feedback,
                "detail": detail,
                "format": format,
                "memory_hints": memory_hints,
            },
            plan)
        if format == "json" and memory_hints:
            data = plan.to_dict()
            data["memory_hints"] = memory_hints
            return json.dumps(data, indent=2, sort_keys=True)
        text = format_recovery_plan(plan, detail=detail, format=format)
        if format == "text" and memory_hints:
            text += "\n\n== Recovery experience hints ==\n"
            text += "\n".join(self._format_memory_hits(memory_hints).splitlines()[1:])
        return text

    def recovery_tool_guide(self, format: str = "text") -> str:
        """Describe recovery tool effort levels and escalation order."""
        profiles = [profile.to_dict() for profile in TOOL_PROFILES.values()]
        if format == "json":
            return json.dumps(profiles, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        effort_order = {"probe": 0, "verify": 1, "light": 2, "medium": 3, "heavy": 4}
        lines = ["Recovery tool guide — cheapest sufficient method first"]
        for profile in sorted(
            TOOL_PROFILES.values(),
            key=lambda p: (effort_order.get(p.effort, 99), p.tool),
        ):
            lines.append(
                f"  {profile.tool}: effort={profile.effort} "
                f"search={profile.search_space} risk={profile.risk}")
            if profile.requires_hypothesis:
                lines.append("      requires: explicit hypothesis")
            if profile.prerequisites:
                lines.append(f"      prereq  : {', '.join(profile.prerequisites)}")
            if profile.fallback_after:
                lines.append(f"      after   : {', '.join(profile.fallback_after)}")
            if profile.guidance:
                lines.append(f"      use     : {profile.guidance}")
        lines.append("")
        lines.append("Rule: if a formula hypothesis exists, validate_expr is cheaper than any search.")
        lines.append("Rule: escalate probe -> verify -> light -> medium -> heavy; on failure, shrink before escalating.")
        return "\n".join(lines)

    def start_recovery_run(self, case_id: str | None = None,
                           out_dir: str | None = None,
                           format: str = "text") -> str:
        """Start a persistent reasoning-graph run for the current circuit."""
        run = self._ir_recorder.start(
            self._current(), source_path=self.source, case_id=case_id,
            out_dir=out_dir)
        if format == "json":
            return json.dumps(run.to_dict(), indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        return "\n".join([
            "Recovery reasoning graph run started",
            f"    run_id : {run.run_id}",
            f"    case   : {run.case_id or '(none)'}",
            f"    dir    : {run.run_dir}",
            f"    circuit: {run.circuit.circuit_name}",
            "    files  : graph.json, summary.json, graph.jsonl after first node",
        ])

    def export_recovery_ir(self, format: str = "json") -> str:
        """Export the current reasoning-graph snapshot."""
        data = self._ir_recorder.export()
        if format == "json":
            return json.dumps(data, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        if data.get("status") == "no_active_recovery_run":
            return "No active recovery reasoning graph run."
        return "\n".join([
            "Recovery reasoning graph snapshot",
            f"    run_id : {data.get('run_id')}",
            f"    case   : {data.get('case_id') or '(none)'}",
            f"    dir    : {data.get('run_dir')}",
            f"    nodes  : {data.get('node_count')}",
        ])

    def record_recovery_graph_node(self, node_type: str,
                                   target: str = "",
                                   summary: str = "",
                                   data: dict | None = None,
                                   links: list[str] | None = None,
                                   promotable: bool = False,
                                   format: str = "text") -> str:
        """Record one compact reasoning-graph node."""
        node = self._ir_recorder.add_graph_node(
            node_type,
            target=target,
            summary=summary,
            data=data,
            links=links,
            promotable=promotable)
        payload = node or {"status": "no_active_recovery_run"}
        if format == "json":
            return json.dumps(payload, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        if node is None:
            return "No active recovery reasoning graph run; node was not recorded."
        return (
            f"Recorded reasoning graph node {node['node_id']} "
            f"type={node_type} target={target or '(none)'}"
        )

    def query_recovery_experience(self, target: str = "",
                                  inputs: list[str] | None = None,
                                  features: dict | None = None,
                                  limit: int = 5,
                                  format: str = "text") -> str:
        """Query local promoted recovery experience without accepting candidates."""
        hits = self._ir_recorder.query_memory(
            circuit=self.circuit,
            target=target,
            inputs=inputs,
            features=features,
            limit=limit)
        if format == "json":
            return json.dumps(hits, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        return self._format_memory_hits(hits)

    def query_recovery_memory(self, target: str = "",
                              inputs: list[str] | None = None,
                              features: dict | None = None,
                              limit: int = 5,
                              format: str = "text") -> str:
        """Compatibility alias for query_recovery_experience."""
        return self.query_recovery_experience(
            target=target,
            inputs=inputs,
            features=features,
            limit=limit,
            format=format)

    def promote_recovery_experience(self, run_id: str | None = None,
                                    relation_node_ids: list[str] | None = None,
                                    problem_node_ids: list[str] | None = None,
                                    dry_run: bool = True,
                                    format: str = "text") -> str:
        """Promote verified reasoning-graph paths into the local experience KB."""
        result = self._ir_recorder.promote_memory(
            run_id=run_id,
            candidate_ids=relation_node_ids,
            failure_ids=problem_node_ids,
            dry_run=dry_run)
        if format == "json":
            return json.dumps(result, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        lines = [
            "Recovery experience promotion",
            f"    mode      : {'dry-run' if dry_run else 'write'}",
            f"    run       : {result.get('run')}",
            f"    relations : {result.get('relations', 0)}",
            f"    experience: {result.get('experiences', 0)}",
            f"    families  : {result.get('template_families', 0)}",
            f"    negative  : {result.get('failures', 0)}",
        ]
        if result.get("written"):
            lines.append(f"    written   : {result['written']}")
        return "\n".join(lines)

    def promote_recovery_memory(self, run_id: str | None = None,
                                candidate_ids: list[str] | None = None,
                                failure_ids: list[str] | None = None,
                                dry_run: bool = True,
                                format: str = "text") -> str:
        """Compatibility alias for promote_recovery_experience.

        ``candidate_ids`` are interpreted as relation graph node ids in the
        graph-only recorder. ``failure_ids`` are problem graph node ids.
        """
        return self.promote_recovery_experience(
            run_id=run_id,
            relation_node_ids=candidate_ids,
            problem_node_ids=failure_ids,
            dry_run=dry_run,
            format=format)

    def record_recovery_note(self, kind: str,
                             target: str = "",
                             summary: str = "",
                             refs: list[str] | None = None,
                             format: str = "text") -> str:
        """Record model reasoning as unverified IR, never as accepted evidence."""
        event = self._ir_recorder.record_note(
            kind=kind, target=target, summary=summary, refs=refs)
        data = event or {"status": "no_active_recovery_run"}
        if format == "json":
            return json.dumps(data, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        if event is None:
            return "No active recovery reasoning graph run; note was not recorded."
        return (
            f"Recorded recovery graph node {event['node_id']} "
            f"target={target or '(none)'} kind={kind}"
        )

    @staticmethod
    def _format_memory_hits(hits: list[dict]) -> str:
        lines = ["Recovery experience hits"]
        if not hits:
            lines.append("  (empty)")
            lines.append("  Retrieval suggests experiments only; validate any hypothesis before using it.")
            return "\n".join(lines)
        for hit in hits:
            marker = "negative-experience" if hit.get("negative") else hit.get("kind", "experience")
            lines.append(
                f"  - [{marker} score={hit.get('score')}] "
                f"target={hit.get('target') or '(any)'}")
            if hit.get("relation"):
                lines.append(f"      relation={hit.get('relation')}")
            if hit.get("trigger"):
                lines.append(f"      trigger={hit.get('trigger')}")
            if hit.get("suggested_experiment"):
                lines.append(f"      experiment={hit.get('suggested_experiment')}")
            family = hit.get("template_family") or {}
            if isinstance(family, dict) and family:
                if family.get("family"):
                    lines.append(f"      template_family={family.get('family')}")
                if family.get("template"):
                    lines.append(f"      template={family.get('template')}")
            validation = hit.get("validation") or hit.get("verification")
            if validation:
                if isinstance(validation, dict):
                    validation = validation.get("status") or validation.get("verification") or validation
                lines.append(f"      validation={validation}")
            provenance = hit.get("provenance") or {}
            if isinstance(provenance, dict):
                run_id = provenance.get("run_id", "")
                source_node = provenance.get("source_node_id", "")
                validation_node = provenance.get("validation_node_id", "")
                if run_id or source_node or validation_node:
                    lines.append(
                        f"      provenance=run:{run_id or '?'} source:{source_node or '?'} validation:{validation_node or '?'}")
            tokens = hit.get("matched_tokens") or []
            if tokens:
                lines.append(f"      matched={', '.join(tokens[:8])}")
        lines.append("  Retrieval suggests experiments only; it never creates accepted candidates.")
        return "\n".join(lines)

    def _memory_hits_for(self, target: str,
                         inputs: list[str] | tuple[str, ...] | None = None,
                         limit: int = 3) -> list[dict]:
        return self._ir_recorder.query_memory(
            circuit=self.circuit,
            target=target,
            inputs=inputs,
            limit=limit)

    @staticmethod
    def _append_memory_notes(report: RecoveryReport,
                             hits: list[dict]) -> RecoveryReport:
        if not hits:
            return report
        notes = list(report.notes)
        notes.append(
            "local recovery experience returned advisory experiment/stop-rule hints; "
            "retrieval never creates accepted candidates")
        for hit in hits[:3]:
            label = "negative-experience" if hit.get("negative") else "experience"
            relation = hit.get("relation") or hit.get("summary") or hit.get("target")
            family = ""
            template_family = hit.get("template_family") or {}
            if isinstance(template_family, dict) and template_family.get("family"):
                family = f" family={template_family['family']}"
            notes.append(
                f"memory {label} hit score={hit.get('score')}: "
                f"{relation}{family}")
        report.notes = tuple(notes)
        return report

    def _cache_accepted_candidates(self, report: RecoveryReport) -> RecoveryReport:
        """Assign stable ids to candidates and remember their role."""
        for cand in report.candidates:
            if cand.verification.accepted and cand.case_condition:
                if not cand.candidate_id:
                    self._branch_counter += 1
                    cand.candidate_id = f"B{self._branch_counter}"
                self._branch_candidates[cand.candidate_id] = cand
                continue
            if cand.verification.accepted:
                if not cand.candidate_id:
                    self._candidate_counter += 1
                    cand.candidate_id = f"C{self._candidate_counter}"
                self._accepted_candidates[cand.candidate_id] = cand
                continue
            if cand.verification.status.startswith("slice-"):
                continue
            if not cand.candidate_id:
                self._failed_counter += 1
                cand.candidate_id = f"F{self._failed_counter}"
            self._failed_candidates[cand.candidate_id] = cand
        return report

    @staticmethod
    def _candidate_rows(candidates: dict[str, ExpressionCandidate],
                        role: str) -> list[dict]:
        rows: list[dict] = []
        for cid, cand in candidates.items():
            rows.append({
                "id": cid,
                "role": role,
                "target": cand.target,
                "expression": cand.expression,
                "method": cand.method,
                "verification": cand.verification.status,
                "case_condition": cand.case_condition,
                "inputs": list(cand.inputs),
                "cost": cand.cost,
            })
        return rows

    def list_candidates(self, format: str = "text") -> str:
        """List accepted, branch-only, and failed local candidates."""
        rows = (
            self._candidate_rows(self._accepted_candidates, "accepted-global")
            + self._candidate_rows(self._branch_candidates, "branch-evidence")
            + self._candidate_rows(self._failed_candidates, "failed")
        )
        if format == "json":
            return json.dumps(rows, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        lines = ["Candidate cache"]
        if not rows:
            lines.append("  (empty)")
            return "\n".join(lines)
        for role, title in (
            ("accepted-global", "Accepted global candidates"),
            ("branch-evidence", "Branch-only evidence"),
            ("failed", "Failed candidates"),
        ):
            group = [row for row in rows if row["role"] == role]
            lines.append(f"== {title} ==")
            if not group:
                lines.append("  (none)")
                continue
            for row in group:
                cond = f" when {row['case_condition']}" if row["case_condition"] else ""
                lines.append(
                    f"  {row['id']}: {row['target']}{cond} = {row['expression']}")
                lines.append(
                    f"      method={row['method']} verification={row['verification']} "
                    f"cost={row['cost']} inputs={', '.join(row['inputs']) or '(none)'}")
        lines.append("")
        lines.append("Only C# accepted-global candidates are consumed by assemble_rtl.")
        lines.append("B# branch evidence must be combined into a full expression first.")
        return "\n".join(lines)

    @staticmethod
    def _input_base(ref: str) -> str:
        return ref.split("[", 1)[0]

    def _candidate_aliases(self) -> dict[str, ExpressionCandidate]:
        aliases: dict[str, ExpressionCandidate] = {}
        for cid, cand in self._accepted_candidates.items():
            aliases[cid] = cand
            aliases[cand.target] = cand
        return aliases

    def _is_primary_input_ref(self, ref: str) -> bool:
        net_to_node = build_net_to_node(self._current())
        try:
            field = resolve_field(self._current(), ref, net_to_node, role="input")
        except KeyError:
            return False
        return all(self._current().nodes[bit.node_id].is_pi for bit in field.bits)

    def _expand_candidate_refs(self, expression: str,
                               inputs: list[str] | None
                               ) -> tuple[str, list[str] | None]:
        """Inline references to accepted C# candidates or recovered outputs."""
        aliases = self._candidate_aliases()
        if not aliases:
            return expression, inputs
        expanded = expression
        resolved_inputs = list(inputs) if inputs is not None else None
        for _ in range(8):
            changed = False
            for name in expression_identifiers(expanded):
                cand = aliases.get(name)
                if cand is None:
                    continue
                pattern = re.compile(rf"(?<![$A-Za-z0-9_]){re.escape(name)}(?![$A-Za-z0-9_])")
                expanded_next = pattern.sub(f"({cand.expression})", expanded)
                if expanded_next == expanded:
                    continue
                expanded = expanded_next
                changed = True
                if resolved_inputs is None:
                    resolved_inputs = []
                seen = {self._input_base(ref) for ref in resolved_inputs}
                for ref in cand.inputs:
                    base = self._input_base(ref)
                    if base not in seen:
                        resolved_inputs.append(base)
                        seen.add(base)
            if not changed:
                break
        return expanded, resolved_inputs

    def _complete_expression_inputs(self, expression: str,
                                    inputs: list[str] | None
                                    ) -> list[str] | None:
        """Ensure expression variables are PI inputs, not raw outputs/internal nets."""
        refs = list(inputs) if inputs is not None else []
        seen = {self._input_base(ref) for ref in refs}
        invalid: list[str] = []
        for name in expression_identifiers(expression):
            base = self._input_base(name)
            if base in seen:
                continue
            if self._is_primary_input_ref(name):
                refs.append(name)
                seen.add(base)
            else:
                invalid.append(name)
        if invalid:
            names = ", ".join(sorted(set(invalid)))
            raise ValueError(
                "expression references non-input or unknown variable(s): "
                f"{names}. Use only primary inputs and accepted candidates; "
                "accepted candidates can be referenced by C# id or target name "
                "after validate_expr/list_candidates confirms them.")
        return refs

    def _condition_from_fixed(self, fixed_inputs: dict[str, int],
                              input_widths: dict[str, int]) -> str:
        parts: list[str] = []
        for key, raw_value in sorted(fixed_inputs.items()):
            value = int(raw_value)
            base = key.split("[", 1)[0]
            width = input_widths.get(base, 1)
            if "[" in key:
                width = 1
            parts.append(f"{key} == {width}'d{value}")
        if not parts:
            return "1'd1"
        return " && ".join(f"({part})" for part in parts)

    def _combine_case_expression(self, controls: list[str],
                                 cases: list[dict],
                                 inputs: list[str] | None) -> tuple[str, list[str] | None]:
        if not cases:
            raise ValueError("cases must not be empty")
        net_to_node = build_net_to_node(self._current())
        target_stub = resolve_field(self._current(), controls[0], net_to_node, role="input") if controls else None
        input_refs = list(inputs or [])
        seen = {self._input_base(ref) for ref in input_refs}
        for control in controls:
            base = self._input_base(control)
            if base not in seen:
                input_refs.append(base)
                seen.add(base)
        for case in cases:
            fixed = case.get("fixed_inputs") or {}
            expr = str(case.get("expression") or "")
            if not expr:
                raise ValueError("each case must include an expression")
            for name in expression_identifiers(expr):
                if name in self._candidate_aliases():
                    continue
                if not self._is_primary_input_ref(name):
                    raise ValueError(
                        "case expression references non-input or unknown "
                        f"variable {name!r}; validate and cache it first if it "
                        "is an intermediate predicate")
                if name not in seen:
                    input_refs.append(name)
                    seen.add(name)
            for key in fixed:
                base = self._input_base(str(key))
                if base not in seen:
                    input_refs.append(base)
                    seen.add(base)
        input_widths: dict[str, int] = {}
        if input_refs:
            dummy_target = target_stub or resolve_field(
                self._current(), input_refs[0], net_to_node, role="input")
            fields = resolve_inputs(self._current(), input_refs, dummy_target, net_to_node)
            input_widths = {field.label: field.width for field in fields}
        combined = ""
        for idx, case in enumerate(cases):
            expr = str(case["expression"])
            expr, input_refs = self._expand_candidate_refs(expr, input_refs)
            if idx == len(cases) - 1:
                combined = f"({expr})" if not combined else f"{combined} : ({expr})"
            else:
                cond = self._condition_from_fixed(
                    {str(k): int(v) for k, v in (case.get("fixed_inputs") or {}).items()},
                    input_widths)
                prefix = f"({cond}) ? ({expr})"
                combined = prefix if not combined else f"{combined} : {prefix}"
        return combined, input_refs

    def probe_target(self, target: str,
                     inputs: list[str] | None = None,
                     pattern_num: int = 32,
                     seed: int | None = 0,
                     detail: bool = False) -> str:
        """Profile support/cone/samples for one target without expression search."""
        self._analysis_seen = True
        text = probe_target_data(
            self._current(), target, inputs,
            pattern_num=pattern_num, seed=seed, detail=detail)
        self._ir_recorder.record_tool_text(
            "probe_target",
            {
                "target": target,
                "inputs": inputs,
                "pattern_num": pattern_num,
                "seed": seed,
                "detail": detail,
            },
            text,
            target=target)
        return text

    def fit_template(self, target: str,
                     inputs: list[str] | None = None,
                     templates: str = "linear,product,comparator",
                     pattern_num: int = 512,
                     validation_num: int = 2048,
                     seed: int | None = 0,
                     exhaustive: str = "auto",
                     fixed_inputs: dict[str, int] | None = None,
                     detail: bool = False,
                     format: str = "text") -> str:
        """Run restricted template fitting for one target."""
        memory_hints = self._memory_hits_for(target, inputs, limit=3)
        report = fit_template_data(
            self._current(), target, inputs,
            templates=templates,
            pattern_num=pattern_num,
            validation_num=validation_num,
            seed=seed,
            exhaustive=exhaustive,
            fixed_inputs=fixed_inputs,
            detail=detail,
        )
        report = self._append_memory_notes(report, memory_hints)
        report = self._cache_accepted_candidates(report)
        self._ir_recorder.record_report(
            "fit_template",
            {
                "target": target,
                "inputs": inputs,
                "templates": templates,
                "pattern_num": pattern_num,
                "validation_num": validation_num,
                "seed": seed,
                "exhaustive": exhaustive,
                "fixed_inputs": fixed_inputs,
                "detail": detail,
                "format": format,
                "memory_hints": memory_hints,
            },
            report,
            circuit=self.circuit)
        return format_recovery_report(
            report, detail=detail, format=format)

    def validate_expr(self, target: str,
                      expression: str,
                      inputs: list[str] | None = None,
                      pattern_num: int = 512,
                      validation_num: int = 2048,
                      seed: int | None = 0,
                      exhaustive: str = "auto",
                      fixed_inputs: dict[str, int] | None = None,
                      validate_scope: str = "support",
                      detail: bool = False,
                      format: str = "text") -> str:
        """Validate an explicit expression hypothesis and cache it if accepted."""
        expression, inputs = self._expand_candidate_refs(expression, inputs)
        inputs = self._complete_expression_inputs(expression, inputs)
        report = validate_expr_data(
            self._current(), target, expression, inputs,
            pattern_num=pattern_num,
            validation_num=validation_num,
            seed=seed,
            exhaustive=exhaustive,
            fixed_inputs=fixed_inputs,
            validate_scope=validate_scope,
            detail=detail,
        )
        report = self._cache_accepted_candidates(report)
        self._ir_recorder.record_report(
            "validate_expr",
            {
                "target": target,
                "expression": expression,
                "inputs": inputs,
                "pattern_num": pattern_num,
                "validation_num": validation_num,
                "seed": seed,
                "exhaustive": exhaustive,
                "fixed_inputs": fixed_inputs,
                "validate_scope": validate_scope,
                "detail": detail,
                "format": format,
            },
            report,
            circuit=self.circuit)
        return format_recovery_report(
            report, detail=detail, format=format)

    def combine_case_expr(self, target: str,
                          controls: list[str],
                          cases: list[dict],
                          inputs: list[str] | None = None,
                          pattern_num: int = 512,
                          validation_num: int = 2048,
                          seed: int | None = 0,
                          exhaustive: str = "auto",
                          detail: bool = False,
                          format: str = "text") -> str:
        """Combine branch expressions into a full conditional expression."""
        expression, resolved_inputs = self._combine_case_expression(
            controls, cases, inputs)
        report = validate_expr_data(
            self._current(), target, expression, resolved_inputs,
            pattern_num=pattern_num,
            validation_num=validation_num,
            seed=seed,
            exhaustive=exhaustive,
            fixed_inputs=None,
            validate_scope="support",
            detail=detail,
        )
        report.notes = (
            f"combined {len(cases)} branch expression(s) over controls: {', '.join(controls)}",
            "case combination validates the full target and can be assembled if accepted",
            *report.notes,
        )
        report = self._cache_accepted_candidates(report)
        self._ir_recorder.record_report(
            "combine_case_expr",
            {
                "target": target,
                "controls": controls,
                "cases": cases,
                "inputs": inputs,
                "pattern_num": pattern_num,
                "validation_num": validation_num,
                "seed": seed,
                "exhaustive": exhaustive,
                "detail": detail,
                "format": format,
                "combined_expression": expression,
            },
            report,
            circuit=self.circuit)
        return format_recovery_report(
            report, detail=detail, format=format)

    def infer_native_expr(self, target: str,
                          inputs: list[str] | None = None,
                          mode: str = "auto",
                          max_cost: int | None = None,
                          pattern_num: int = 512,
                          validation_num: int = 2048,
                          seed: int | None = 0,
                          detail: bool = False,
                          format: str = "text") -> str:
        """Run native TABLE-I AST candidates for one target."""
        net_to_node = build_net_to_node(self._current())
        target_field = resolve_field(self._current(), target, net_to_node, role="target")
        input_fields = resolve_inputs(self._current(), inputs, target_field, net_to_node)
        support_bits = support_bit_count(input_fields)
        if support_bits >= 32 and max_cost is None:
            profile = get_tool_profile("infer_native_expr")
            payload = {
                "status": "deferred",
                "reason": "native_search_support_too_wide",
                "target": target,
                "support_bits": support_bits,
                "effort": profile.effort,
                "search_space": profile.search_space,
                "next_steps": [
                    "split_control_cases",
                    "validate_expr(..., fixed_inputs={...})",
                    "combine_case_expr",
                ],
            }
            self._ir_recorder.log_event(
                "tool_deferred",
                payload=payload,
                tool="infer_native_expr",
                target=target,
                status="deferred")
            if format == "json":
                return json.dumps(payload, indent=2, sort_keys=True)
            if format != "text":
                raise ValueError("format must be 'text' or 'json'")
            return "\n".join([
                "Native expression inference deferred — support is too wide for default medium search.",
                f"    target={target} support_bits={support_bits}",
                f"    effort={profile.effort} search_space={profile.search_space} risk={profile.risk}",
                "    Complex targets should be decomposed into control cases and explicit hypotheses.",
                "    Next: split_control_cases -> validate_expr(..., fixed_inputs={...}) -> combine_case_expr.",
                "    To intentionally bound this medium search, pass an explicit max_cost.",
            ])
        report = infer_native_expr_data(
            self._current(), target, inputs,
            mode=mode,
            max_cost=max_cost,
            pattern_num=pattern_num,
            validation_num=validation_num,
            seed=seed,
            detail=detail,
        )
        report = self._cache_accepted_candidates(report)
        self._ir_recorder.record_report(
            "infer_native_expr",
            {
                "target": target,
                "inputs": inputs,
                "mode": mode,
                "max_cost": max_cost,
                "pattern_num": pattern_num,
                "validation_num": validation_num,
                "seed": seed,
                "detail": detail,
                "format": format,
            },
            report,
            circuit=self.circuit)
        return format_recovery_report(
            report, detail=detail, format=format)

    def check_polynomial_lowbits(self, target: str,
                                 inputs: list[str] | None = None,
                                 max_low_bits: int = 8,
                                 detail: bool = False,
                                 format: str = "text") -> str:
        """Run bounded low-bit polynomial rewriting for one target."""
        report = check_polynomial_lowbits_data(
            self._current(), target, inputs,
            max_low_bits=max_low_bits,
            detail=detail,
        )
        report = self._cache_accepted_candidates(report)
        self._ir_recorder.record_report(
            "check_polynomial_lowbits",
            {
                "target": target,
                "inputs": inputs,
                "max_low_bits": max_low_bits,
                "detail": detail,
                "format": format,
            },
            report,
            circuit=self.circuit)
        return format_recovery_report(
            report, detail=detail, format=format)

    def split_control_cases(self, target: str,
                            controls: list[str] | None = None,
                            inputs: list[str] | None = None,
                            pattern_num: int = 128,
                            seed: int | None = 0,
                            detail: bool = False) -> str:
        """Inspect scalar control cases without composing expressions."""
        self._analysis_seen = True
        text = split_control_cases_data(
            self._current(), target, controls, inputs,
            pattern_num=pattern_num, seed=seed, detail=detail)
        self._ir_recorder.record_tool_text(
            "split_control_cases",
            {
                "target": target,
                "controls": controls,
                "inputs": inputs,
                "pattern_num": pattern_num,
                "seed": seed,
                "detail": detail,
            },
            text,
            target=target)
        return text

    def influence_profile(self, target: str,
                          fixed_inputs: dict[str, int] | None = None,
                          inputs: list[str] | None = None,
                          base_inputs: dict[str, int] | None = None,
                          pattern_num: int = 2,
                          seed: int | None = 0,
                          detail: bool = False) -> str:
        """Perturb support bits under a fixed branch and report output deltas."""
        self._analysis_seen = True
        text = influence_profile_data(
            self._current(), target, fixed_inputs, inputs,
            base_inputs=base_inputs,
            pattern_num=pattern_num,
            seed=seed,
            detail=detail,
        )
        self._ir_recorder.record_tool_text(
            "influence_profile",
            {
                "target": target,
                "fixed_inputs": fixed_inputs,
                "inputs": inputs,
                "base_inputs": base_inputs,
                "pattern_num": pattern_num,
                "seed": seed,
                "detail": detail,
            },
            text,
            target=target)
        return text

    def infer_pysr_expr(self, target: str,
                        inputs: list[str] | None = None,
                        force: bool = False,
                        timeout_s: int = 30,
                        niterations: int = 50,
                        detail: bool = False,
                        format: str = "text") -> str:
        """Run PySR only when explicitly forced."""
        if not force:
            profile = get_tool_profile("infer_pysr_expr")
            payload = {
                "status": "deferred",
                "reason": "pysr_requires_force",
                "effort": profile.effort,
                "search_space": profile.search_space,
                "risk": profile.risk,
                "next_step": (
                    "Use probe_target, fit_template, or infer_native_expr "
                    "on a narrowed target before infer_pysr_expr(force=True)."
                ),
            }
            self._ir_recorder.log_event(
                "tool_deferred",
                payload=payload,
                tool="infer_pysr_expr",
                target=target,
                status="deferred")
            if format == "json":
                return json.dumps(payload, indent=2, sort_keys=True)
            if format != "text":
                raise ValueError("format must be 'text' or 'json'")
            return "\n".join([
                "PySR inference deferred — force=True is required",
                f"    effort={profile.effort} search_space={profile.search_space} risk={profile.risk}",
                "    PySR is a heavy fallback, not a default recovery step.",
                "    First narrow the target and try probe_target, fit_template, or infer_native_expr.",
                "    To intentionally run it, call infer_pysr_expr(force=True).",
            ])
        report = infer_pysr_expr_data(
            self._current(), target, inputs,
            timeout_s=timeout_s,
            niterations=niterations,
            detail=detail,
        )
        report = self._cache_accepted_candidates(report)
        self._ir_recorder.record_report(
            "infer_pysr_expr",
            {
                "target": target,
                "inputs": inputs,
                "force": force,
                "timeout_s": timeout_s,
                "niterations": niterations,
                "detail": detail,
                "format": format,
            },
            report,
            circuit=self.circuit)
        return format_recovery_report(
            report, detail=detail, format=format)

    def assemble_rtl(self, candidates: list[str] | None = None,
                     timeout_s: int = 300,
                     detail: bool = False,
                     format: str = "text") -> str:
        """Assemble candidate RTL from accepted local candidates only."""
        circuit = self._current()
        analysis = _analyze_recovery_words(circuit)
        unknown: list[str] = []
        if candidates:
            selected: list[ExpressionCandidate] = []
            for cid in candidates:
                cand = self._accepted_candidates.get(cid)
                if cand is None:
                    unknown.append(cid)
                else:
                    selected.append(cand)
        else:
            selected = list(self._accepted_candidates.values())
        chosen: dict[str, ExpressionCandidate] = {}
        for cand in selected:
            if not cand.verification.accepted:
                continue
            target_word = cand.target.split("[", 1)[0]
            current = chosen.get(target_word)
            cand_key = (
                0 if cand.verification.status.endswith("exact") else 1,
                cand.cost,
                cand.expression,
            )
            current_key = (
                0 if current and current.verification.status.endswith("exact") else 1,
                current.cost if current else 1_000_000,
                current.expression if current else "",
            )
            if current is None or cand_key < current_key:
                chosen[target_word] = cand
        missing = [
            word.name for word in analysis.output_words
            if word.name not in chosen
        ]
        notes = [
            "assemble_rtl consumes accepted local candidates only; it does not run expression recovery.",
        ]
        if unknown:
            notes.append(f"unknown candidate id(s): {', '.join(unknown)}")
        if missing:
            report = RecoveryReport(
                circuit_name=circuit.name,
                word_analysis=analysis,
                candidates=tuple(selected),
                verification=VerificationResult(
                    "not-run",
                    "candidate RTL not emitted because accepted candidates are missing"),
                unrecovered=tuple(missing),
                notes=tuple(notes),
            )
            self._ir_recorder.record_report(
                "assemble_rtl",
                {
                    "candidates": candidates,
                    "timeout_s": timeout_s,
                    "detail": detail,
                    "format": format,
                },
                report,
                circuit=self.circuit)
            return format_recovery_report(report, detail=detail, format=format)

        candidate_rtl = emit_candidate_rtl(circuit, chosen)
        if self._source_code:
            verification = self._verify_candidate_code(candidate_rtl, timeout_s)
        else:
            verification = VerificationResult(
                "not-run", "no original Verilog source is available for CEC")
        report = RecoveryReport(
            circuit_name=circuit.name,
            word_analysis=analysis,
            candidates=tuple(selected),
            candidate_rtl=candidate_rtl,
            verification=verification,
            notes=tuple(notes),
        )
        self._ir_recorder.record_report(
            "assemble_rtl",
            {
                "candidates": candidates,
                "timeout_s": timeout_s,
                "detail": detail,
                "format": format,
            },
            report,
            circuit=self.circuit)
        return format_recovery_report(report, detail=detail, format=format)

    def recover_expression(self, target: str,
                           inputs: list[str] | None = None,
                           methods: str = "auto",
                           control: bool = True,
                           mode: str = "auto",
                           max_cost: int | None = None,
                           pattern_num: int = 4096,
                           validation_num: int = 16384,
                           seed: int | None = 0,
                           timeout_s: int = 120,
                           niterations: int = 200,
                           detail: bool = False,
                           format: str = "text") -> str:
        """Deprecated guardrail for the old multi-method expression search."""
        if format == "json":
            return json.dumps({
                "status": "deprecated",
                "reason": "recover_expression_is_too_broad_for_agent_workflow",
                    "next_steps": [
                    "recovery_tool_guide",
                    "probe_target",
                    "validate_expr",
                    "fit_template",
                    "infer_native_expr",
                    "check_polynomial_lowbits",
                    "split_control_cases",
                    "infer_pysr_expr(force=True) only as a fallback",
                ],
            }, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        return "\n".join([
            "recover_expression is deprecated for agent-facing recovery.",
            "    It no longer runs the broad multi-method search.",
            "    Use probe_target first, then choose one narrow tool:",
            "      - validate_expr when you already have a formula hypothesis",
            "      - fit_template for linear/product/comparator hypotheses",
            "      - infer_native_expr for native TABLE-I AST candidates",
            "      - check_polynomial_lowbits for bounded polynomial evidence",
            "      - split_control_cases for scalar control/mux behavior",
            "      - infer_pysr_expr(force=True) only after narrowed failures",
        ])

    def recover_rtl(self, outputs: list[str] | None = None,
                    timeout_s: int = 300,
                    emit_unverified: bool = False,
                    force: bool = False,
                    pattern_num: int = 4096,
                    validation_num: int = 16384,
                    seed: int | None = 0,
                    niterations: int = 200,
                    detail: bool = False,
                    format: str = "text") -> str:
        """Deprecated full-module baseline; default path is assemble_rtl."""
        if not force:
            lines = [
                "recover_rtl is deprecated for default agent-facing recovery.",
                "    It no longer runs per-output expression recovery unless force=True is set.",
                "    Use plan_recovery, then small tools per target, then assemble_rtl.",
                "    To intentionally run the old heavy baseline, call recover_rtl(force=True).",
            ]
            if self._analysis_seen:
                lines.append(
                    "    NOTE: previous analysis is present; accepted candidates still need assemble_rtl.")
            if format == "json":
                return json.dumps({
                    "status": "deferred",
                    "reason": "recover_rtl_deprecated_without_force",
                    "next_step": "plan_recovery(detail=True)",
                    "assembly_step": "assemble_rtl()",
                    "force_override": "recover_rtl(force=True)",
                }, indent=2, sort_keys=True)
            if format != "text":
                raise ValueError("format must be 'text' or 'json'")
            return "\n".join(lines)

        verify = self._verify_candidate_code if self._source_code else None
        report = recover_rtl_data(
            self._current(),
            outputs=outputs,
            source_code=self._source_code,
            verify_func=verify,
            timeout_s=timeout_s,
            emit_unverified=emit_unverified,
            pattern_num=pattern_num,
            validation_num=validation_num,
            seed=seed,
            niterations=niterations,
            detail=detail,
        )
        return format_recovery_report(report, detail=detail, format=format)

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

    def _candidate_port_guard(self, candidate_code: str) -> VerificationResult | None:
        circuit = self._current()
        expected = list(getattr(circuit, "module_ports", []) or [])
        actual = _module_ports_from_code(_unescape(candidate_code))
        if not expected or not actual:
            return None
        expected_set = set(expected)
        actual_set = set(actual)
        if expected_set != actual_set:
            missing = sorted(expected_set - actual_set)
            extra = sorted(actual_set - expected_set)
            return VerificationResult(
                "failed",
                "port_mismatch: candidate top module ports do not match original "
                f"(missing={missing or '[]'} extra={extra or '[]'})",
            )
        if expected != actual:
            return VerificationResult(
                "failed",
                "port_order_mismatch: candidate top module port order differs from "
                "the original; ABC/Yosys CEC maps ports by index here, so a "
                "counterexample would be unreliable. "
                f"expected={expected} actual={actual}",
            )
        return None

    def _verify_candidate_code(self, candidate_code: str,
                               timeout_s: int = 300) -> VerificationResult:
        """Run CEC between the loaded Verilog source and candidate RTL."""
        if not self._source_code:
            return VerificationResult(
                "not-run", "no original Verilog source is available for CEC")
        port_guard = self._candidate_port_guard(candidate_code)
        if port_guard is not None:
            return port_guard
        result = self._run_cec(
            self._source_code, _unescape(candidate_code), timeout_s=timeout_s)
        reason = result.get("reason", "unknown")
        if result.get("success") and reason == "equivalent":
            return VerificationResult(
                "cec-proved", reason, result.get("elapsed", 0.0))
        if result.get("success") and reason == "timeout_assumed_equivalent":
            return VerificationResult(
                "cec-timeout-assumed", reason, result.get("elapsed", 0.0))
        return VerificationResult(
            "failed", reason, result.get("elapsed", 0.0),
            result.get("counterexample"))

    def verify_rtl_candidate(self, candidate_code: str,
                             timeout_s: int = 300,
                             format: str = "text") -> str:
        """Verify an externally supplied candidate RTL module against source."""
        result = self._verify_candidate_code(candidate_code, timeout_s=timeout_s)
        self._ir_recorder.log_event(
            "rtl_verification",
            payload={
                "timeout_s": timeout_s,
                "verification": result.to_dict(),
                "candidate_preview": _unescape(candidate_code)[:2000],
            },
            tool="verify_rtl_candidate",
            status=result.status)
        self._ir_recorder.record_cex(result, source="verify_rtl_candidate")
        if format == "json":
            return json.dumps(result.to_dict(), indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        lines = [f"RTL candidate verification: {result.status}"]
        if result.reason:
            lines.append(f"    reason : {result.reason}")
        if result.elapsed:
            lines.append(f"    elapsed: {result.elapsed:.1f}s")
        if result.counterexample:
            lines.append("    counterexample:")
            lines.append(json.dumps(result.counterexample, indent=2, sort_keys=True))
        return "\n".join(lines)

    def _rtl_code_from_input(self, candidate_code: str = "",
                             path: str = "") -> str:
        if candidate_code:
            return _unescape(candidate_code)
        if path:
            return Path(path).read_text(encoding="utf-8")
        if self._current_code:
            return self._current_code
        if self._source_code:
            return self._source_code
        raise ValueError("provide candidate_code or path, or load a Verilog source first")

    def analyze_rtl_cost(self, candidate_code: str = "",
                         path: str = "",
                         top_n: int = 10,
                         format: str = "text") -> str:
        """Analyze RTL cost hotspots without changing or verifying code."""
        code = self._rtl_code_from_input(candidate_code, path)
        payload = analyze_rtl_cost_data(code, top_n=top_n)
        payload["total_cost"] = self._compute_cost(code)
        if format == "json":
            return json.dumps(payload, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        lines = [
            "RTL cost analysis",
            f"    total_cost      : {payload['total_cost']}",
            f"    assignment_count: {payload['assignment_count']}",
            "",
            "== Cost hotspots ==",
        ]
        for item in payload["hotspots"]:
            lines.append(
                f"  - cost={item['cost']} lhs={item['lhs']} rhs={item['rhs']}")
        if payload["duplicate_expressions"]:
            lines.append("")
            lines.append("== Duplicate expressions ==")
            for item in payload["duplicate_expressions"]:
                lines.append(
                    f"  - count={item['count']} saving~{item['estimated_saving']} "
                    f"lhs={', '.join(item['lhs'])} rhs={item['rhs']}")
        if payload["repeated_fragments"]:
            lines.append("")
            lines.append("== Repeated fragments ==")
            for item in payload["repeated_fragments"]:
                lines.append(
                    f"  - {item['kind']} count={item['count']} fragment={item['fragment']}")
        if payload["hints"]:
            lines.append("")
            lines.append("== Suggested rewrite families ==")
            lines.extend(f"  - {hint}" for hint in payload["hints"])
        return "\n".join(lines)

    def propose_rtl_rewrites(self, candidate_code: str = "",
                             path: str = "",
                             strategies: str = "all",
                             max_candidates: int = 8,
                             format: str = "text") -> str:
        """Generate bounded cost-guided RTL rewrite candidates."""
        code = self._rtl_code_from_input(candidate_code, path)
        base_cost = self._compute_cost(code)
        candidates = propose_rtl_rewrites_data(
            code, strategies=strategies, max_candidates=max_candidates)
        rows = []
        for cand in candidates:
            try:
                new_cost = self._compute_cost(cand.code)
            except Exception as exc:
                new_cost = None
                reason = f"cost_error: {exc}"
            else:
                reason = ""
            row = cand.to_dict(include_code=(format == "json"))
            row["base_cost"] = base_cost
            row["candidate_cost"] = new_cost
            row["cost_delta"] = None if new_cost is None else new_cost - base_cost
            if reason:
                row["reason"] = f"{row['reason']}; {reason}"
            rows.append(row)
        payload = {
            "base_cost": base_cost,
            "candidate_count": len(rows),
            "candidates": rows,
        }
        if format == "json":
            return json.dumps(payload, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        lines = [
            "RTL rewrite proposals",
            f"    base_cost : {base_cost}",
            f"    candidates: {len(rows)}",
        ]
        if not rows:
            lines.append("  (empty)")
            return "\n".join(lines)
        for row in rows:
            lines.append(
                f"  - {row['candidate_id']} strategy={row['strategy']} "
                f"cost={row['candidate_cost']} delta={row['cost_delta']} "
                f"reason={row['reason']}")
        lines.append("Use format=json to retrieve candidate code for verification or editing.")
        return "\n".join(lines)

    def optimize_rtl_round(self, candidate_code: str = "",
                           path: str = "",
                           strategies: str = "all",
                           max_candidates: int = 8,
                           timeout_s: int = 60,
                           accept_timeout: bool = True,
                           format: str = "text") -> str:
        """Run one bounded cost-guided rewrite round with cost and CEC checks."""
        code = self._rtl_code_from_input(candidate_code, path)
        base_cost = self._compute_cost(code)
        proposals = propose_rtl_rewrites_data(
            code, strategies=strategies, max_candidates=max_candidates)
        rows = []
        accepted = []
        for proposal in proposals:
            row = proposal.to_dict(include_code=(format == "json"))
            row["base_cost"] = base_cost
            try:
                candidate_cost = self._compute_cost(proposal.code)
            except Exception as exc:
                row.update({
                    "candidate_cost": None,
                    "cost_delta": None,
                    "cec_status": "not-run",
                    "accepted": False,
                    "reject_reason": f"cost_error: {exc}",
                })
                rows.append(row)
                continue
            row["candidate_cost"] = candidate_cost
            row["cost_delta"] = candidate_cost - base_cost
            if candidate_cost >= base_cost:
                row.update({
                    "cec_status": "not-run",
                    "accepted": False,
                    "reject_reason": "cost_not_lower",
                })
                rows.append(row)
                continue
            verification = self._verify_candidate_code(
                proposal.code, timeout_s=timeout_s)
            row["cec_status"] = verification.status
            row["cec_reason"] = verification.reason
            row["cec_elapsed"] = verification.elapsed
            is_accepted = verification.status == "cec-proved" or (
                accept_timeout and verification.status == "cec-timeout-assumed")
            row["accepted"] = is_accepted
            if not is_accepted:
                row["reject_reason"] = verification.reason or verification.status
            rows.append(row)
            if is_accepted:
                accepted.append(row)
        best = None
        if accepted:
            best = min(accepted, key=lambda item: (item["candidate_cost"], item["candidate_id"]))
        payload = {
            "base_cost": base_cost,
            "timeout_s": timeout_s,
            "candidate_count": len(rows),
            "accepted_count": len(accepted),
            "best": best,
            "candidates": rows,
        }
        self._ir_recorder.add_graph_node(
            "experiment",
            target="",
            summary="cost-guided RTL optimization round",
            data={
                "base_cost": base_cost,
                "timeout_s": timeout_s,
                "accepted_count": len(accepted),
                "best_strategy": best.get("strategy", "") if best else "",
                "best_cost": best.get("candidate_cost") if best else None,
            },
        )
        if format == "json":
            return json.dumps(payload, indent=2, sort_keys=True)
        if format != "text":
            raise ValueError("format must be 'text' or 'json'")
        lines = [
            "RTL optimization round",
            f"    base_cost : {base_cost}",
            f"    timeout_s : {timeout_s}",
            f"    candidates: {len(rows)}",
            f"    accepted  : {len(accepted)}",
        ]
        if best:
            lines.append(
                f"    best      : {best['candidate_id']} {best['strategy']} "
                f"cost={best['candidate_cost']} delta={best['cost_delta']} "
                f"cec={best['cec_status']}")
        for row in rows:
            lines.append(
                f"  - {row['candidate_id']} strategy={row['strategy']} "
                f"cost={row.get('candidate_cost')} delta={row.get('cost_delta')} "
                f"cec={row.get('cec_status')} accepted={row.get('accepted')} "
                f"reason={row.get('reject_reason', row.get('reason', ''))}")
        lines.append("Use format=json to retrieve the best candidate code.")
        return "\n".join(lines)

    def _run_cec(self, old_code: str, new_code: str,
                 timeout_s: int = 310) -> dict:
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
                 "--timeout", str(timeout_s)],
                capture_output=True, text=True, timeout=timeout_s + 10,
            )
            return json.loads(proc.stdout.strip())
        except subprocess.TimeoutExpired:
            return {
                "success": True,
                "reason": "timeout_assumed_equivalent",
                "output": (
                    f"ABC CEC timed out after {timeout_s + 10}s — "
                    "assumed equivalent"),
                "elapsed": float(timeout_s + 10),
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
