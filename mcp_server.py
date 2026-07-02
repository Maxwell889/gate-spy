"""MCP server (demo): a thin FastMCP frontend over :class:`CircuitSession`.

Run with stdio transport: ``uv run python mcp_server.py``.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.session import CircuitSession

mcp = FastMCP("gate-spy")
session = CircuitSession()


@mcp.tool()
def read_file(path: str) -> str:
    """Load a circuit file and make it the current circuit.

    Accepts gate-level Verilog (``.v``/``.sv``/``.verilog``, parsed against the
    default example library) or AIGER (``.aig``/``.aag``).  Returns the circuit
    summary and the input/output port signal names — use those as starting points
    with ``get_node`` to explore the circuit neighbourhood.

    Naming conventions:
    - Ports are listed as ``base[0..N-1] (N)`` for buses or plain names for scalars.
    - For Verilog: internal wires keep their names from Verilog (e.g. _0001_).
    - For AIGER: internal nodes are named ``n{v}`` for AND gates and ``n{v}_n``
      for their dedicated inverters (literal negation produces one NOT per gate).
    """
    return session.load(path)


@mcp.tool()
def get_node(node: str, depth: int = 2, detail: bool = False) -> str:
    """Inspect a signal, bus, or group of signals in the current circuit.

    ``node`` can be:
    - a port signal name (e.g. ``"Out[5]"``, ``"IN1[0]"``, ``"a"``)
    - an internal signal name (e.g. ``"_0001_"`` for Verilog, ``"n33"`` for AIGER)
    - a numeric node id
    - a **bus base name** (e.g. ``"out1"``) — shows a summary of all bits
    - a **prefix** or **wildcard pattern** (e.g. ``"csa_tree_*"``, ``"*adder*"``)
      — searches for matching internal signals, grouped by common prefix

    **Single-node mode**: returns the node's kind, basic info, and its fan-in /
    fan-out cones up to ``depth`` levels (default 2).  When ``detail`` is True,
    all neighbours are listed; otherwise at most 16 per level.

    **Bus mode** (when the name matches ``basename[N]`` bits): returns width,
    driver gate kinds, and example bit nets.

    **Search mode** (when a wildcard ``*`` / ``?`` is used, or an exact match
    fails but a prefix matches internal signals): returns matching signal groups
    with gate-type histograms and example net names.
    """
    return session.node_info(node, depth, detail=detail)


# @mcp.tool()
# def extract_subgraph(outputs: list[str],
#                      inputs: list[str] | None = None,
#                      out_path: str = "subgraph.v") -> str:
#     """Extract the backward-cone sub-circuit that feeds ``outputs`` and write it.

#     ``outputs`` (required) become the sub-circuit's primary outputs.  ``inputs``
#     (optional) is a list of signals to *cut* the fan-in walk at — each one that
#     is reached becomes a primary input and the walk does not go past it.  Any
#     dependency NOT covered by ``inputs`` is followed all the way to the circuit's
#     own primary inputs, which then become inputs of the sub-circuit too.

#     This extraction is *total*: it never fails with "outside the subgraph".  If
#     you under-specify ``inputs``, the missing dependencies simply surface as
#     extra primary inputs (the true support set), and the report flags them under
#     ``auto-added`` so the boundary is never silently wrong.  Omit ``inputs``
#     entirely to extract the full cone down to primary inputs.

#     Signal names come from ``read_file`` ports or ``get_node`` / ``find_cone``
#     internal names.  The output format follows the file extension of ``out_path``:

#     - ``.v`` (default) — structural Verilog with library cells
#     - ``.aig`` — binary AIGER, via ``scripts/v2aig.sh``

#     Returns a summary of the written circuit plus its resolved input list.
#     """
#     return session.extract_subcircuit(outputs, inputs, out_path)


@mcp.tool()
def find_cone(signals: list[str],
              direction: str,
              stop_at: list[str] | None = None,
              depth: int = -1,
              detail: bool = False) -> str:
    """Trace the fan-in (``backward``) or fan-out (``forward``) cone of a set of
    starting signals, returning the cone layer-by-layer.

    Use this to discover the boundary of a sub-circuit:

    - ``direction="backward"`` traces **upstream** (fan-in / inputs) to find
      which primary inputs (or ``stop_at`` signals) a set of signals depends on.
      Example: given 36 adder-tree result signals, ``backward`` tells you those
      results ultimately depend on ``io_a[0:10], io_b[0:10]`` — the mantissa bits.
    - ``direction="forward"`` traces **downstream** (fan-out) to find where a
      set of signals converges, e.g. which product-bit signals emerge before
      normalisation logic.

    ``stop_at`` is a list of signal names where traversal halts — the signals
    are included in the report but not expanded further.  This lets you "clip"
    the cone at a known boundary (e.g. the normalisation shifter inputs).

    CAUTION: when ``stop_at`` signals depend on each other (one is upstream of
    another), the upstream one is *masked* — traversal halts at the downstream
    one first, so the upstream never appears as a boundary.  Do NOT read "absent
    from the boundary" as "does not reach the start signals".  To test whether a
    single signal actually reaches a primary output, trace it on its own with
    ``direction="forward"`` and no ``stop_at`` instead.

    ``depth`` caps the number of levels (0 = start signals only, -1 = unlimited).
    ``detail=True`` lists every node at every layer; otherwise each layer is
    capped at 16 entries with an overflow count.

    Returns a structured, layer-by-layer report: a header with parameters, one
    section per distance level, a boundary-signal breakdown grouped by *why*
    traversal stopped, and a compact summary with bus ranges.
    """
    return session.cone_report(signals, direction, stop_at=stop_at,
                               depth=depth, detail=detail)


