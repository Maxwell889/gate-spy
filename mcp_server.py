"""MCP server (demo): a thin FastMCP frontend over :class:`CircuitSession`.

Run with stdio transport: ``uv run python mcp_server.py``.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from src.session import CircuitSession

mcp = FastMCP("gate-spy")
session = CircuitSession()


@mcp.tool()
def read_file(path: str,
              record_ir: bool = True,
              case_id: str = "",
              out_dir: str = "") -> str:
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
    By default this also starts a persistent recovery reasoning graph under
    ``.gate_spy/runs/<run_id>/``.  ``record_ir`` is kept for compatibility, but
    it now means graph-only recording: no raw ``events.jsonl`` tool log, no full
    report text, no transcript, and no candidate files unless final assembly
    succeeds.  The run directory contains compact ``graph.json``,
    ``summary.json``, ``graph.jsonl`` after the first graph node, and optionally
    ``final_candidate.v``.

    Use ``record_recovery_graph_node`` to record learning-value nodes:
    ``problem -> experiment -> observation -> relation -> validation ->
    experience``.  Pass ``record_ir=False`` for ad hoc structure queries that
    should not write run artifacts.  ``case_id`` and ``out_dir`` optionally
    override the inferred case id and run base directory.
    """
    return session.load(
        path,
        record_ir=record_ir,
        case_id=case_id or None,
        out_dir=out_dir or None)


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
def analyze_words(outputs: list[str] | None = None,
                  max_low_bits: int = 10,
                  detail: bool = False,
                  format: str = "text") -> str:
    """Recover word boundaries and support cuts for RTL recovery.

    This is the first step before attempting word-level RTL reconstruction.  It
    groups declared PI/PO buses, traces each selected output's backward cone to
    primary-input support words, and reports low-bit support-inclusion evidence
    that helps identify arithmetic word ordering.

    ``outputs`` optionally restricts analysis to selected output refs.  Use
    ``format="json"`` when another tool should consume the result.
    """
    return session.analyze_words(outputs, max_low_bits, detail, format)


@mcp.tool()
def plan_recovery(outputs: list[str] | None = None,
                  feedback: dict | str | None = None,
                  detail: bool = False,
                  format: str = "text") -> str:
    """Create or revise an agent-facing RTL recovery plan.

    Use this before attempting full-module RTL recovery.  The plan groups
    outputs by support/cone/role, recommends per-cluster tools and parameters,
    and records guardrails for the analysis -> local recovery -> feedback ->
    revised-plan loop.

    When a local recovery knowledge base exists, the plan may include advisory
    memory hints with provenance.  These hints only prioritize hypotheses; they
    are not accepted evidence until a normal validation tool proves them.

    Pass ``feedback`` after a failed or inconclusive attempt.  Useful feedback
    includes CEC counterexamples, timeout notes, failed-validation summaries, or
    unrecovered targets.  The returned plan will include revision guidance.

    Important: a failure is usually evidence that the current hypothesis,
    validation scope, support boundary, or control assignment is wrong.  Do not
    explain failure away as "the circuit is complex" and continue natural
    language reasoning.  Put the falsified hypothesis, omitted/variable support,
    CEX pattern, and next smaller experiment into ``feedback`` so the plan can
    choose a better local tool or decomposition.
    """
    return session.plan_recovery(outputs, feedback, detail, format)


@mcp.tool()
def recovery_tool_guide(format: str = "text") -> str:
    """List recovery tools by effort, search space, risk, and prerequisites.

    Use this when choosing between verification, light templates, medium native
    enumeration, and heavy fallback tools.  The intended order is
    probe -> validate explicit hypothesis -> light tool -> medium tool -> heavy
    tool, shrinking the target before escalating.  After a failed local attempt,
    first audit whether the tool report shows ``omitted_support``, remaining
    scalar support under ``fixed_inputs``, slice-only evidence, or an incomplete
    control assignment.  Escalate only after that audit produces a narrower
    experiment that still fails.
    """
    return session.recovery_tool_guide(format)