@mcp.tool()
def simulate(pattern_num: int = 100,
             fixed_inputs: dict[str, int] | None = None,
             watch: list[str] | None = None,
             seed: int | None = None) -> str:
    """Simulate the current circuit and report bus values across input patterns.

    With no arguments, applies ``pattern_num`` (default 100) random input
    patterns and reports, for each pattern, every output bus as a decimal
    integer (and each input bus, so the rows are interpretable).

    Pin specific inputs with ``fixed_inputs`` — a mapping whose keys are:
    - a bus base (``"IN1"``) → the integer is spread over its bits (MSB..LSB),
    - a single bit (``"IN1[0]"``), or
    - a scalar port name.
    Pinned bits stay constant across patterns; the rest are randomised. If every
    input bit is pinned, exactly one (deterministic) pattern is run.

    Use ``watch`` to surface internal signals: pass a single net name
    (``"_0007_"``, ``"n33"``) to see its raw bit, or an internal bus base
    (``"sum"``) to gather its bits into an integer column.

    ``seed`` makes the random patterns reproducible.

    Inputs are validated, not silently coerced. An unknown input name, a single
    bit set to anything but 0/1, or a bus integer that overflows its width all
    raise an error naming the input and its allowed range. When a bus is only
    partially pinned, the report header flags it with a NOTE (its remaining bits
    are randomised), so a partial column is never mistaken for a chosen value.
    """
    return session.simulate(pattern_num, fixed_inputs, watch, seed)


@mcp.tool()
def print_xor_stats(detail: bool = False) -> str:
    """Extract XOR gates from the current circuit and report their chains.

    Reports the XOR count, the longest XOR chain (the signals it threads
    through).  The ``detail`` flag is accepted for API consistency.
    """
    return session.xor_stats(detail=detail)


@mcp.tool()
def print_adder_stats(detail: bool = False) -> str:
    """Extract half/full adders from the current circuit and report adder trees.

    Reports the HA/FA counts and the largest connected adder tree (its size,
    composition, carry-depth, and the outputs it drives).  With ``detail=True``,
    all boundary signals are listed without truncation.
    """
    return session.adder_stats(detail=detail)


# ---------------------------------------------------------------------------
#  Word-level hypothesis workflow tools
# ---------------------------------------------------------------------------

@mcp.tool()
def propose_strategy(output: str = "",
                     detail: bool = False,
                     budget: dict | None = None) -> str:
    """Rank recovery lanes for one or all output words.

    Use this at the start of recovery for an output word/group to decide which
    lane is worth trying first.  The report compares:

    - ``template``: built-in candidates plus LLM-defined basis/template fitting,
    - ``polynomial``: bounded PO-to-PI structural rewrite,
    - ``symbolic``: sample-driven symbolic search.

    ``output=""`` summarizes all output words.  ``budget`` may set limits such
    as ``max_nodes`` and ``max_expr_chars``.

    Returns a prioritization report with support/cone metrics, budget estimates,
    and recommended method calls.  It is a hint, not a decision authority: it
    does not create hypotheses, run proof, edit RTL, or change source state.
    """
    return session.propose_strategy(
        output=output, detail=detail, budget=budget)


@mcp.tool()
def run_method(output: str,
               method: str,
               sample_num: int = 256,
               budget: dict | None = None,
               detail: bool = False) -> str:
    """Run one recovery lane without editing source code.

    ``method`` accepts:

    - ``template``: delegate to ``infer_candidates`` for this output,
    - ``polynomial``: expand a bounded PO-to-PI expression and sample-check it,
    - ``symbolic``: run deterministic word/sketch/grammar searches on samples.

    Returns candidate expressions, sample-check status, hypothesis ids, and
    budget status such as skipped/expanded/timeout.  Returned expressions are
    evidence only.  This tool may store hypotheses for later tracing, but it
    never modifies the current recovered RTL and never proves equivalence.
    """
    return session.run_method(
        output=output,
        method=method,
        sample_num=sample_num,
        budget=budget,
        detail=detail,
    )