@mcp.tool()
def start_recovery_run(case_id: str = "",
                       out_dir: str = "",
                       format: str = "text") -> str:
    """Start or restart graph-only recovery reasoning recording.

    Use this if ``read_file(record_ir=False)`` was used or if a new run boundary
    is needed.  ``record_ir=True`` and this tool both create a graph-only run:
    no ordinary tool log, no raw report text, no full transcript.  The durable
    files are ``graph.json``, ``summary.json``, ``graph.jsonl`` after the first
    node, and optional ``final_candidate.v`` after successful assembly.
    """
    return session.start_recovery_run(
        case_id=case_id or None,
        out_dir=out_dir or None,
        format=format)


@mcp.tool()
def export_recovery_ir(format: str = "json") -> str:
    """Export the active recovery reasoning graph snapshot.

    Compatibility name: this no longer exports a raw tool trace.  It exports
    compact graph nodes only: problems, experiments, observations, relations,
    validations, and experiences.  Ordinary tool output, full reports, text
    previews, and transcripts are intentionally absent.
    """
    return session.export_recovery_ir(format)


@mcp.tool()
def record_recovery_graph_node(node_type: str,
                               target: str = "",
                               summary: str = "",
                               data: dict | None = None,
                               links: list[str] | None = None,
                               promotable: bool = False,
                               format: str = "text") -> str:
    """Record one compact recovery reasoning graph node.

    Allowed ``node_type`` values:
    - ``problem``: failed validation, CEX, timeout, template miss, tool mismatch.
    - ``experiment``: directed simulation, influence profile, CEX-derived check,
      or temporary script path. Store only compact parameters/results.
    - ``observation``: small word-level sample pattern or support/control fact.
    - ``relation``: inferred relation family such as successor equality,
      signed affine compare, or masked interaction.
    - ``validation``: full-support validation or CEC result.
    - ``experience``: reusable lesson after validation, normally via promotion.

    This is the preferred way to make model reasoning durable.  Each failure
    should create at least one ``problem`` node and one follow-up ``experiment``
    node.  Do not store raw tool text, full transcripts, bit patterns, or long
    scripts; put temporary scripts under ``/private/tmp/gate-spy-*`` and record
    only their path and compact findings.
    """
    return session.record_recovery_graph_node(
        node_type=node_type,
        target=target,
        summary=summary,
        data=data,
        links=links,
        promotable=promotable,
        format=format)


@mcp.tool()
def query_recovery_experience(target: str = "",
                              inputs: list[str] | None = None,
                              features: dict | None = None,
                              limit: int = 5,
                              format: str = "text") -> str:
    """Query promoted recovery experience graph records.

    Retrieval is deterministic local RAG over graph-derived experience:
    triggering conditions, suggested small experiments, induced template
    families, relation families, validation source, and negative stop rules.
    It intentionally does not return an accepted candidate formula.  A hit can
    only guide the next experiment; it cannot enter the candidate cache or
    final RTL without normal full-support validation or CEC.
    """
    return session.query_recovery_experience(target, inputs, features, limit, format)


@mcp.tool()
def query_recovery_memory(target: str = "",
                          inputs: list[str] | None = None,
                          features: dict | None = None,
                          limit: int = 5,
                          format: str = "text") -> str:
    """Compatibility alias for ``query_recovery_experience``.

    Existing prompts may still call this name.  The implementation now queries
    graph experience and induced template families, not answer templates.  Hits
    suggest what experiment to run or what failed path to avoid; they never
    create accepted candidates.
    """
    return session.query_recovery_memory(target, inputs, features, limit, format)


@mcp.tool()
def promote_recovery_experience(run_id: str = "",
                                relation_node_ids: list[str] | None = None,
                                problem_node_ids: list[str] | None = None,
                                dry_run: bool = True,
                                format: str = "text") -> str:
    """Promote verified reasoning graph paths into ``data/recovery_kb``.

    Positive promotion requires a ``relation`` node linked to, or matching, an
    accepted ``validation`` node from full-support validation or CEC.  During
    promotion GateSpy induces a parameterized ``template_family`` from the
    verified relation, for example ``X_ext CMP (Y_ext +/- CONST)``; it does not
    promote the concrete answer formula as an accepted candidate.  ``expr_only`` evidence,
    unverified notes, ordinary tool output, and failed candidates are
    never positive experience.  Problem nodes may be promoted as negative
    experience only when explicitly selected or marked promotable.

    Default is ``dry_run=True`` so the agent can inspect exactly what would be
    learned before writing curated KB files.
    """
    return session.promote_recovery_experience(
        run_id=run_id or None,
        relation_node_ids=relation_node_ids,
        problem_node_ids=problem_node_ids,
        dry_run=dry_run,
        format=format)


@mcp.tool()
def promote_recovery_memory(run_id: str = "",
                            candidate_ids: list[str] | None = None,
                            failure_ids: list[str] | None = None,
                            dry_run: bool = True,
                            format: str = "text") -> str:
    """Compatibility alias for ``promote_recovery_experience``.

    ``candidate_ids`` are interpreted as relation graph node ids; ``failure_ids``
    are interpreted as problem graph node ids.  Promotion reads graph files, not
    raw event logs.
    """
    return session.promote_recovery_memory(
        run_id=run_id or None,
        candidate_ids=candidate_ids,
        failure_ids=failure_ids,
        dry_run=dry_run,
        format=format)


@mcp.tool()
def record_recovery_note(kind: str,
                         target: str = "",
                         summary: str = "",
                         refs: list[str] | None = None,
                         format: str = "text") -> str:
    """Compatibility helper for recording a small reasoning graph observation.

    Prefer ``record_recovery_graph_node`` when possible because it forces the
    node type.  This helper maps known graph node kinds directly and otherwise
    records an ``observation``.  It still cannot become accepted evidence or
    positive experience without a later verified ``relation -> validation`` path.
    """
    return session.record_recovery_note(kind, target, summary, refs, format)


@mcp.tool()
def list_candidates(format: str = "text") -> str:
    """List accepted global candidates, branch evidence, and failed candidates.

    Use this before assembling RTL or after several local validations.  Only
    ``C#`` accepted-global candidates are consumed by ``assemble_rtl``; ``B#``
    branch evidence must first be combined into a full expression.

    Important workflow rule: ``read_file`` resets the session cache.  After
    reloading a circuit, call this before referencing ``C#`` ids or recovered
    output names inside another expression.
    """
    return session.list_candidates(format)


@mcp.tool()
def probe_target(target: str,
                 inputs: list[str] | None = None,
                 pattern_num: int = 32,
                 seed: int | None = 0,
                 detail: bool = False) -> str:
    """Profile one target before attempting expression recovery.

    Reports target width, explicit or inferred support, backward-cone size,
    directed sample activity, scalar-control hints, and suggested next steps.
    This tool never generates candidate formulas and never writes to the
    accepted-candidate cache.

    Use probe results to design the next validation experiment, not to conclude
    semantics.  Sample activity and scalar-control hints are weak evidence until
    ``validate_expr``, ``fit_template`` under full fixed controls, or CEC proves
    the candidate.
    """
    return session.probe_target(target, inputs, pattern_num, seed, detail)


@mcp.tool()
def fit_template(target: str,
                 inputs: list[str] | None = None,
                 templates: str = "linear,product,comparator",
                 pattern_num: int = 512,
                 validation_num: int = 2048,
                 seed: int | None = 0,
                 exhaustive: str = "auto",
                 fixed_inputs: dict[str, int] | None = None,
                 detail: bool = False,
                 format: str = "text") -> str:
    """Fit only restricted templates for one target.

    This runs linear/product/comparator template fitting plus signed comparator,
    signed affine-difference comparator, bounded offset subtract, and bounded
    offset comparator variants such as ``X_ext - Y_ext +/- CONST`` and
    ``X_ext CMP (Y_ext +/- CONST)``.  The bounded offset comparator family is
    still only accepted after GateSpy bit-vector validation.  It does not run
    PySR, polynomial rewriting, or control enumeration.  Accepted global
    candidates are stored in the session with ids for later ``assemble_rtl``.

    For branch-local fitting, pass ``fixed_inputs={...}`` with every scalar
    control in that branch.  Candidate expressions are generated from ``inputs``
    while validation still samples the target cone's full non-fixed support.
    Branch results are stored as ``B#`` evidence only and must be combined with
    ``combine_case_expr`` before assembly.

    If the report says no candidate passed, inspect ``support_inputs``,
    ``template_inputs``, and any ``diagnostic=partial_control_assignment_*``
    note before changing methods.  A remaining scalar support input under
    ``fixed_inputs`` often means the branch was only partially fixed; rerun
    ``split_control_cases`` and try every full assignment.  This failure does
    not refute the whole template family until the full branch assignment has
    been tested.

    Before fitting, GateSpy may query local recovery experience for suggested
    experiments or negative stop rules.  Those RAG hits are reported as
    provenance notes only; the emitted candidate still comes from template
    fitting plus validation, not from trusting retrieval.
    """
    return session.fit_template(
        target=target,
        inputs=inputs,
        templates=templates,
        pattern_num=pattern_num,
        validation_num=validation_num,
        seed=seed,
        exhaustive=exhaustive,
        fixed_inputs=fixed_inputs,
        detail=detail,
        format=format,
    )