@mcp.tool()
def explain_failure(hypothesis_id: int | str | None = None,
                    output: str = "",
                    depth: int = 3) -> str:
    """Analyze a failed hypothesis and recommend the next recovery action.

    Use this after ``check_hypothesis``, ``run_method``, or ``infer_candidates``
    reports a mismatch, counterexample, timeout, or no-fit state.  If
    ``hypothesis_id`` is omitted, the latest relevant failed hypothesis is used;
    ``output`` can narrow the report to one output word.

    Returns the failure state, smallest known contradiction when available,
    likely causes such as width/signedness/selector/missing term, and a suggested
    next tool call.  It does not edit RTL or prove anything.
    """
    return session.explain_failure(
        hypothesis_id=hypothesis_id, output=output, depth=depth)


@mcp.tool()
def infer_candidates(output: str = "",
                     methods: list[str] | None = None,
                     sample_num: int = 256,
                     detail: bool = False) -> str:
    """Generate initial word-level expression candidates for output words.

    This is an LLM workbench tool, not an automatic rewrite.  It uses output
    cones to find support input words, runs deterministic/random samples, and
    fits built-in arithmetic, bit-selection, comparison, and MUX templates.

    ``output=""`` attempts all output words; for many-output cases it may emit a
    compact batch ``assignments_json`` map that can be passed directly to
    ``check_hypothesis``.  ``detail=True`` includes more structure and fit
    diagnostics.

    Returns support words, cone summaries, candidate expressions, sample-fit
    status, hypothesis ids, and sometimes ``assignments_json``.  Candidates are
    stored for tracing but are not proof and do not modify source.
    """
    return session.infer_candidates(
        output=output, methods=methods, sample_num=sample_num, detail=detail)


@mcp.tool()
def check_hypothesis(assignments: dict[str, str],
                     declarations: str = "",
                     sample_num: int = 256,
                     run_cec: bool = True,
                     share_common: bool = True) -> str:
    """Check an LLM-proposed word-level hypothesis without editing the source.

    ``assignments`` maps output word names to expressions, for example
    ``{"out3": "in1 * (in2 + in3 + in4) + in5"}``.  ``declarations`` can add
    helper wires/regs needed by those expressions, including simple signed
    helper declarations such as ``wire signed [7:0] sa = a;``.  Sample checking
    understands ``$signed(...)`` and ``$unsigned(...)`` casts.

    The tool compares samples against the loaded gate-level circuit.  With
    ``share_common=True`` it conservatively extracts repeated additive
    subexpressions into local wires, re-checks samples, and then computes
    cost/CEC on the shared form.  If every output word is assigned and
    ``run_cec`` is true, it renders temporary RTL and runs CEC; partial-output
    hypotheses are sample-checked but CEC is skipped.

    Returns sample status, mismatches or CEX information, CEC status when run,
    rendered/costed RTL notes, and contest cost.  It does not modify the current
    recovered source; use ``edit`` to apply a proved rewrite.
    """
    return session.check_hypothesis(
        assignments=assignments, declarations=declarations,
        sample_num=sample_num, run_cec=run_cec, share_common=share_common,
    )


@mcp.tool()
def fit_hypothesis(output: str,
                   template: str,
                   unknowns: list[str] | dict | None = None,
                   sample_num: int = 256) -> str:
    """Fit integer coefficients for an LLM-proposed expression template.

    ``template`` should reference real input word names and unknown identifiers.
    ``unknowns`` may be a list (default domains) or a mapping such as
    ``{"c": {"min": -512, "max": 512}, "a": [-1, 0, 1]}``.  Small problems use
    grid search; large affine templates such as ``c0*x0 + c1*x1 + ...`` are
    solved directly instead of enumerating all coefficient combinations.

    Returns solved coefficients and an expression, or a no-fit/budget reason
    with sample diagnostics.  The fitted expression is a candidate only; this
    tool does not edit source or run full-module proof.
    """
    return session.fit_hypothesis(
        output=output, template=template, unknowns=unknowns,
        sample_num=sample_num,
    )


@mcp.tool()
def fit_basis(output: str,
              basis: list[str],
              include_constant: bool = True,
              coefficient_limit: int = 4096,
              sample_num: int = 256) -> str:
    """Fit an LLM-supplied expression basis for one output word.

    This is the open-ended alternative to adding more built-in templates.  The
    model proposes basis terms such as ``["in1", "in2", "in1 * in2",
    "sel ? in5 : 0", "in8 << 3"]``; GateSpy solves
    ``const + sum(coeff_i * basis_i)`` and verifies the resulting expression on
    samples.

    Returns coefficients, a fitted expression, sample status, and no-fit reasons
    when applicable.  Use ``check_hypothesis`` on the returned expression before
    edit; this tool does not change source.
    """
    return session.fit_basis(
        output=output,
        basis=basis,
        include_constant=include_constant,
        coefficient_limit=coefficient_limit,
        sample_num=sample_num,
    )