@mcp.tool()
def validate_expr(target: str,
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
    """Validate an explicit expression hypothesis.

    Use this when reasoning, simulation, or structure has already produced a
    formula such as ``out1 = in1 + in2 + in3``.  This is a verify-cost tool:
    it does not search for expressions.  If support is small enough and
    ``exhaustive="auto"``, validation is exhaustive; otherwise it uses directed
    training/validation samples.

    Default scope is ``validate_scope="support"``.  In this mode GateSpy samples
    every non-fixed primary input in the target cone, even if the expression or
    explicit ``inputs`` list omits it.  The report lists ``sample_scope``,
    ``support_inputs``, ``expression_inputs``, and ``omitted_support`` so a
    passing result cannot hide unsampled cone inputs.

    ``validate_scope="expr_only"`` is an explicit slice mode for debugging a
    narrowed hypothesis.  It samples only expression/fixed inputs; omitted
    target support is not driven and therefore defaults to 0 in circuit
    simulation.  Slice results are labelled ``slice-*`` and are hypothesis
    evidence only: they are not cached as ``C#``/``B#`` and cannot be consumed
    by ``assemble_rtl``.

    ``fixed_inputs`` validates a branch hypothesis under scalar/control cases,
    e.g. ``fixed_inputs={"sel": 1}``.  Branch-conditioned candidates are
    reported as ``B#`` evidence only when full-support validation passes; they
    are not cached for ``assemble_rtl`` until cases are combined into a full
    expression.

    Expression variables must be either primary inputs or previously accepted
    candidates from the session cache.  If ``inputs`` omits a primary input that
    appears in the expression, GateSpy adds it automatically.  Raw output or
    internal signal names are rejected unless they have already been accepted
    and cached as ``C#`` candidates; use ``list_candidates`` to check.  This
    prevents accidentally validating a formula by treating the original output
    as an input.

    Do not call a search tool after deriving a concrete formula.  First call
    this verifier; if it fails, summarize the mismatch and narrow the target or
    fixed control case before trying another method.  A failed validation
    refutes only the exact expression under the reported ``sample_scope`` and
    ``fixed_inputs``.  Check whether ``omitted_support`` is non-empty, whether
    slice evidence was used, or whether scalar controls remain variable before
    concluding the operator/template is wrong.
    """
    return session.validate_expr(
        target, expression, inputs, pattern_num, validation_num,
        seed, exhaustive, fixed_inputs, validate_scope, detail, format)


@mcp.tool()
def combine_case_expr(target: str,
                      controls: list[str],
                      cases: list[dict],
                      inputs: list[str] | None = None,
                      pattern_num: int = 512,
                      validation_num: int = 2048,
                      seed: int | None = 0,
                      exhaustive: str = "auto",
                      detail: bool = False,
                      format: str = "text") -> str:
    """Combine branch expressions into a full conditional expression.

    ``cases`` is a list of objects with ``fixed_inputs`` and ``expression``.
    Example:
    ``[{"fixed_inputs": {"s": 0}, "expression": "a"},
       {"fixed_inputs": {"s": 1}, "expression": "b"}]``.

    The combined ``?:`` expression is globally validated.  If accepted, it is
    cached as a normal ``C#`` candidate for ``assemble_rtl``.

    Use this only after each branch expression has been validated with
    ``validate_expr(..., fixed_inputs={...})``.  Branch-only ``B#`` evidence is
    not complete RTL and is not consumed by ``assemble_rtl``.
    """
    return session.combine_case_expr(
        target, controls, cases, inputs, pattern_num, validation_num,
        seed, exhaustive, detail, format)


@mcp.tool()
def infer_native_expr(target: str,
                      inputs: list[str] | None = None,
                      mode: str = "auto",
                      max_cost: int | None = None,
                      pattern_num: int = 512,
                      validation_num: int = 2048,
                      seed: int | None = 0,
                      detail: bool = False,
                      format: str = "text") -> str:
    """Infer one expression using only GateSpy native TABLE-I AST candidates.

    This is a medium-cost search tool: no PySR, no polynomial rewriting, and no
    control enumeration, but it still enumerates native candidates.  If a formula
    hypothesis already exists, call ``validate_expr`` instead; validation has no
    search space and will cache the accepted candidate for ``assemble_rtl``.

    Guardrail: small support does not always mean cheap native search.  If the
    candidate pool is large, GateSpy first filters with directed samples and
    exhaustively validates only survivors.  A report saying
    ``native exhaustive verification pruned`` means the old full candidate x
    exhaustive-pattern product would be too expensive; treat that as bounded
    tool behavior, not as proof that no formula exists.
    """
    return session.infer_native_expr(
        target, inputs, mode, max_cost, pattern_num, validation_num,
        seed, detail, format)


@mcp.tool()
def check_polynomial_lowbits(target: str,
                             inputs: list[str] | None = None,
                             max_low_bits: int = 8,
                             detail: bool = False,
                             format: str = "text") -> str:
    """Check bounded low-bit polynomial rewriting evidence for one target.

    The tool is deliberately capped.  If the support or cone is too large, it
    returns an explicit skip reason instead of expanding the search.
    """
    return session.check_polynomial_lowbits(
        target, inputs, max_low_bits, detail, format)


@mcp.tool()
def split_control_cases(target: str,
                        controls: list[str] | None = None,
                        inputs: list[str] | None = None,
                        pattern_num: int = 128,
                        seed: int | None = 0,
                        detail: bool = False) -> str:
    """Inspect scalar control cases for a target.

    This is a profiling tool, not a branch recovery tool.  It reports marginal
    per-control sample profiles and full control-combination profiles.  For
    multiple controls, samples are generated with balanced full assignments
    rather than relying on random coverage; low or missing coverage is reported.

    Critical interpretation rule: marginal rows such as ``c0=0`` vary all other
    controls freely and are only hints.  For two or more controls, do not infer
    branch semantics from marginal rows.  Validate branch hypotheses under full
    assignments, e.g. ``fixed_inputs={"c0": 0, "c1": 1}``, then use
    ``combine_case_expr``.

    If a marginal row appears to show an enable, dummy control, constant branch,
    or don't-care, treat that as an unverified hypothesis.  The next action is a
    full-assignment experiment or a small temporary script that enumerates the
    relevant rows, not a natural-language conclusion.

    This tool does not compose mux expressions and does not run candidate
    search.
    """
    return session.split_control_cases(
        target, controls, inputs, pattern_num, seed, detail)


@mcp.tool()
def influence_profile(target: str,
                      fixed_inputs: dict[str, int] | None = None,
                      inputs: list[str] | None = None,
                      base_inputs: dict[str, int] | None = None,
                      pattern_num: int = 2,
                      seed: int | None = 0,
                      detail: bool = False) -> str:
    """Perturb support bits/words under a fixed branch and report deltas.

    Use this after a CEX or suspicious branch formula to identify which support
    inputs still affect ``target`` under the selected ``fixed_inputs``.  The
    tool runs directed perturbation only: it does not infer expressions or cache
    candidates.

    ``inputs=None`` uses the full target cone support.  ``base_inputs`` can pin
    a concrete base pattern, otherwise directed base pattern(s) are generated;
    the default ``pattern_num=2`` checks both zero-like and nonzero/extreme
    bases so simple product/gated interactions are less likely to look like
    zero influence.  The report lists signed and unsigned output deltas plus
    zero-influence bits and words.  Treat zero influence as branch/base-specific
    evidence, not as a global proof that an input can be omitted.  If a branch
    may contain products, masks, or enables, rerun with multiple bases or
    explicit nonzero ``base_inputs`` before removing a support word.
    """
    return session.influence_profile(
        target, fixed_inputs, inputs, base_inputs, pattern_num, seed, detail)


@mcp.tool()
def infer_pysr_expr(target: str,
                    inputs: list[str] | None = None,
                    force: bool = False,
                    timeout_s: int = 30,
                    niterations: int = 50,
                    detail: bool = False,
                    format: str = "text") -> str:
    """Run PySR for one narrowed target only when ``force=True``.

    With the default ``force=False``, returns a guardrail explaining the cost and
    recommending probe/template/native tools first.  When forced, PySR candidates
    are still converted to GateSpy AST and validated before being cached.

    PySR is a heavy fallback, not a response to confusion.  Use it only after
    the target has been narrowed and explicit/template/native/control-case
    attempts have produced a clear failure summary.  Do not run PySR when the
    most recent failure still shows omitted support, partial fixed controls,
    slice-only evidence, or an unexamined CEX; first repair the experiment or
    write a temporary deterministic script to classify the failing patterns.
    """
    return session.infer_pysr_expr(
        target, inputs, force, timeout_s, niterations, detail, format)


@mcp.tool()
def assemble_rtl(candidates: list[str] | None = None,
                 timeout_s: int = 300,
                 detail: bool = False,
                 format: str = "text") -> str:
    """Assemble and optionally CEC-verify RTL from accepted local candidates.

    This tool consumes the session candidate cache populated by narrow recovery
    tools.  It does not attempt to recover missing outputs automatically; when
    candidates are missing, it returns the missing output list.

    Only ``C#`` accepted-global candidates are eligible.  ``B#`` branch evidence
    must first be combined with ``combine_case_expr``.  If outputs are missing,
    return to local recovery rather than invoking old broad recovery.

    Candidate RTL preserves the original module port order when the Verilog
    source provides one.  Before ABC CEC, GateSpy checks that the candidate and
    original top modules have the same port set and order; a mismatch is reported
    before running CEC because the resulting CEX would be unreliable.
    """
    return session.assemble_rtl(candidates, timeout_s, detail, format)


@mcp.tool()
def verify_rtl_candidate(candidate_code: str,
                         timeout_s: int = 300,
                         format: str = "text") -> str:
    """Verify complete candidate RTL text against the loaded Verilog source.

    This runs ABC CEC on a whole-module candidate.  It is a final/sanity
    verifier for a candidate assembled from local evidence, not a formula search
    loop.  If a CEX is returned, summarize the mismatching output, input word
    values, attempted formula, and likely activated control/cone path before
    trying another candidate.

    The verifier first checks top-module port names and order against the loaded
    source.  If the set or order differs, fix the candidate module header before
    interpreting any functional CEX.

    Stop rule: after two nearby whole-module candidates fail, do not keep
    mutating formulas here.  Go back to ``probe_target``, directed ``simulate``,
    ``split_control_cases``, ``validate_expr(fixed_inputs=...)``, or
    ``plan_recovery(feedback=...)``.
    """
    return session.verify_rtl_candidate(candidate_code, timeout_s, format)


@mcp.tool()
def analyze_rtl_cost(candidate_code: str = "",
                     path: str = "",
                     top_n: int = 10,
                     format: str = "text") -> str:
    """Analyze ICCAD cost hotspots in a complete RTL candidate.

    Use this after recovery has produced functionally equivalent RTL, or when
    inspecting an existing recovered RTL file.  It reports total cost,
    per-assignment and declaration-initializer hotspots, repeated RHS
    expressions, repeated sign/zero extension fragments, concat bit-patterns,
    output/predicate relation opportunities, signed-alias collapse
    opportunities, and rewrite-family hints.  It does not change code, does
    not run CEC, and does not produce accepted candidates.

    Provide either ``candidate_code`` or ``path``.  If both are empty, the
    current loaded Verilog/edit state is analyzed.
    """
    return session.analyze_rtl_cost(candidate_code, path, top_n, format)


@mcp.tool()
def propose_rtl_rewrites(candidate_code: str = "",
                         path: str = "",
                         strategies: str = "all",
                         max_candidates: int = 8,
                         format: str = "text") -> str:
    """Generate bounded cost-guided RTL rewrite candidates.

    This is an optimization-stage tool, not a semantic recovery tool.  It
    proposes local rewrite candidates from a CEC-proved or candidate RTL using
    families such as exact common-subexpression extraction, repeated
    sign/zero-extension hoisting, concat-to-affine, narrow modular-subtract, or
    conditional-subtract fitting, signed-alias collapse, output-relation
    predicate factoring, and mux-plus-common-add factoring.  The returned
    proposals are untrusted until ``optimize_rtl_round`` or
    ``verify_rtl_candidate`` proves them equivalent.

    Use ``format=json`` to retrieve candidate code for a selected rewrite.
    """
    return session.propose_rtl_rewrites(
        candidate_code, path, strategies, max_candidates, format)


@mcp.tool()
def optimize_rtl_round(candidate_code: str = "",
                       path: str = "",
                       strategies: str = "all",
                       max_candidates: int = 8,
                       timeout_s: int = 60,
                       accept_timeout: bool = True,
                       format: str = "text") -> str:
    """Run one cost-guided RTL optimization round.

    The tool generates a bounded batch of rewrite candidates, computes ICCAD
    cost for each one, and runs ABC CEC only on candidates whose cost is lower
    than the starting RTL.  Per-round ``timeout_s`` should normally be 60; the
    final chosen candidate should still be checked with
    ``verify_rtl_candidate(timeout_s=300)`` before reporting it as final.

    Accepted results are optimization candidates only.  Record failed rewrite
    families as negative experience when useful; do not use this tool to mutate
    formulas for unrecovered outputs.
    """
    return session.optimize_rtl_round(
        candidate_code, path, strategies, max_candidates,
        timeout_s, accept_timeout, format)


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
#  Code-modification workflow tools
# ---------------------------------------------------------------------------

@mcp.tool()
def edit(
    matches: list[str] | None = None,
    replacements: list[str] | None = None,
    rewrite: str = "",
    begin: str = "",
    end: str = "",
) -> str:
    """Apply string replacements to the current Verilog source code.

    This is an optimization/editing tool after recovery evidence exists.  It is
    not a hypothesis search tool.  Prefer ``validate_expr`` and
    ``assemble_rtl`` for local candidates; use ``edit`` only when you know which
    source region or full rewrite should be applied.

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

    After applying, Yosys CEC verifies functional equivalence between the
    old and new code.  If it fails (syntax error or functional mismatch) the
    edit is **rejected** — the current code is NOT changed and nothing is
    recorded.

    A rejected edit is feedback, not permission to brute-force whole modules.
    Summarize the CEX and return to local analysis after repeated nearby
    rejections.

    On success the ICCAD-2022-Problem-A cost is computed and the edit is
    recorded so it can be reverted later with ``revert``.

    Returns a structured report with cost, reduction rate, and status.
    """
    return session.edit(
        matches=matches, replacements=replacements,
        rewrite=rewrite, begin=begin, end=end,
    )


@mcp.tool()
def revert(depth: int = 1, id: int = 0, help: bool = False) -> str:
    """Revert previous edits or show the modification history.

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
    """
    return session.revert(depth=depth, id=id, help=help)


@mcp.tool()
def dump(path: str) -> str:
    """Write the current Verilog source code to *path*.

    Use this to save the latest (modified) code to a file after a series of
    successful edits.  Returns a confirmation with file size.
    """
    return session.dump(path)


@mcp.tool()
def show(detail: bool = False, grep: str = "") -> str:
    """Return the current Verilog source code with optional filtering.

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
    """
    return session.show(detail=detail, grep=grep)


if __name__ == "__main__":
    mcp.run()