@mcp.tool()
def trace_counterexample(hypothesis_id: int | str | None = None,
                         output: str = "",
                         bits: list[int] | None = None,
                         depth: int = 3) -> str:
    """Replay a failed hypothesis and report mismatch-focused debug context.

    Use this after a sample mismatch or CEC counterexample.  If
    ``hypothesis_id`` is omitted, the latest failed hypothesis is used.
    ``output`` and ``bits`` narrow the trace; ``depth`` controls related cone
    context.

    Returns mismatch output bits/words, input valuation, old/new values, watched
    candidate subterms when available, and fan-in cone hints.  It is for
    diagnosis only and does not edit or prove the design.
    """
    return session.trace_counterexample(
        hypothesis_id=hypothesis_id, output=output, bits=bits, depth=depth)


# ---------------------------------------------------------------------------
#  Code-modification workflow tools
# ---------------------------------------------------------------------------

@mcp.tool()
def edit(
    matches: list[str] | None = None,
    replacements: list[str] | None = None,
    rewrite: str = "",
    begin: str = "",
    end: str = "",
    accept_timeout: bool = False,
) -> str:
    """Apply a verified edit to the current recovered Verilog source code.

    Three modes, evaluated in priority order:

    1. **Full rewrite** — ``rewrite`` is non-empty.
       Replaces the **entire source file** with *rewrite*.
       Example: ``rewrite="module top(a,b,c); assign c = a + b; endmodule"``

    2. **Region replace** — ``begin`` and ``end`` are both non-empty.
       Each is a regex that must match **exactly once**.  The span from
       the start of the *begin* match through the end of the *end* match
       (inclusive) is replaced by ``replacements[0]``.
       Example: ``begin=r"wire n_2,"``, ``end=r"endmodule"``

    3. **Exact-string replace** (default) — ``matches`` + ``replacements``.
       Each string in ``matches`` must appear **exactly once** in the
       current code.  Replacements are applied in order.

    For multi-output modules, provide complete RTL or preserve every existing
    output explicitly.  Partial rewrites can leave outputs at default values and
    create misleading counterexamples.

    After applying the temporary edit, CEC verifies functional equivalence
    between the old and new code.  If it fails (syntax error or functional
    mismatch) the edit is **rejected** — the current code is NOT changed and
    nothing is recorded.  Timeout is also rejected unless
    ``accept_timeout=True`` is set.

    On success the ICCAD-2022-Problem-A cost is computed and the edit is
    recorded so it can be reverted later with ``revert``.

    Returns accepted/rejected status, CEC state, cost before/after, reduction
    rate, and modification id when accepted.  This is the only hypothesis
    workflow tool that modifies current recovered source.
    """
    return session.edit(
        matches=matches, replacements=replacements,
        rewrite=rewrite, begin=begin, end=end,
        accept_timeout=accept_timeout,
    )


@mcp.tool()
def revert(depth: int = 1, id: int = 0, help: bool = False) -> str:
    """Revert accepted edits or show the modification history.

    Parameters
    ----------
    help : bool
        If True, show the full modification history table (id, time, cost
        delta, status) instead of reverting.
    depth : int
        Number of edits to revert (default 1).  Ignored when *id* is set.
    id : int
        Revert to this modification id — all edits with id >= *id* are
        dropped.  Use *id* to jump back to a specific state.  ``id=0``
        means "not set" (the *depth* parameter is used instead).

    Returns either the edit history table or a confirmation of the restored
    source state.  It only affects edits previously accepted by ``edit``.
    """
    return session.revert(depth=depth, id=id, help=help)


@mcp.tool()
def dump(path: str) -> str:
    """Write the current accepted Verilog source code to *path*.

    Use this only after proof and cost audit.  ``dump`` does not run CEC,
    compute cost, or decide whether the current source is solved; it simply
    writes the current session source and returns a confirmation with file size.
    """
    return session.dump(path)


@mcp.tool()
def show(detail: bool = False, grep: str = "") -> str:
    """Return the current recovered Verilog source code with optional filtering.

    Parameters
    ----------
    detail : bool
        If False (default), return a uniformly-sampled abridged version
        (blocks of lines interleaved with skip markers).  If True, return
        the complete source.
    grep : str
        A regex pattern.  When non-empty, only matching lines are shown,
        each with ±5 lines of surrounding context.  Overlapping context
        windows are merged.  Ignores *detail* when set.

    Returns source text only.  It is the preferred way to re-read the current
    RTL before cost audit or ``dump``; it does not run proof or modify state.
    """
    return session.show(detail=detail, grep=grep)


if __name__ == "__main__":
    mcp.run()
