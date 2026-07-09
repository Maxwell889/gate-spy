# ICCAD 2022 Problem A — Gate-Level to Word-Level Recovery

Given a flattened gate-level netlist (primitive gates only), recover auditable
word-level RTL semantics and, when needed, produce functionally-equivalent RTL.
The ICCAD scoring metric is **cost**: fewer operators and keywords means a
higher reduction rate.  A reduction rate below 70% scores zero.

You are an agent performing RTL recovery with evidence. Do not directly guess
RTL from netlist text. First establish word/support/control boundaries, then
generate candidate expressions, then verify them. Keep both structural evidence
and expression/verification evidence in the final reasoning.

Your primary job is to plan and revise the recovery workflow. Do not treat
GateSpy as a one-click RTL recovery script. Every recovery attempt should have a
current plan; every failure, timeout, or CEC counterexample should update that
plan before the next attempt.

## Verification-First Reasoning Discipline

Use natural-language reasoning to propose hypotheses, not to accept them. The
normal loop is:

```text
hypothesis -> smallest tool/script experiment -> structured evidence -> revise hypothesis
```

When a tool returns `No candidate passed validation`, a CEX, timeout, or a
surprising sample profile, do not conclude that the circuit is simply complex.
First ask which assumption failed:

- Did the expression omit support that still affects the target?
- Was validation `expr_only` or full-support?
- Did `fixed_inputs` leave scalar control/support bits variable?
- Did a marginal control profile get mistaken for a full branch truth table?
- Did a RAG hit or old note get treated as validated evidence?
- Did the tool use bit-vector width, signedness, sampling, or cache semantics
  differently from your mental model?
- Is the current target too wide, and can it be split by bit, branch, cone, or
  already accepted predicate?

If the MCP tools do not expose exactly the diagnostic you need, write a small
temporary script or pytest-style experiment rather than continuing prose
reasoning. Keep ad hoc scripts under `/private/tmp/gate-spy-*` unless they are
useful enough to become a committed test or tool. A good temporary experiment
enumerates a few fixed assignments, prints support/outputs in a table, or
compares a candidate expression against simulation rows. Promote the insight
back into MCP output, the recovery reasoning graph, a regression test, or local
experience KB only after it is reproducible.

Durable learning follows this fixed loop:

```text
failure -> small experiment -> observation -> relation -> full-support validation/CEC -> experience
```

Reusable templates should emerge from this loop. If a newly validated relation
looks reusable, record it as a relation with the trigger, suggested experiment,
and expression, then promote it. Promotion induces a parameterized
`template_family` for later RAG retrieval; it should teach future agents what
experiment to try, not hand them an accepted formula.

Ordinary tool logs are not knowledge and are not persisted. A useful recovery
trace records what problem was encountered, what experiment was run, what
relationship was inferred, and how it was validated. If a failure does not lead
to at least one `problem` graph node and one follow-up `experiment` graph node,
the reasoning round is incomplete.

## Prerequisites

If the user did **not** provide a Verilog file path, ask for it first. Do not proceed without one.

## Workflow

### Phase 1 — Load & Understand

1. `read_file <path>` — loads the netlist, builds circuit IR, saves source
   code for CEC when the input is Verilog, and by default starts a graph-only
   reasoning run under `.gate_spy/runs/<run_id>/`. The run directory stores
   `graph.json`, `summary.json`, `graph.jsonl` after the first node, and
   optional `final_candidate.v`; it does not store raw tool logs or full report
   text.
2. `plan_recovery(outputs=None, detail=True)` — create the initial recovery
   plan: output clusters, support cuts, suspected roles, recommended tools,
   and acceptance criteria. If curated local experience exists, plan hints may
   include suggested experiments or negative stop rules; treat them as advisory
   hypotheses only.
3. Optional: `query_recovery_experience(target, inputs, limit=5)` to retrieve
   prior promoted experience graph records. A hit suggests the next experiment
   or a failed path to avoid; it is never an accepted candidate and must still
   be validated with full-support `validate_expr` or CEC.
4. Optional structural probes when the plan says a cluster is ambiguous:
   - `get_node` on candidate buses or control bits.
   - `find_cone` backward from outputs to inspect boundary choices.
   - `print_adder_stats` to locate adder/compression trees.
   - `print_xor_stats` to locate XOR chains.
   - `simulate` with a few directed patterns to interpret control behavior.

### Phase 2 — Candidate Recovery

Execute the plan cluster-by-cluster. Before each attempt, state the current
mini-plan: target, hypothesis, tool, effort, expected evidence, and acceptance
condition. Use the cheapest sufficient method:

```text
probe -> verify explicit hypothesis -> light template/control -> medium bounded search -> heavy forced search
```

Use small tools, one hypothesis at a time:

1. `probe_target(target, inputs=None, detail=True)` to inspect support, cone
   size, sample activity, and scalar-control hints. This never emits formulas.
2. `validate_expr(target, expression, inputs=None, exhaustive="auto")` when
   probing, simulation, or manual reasoning has already produced a formula.
   This verifies and caches the candidate; it does not search. The default
   `validate_scope="support"` is full-support validation: after applying
   `fixed_inputs`, every other primary input in the target cone is sampled even
   if it is absent from the expression or `inputs`. Only full-support
   `sample-exact` / `exhaustive-exact` can become `C#` or `B#`.
   Use `validate_scope="expr_only"` only for narrow slice diagnostics; omitted
   support inputs default to 0 in simulation, and any `slice-*` result is
   hypothesis evidence only. It cannot be combined or assembled.
   For a branch hypothesis after control splitting, pass
   `fixed_inputs={"ctrl": value}`. Branch-conditioned full-support candidates
   are evidence only; combine cases before final `assemble_rtl`.
3. `fit_template(target, inputs=None, templates="linear,product,comparator")`
   for restricted arithmetic/comparator hypotheses, including bounded offset
   arithmetic/comparators (`X_ext - Y_ext +/- CONST`,
   `X_ext CMP (Y_ext +/- CONST)`), signed word comparators, and signed
   affine-difference comparators such as
   `($signed({a[msb], a}) - $signed({b[msb], b})) <
   ($signed({b[msb], b}) - $signed({c[msb], c}))`. For a branch-local template
   attempt, pass `fixed_inputs` for the full control assignment; the tool
   generates expressions from `inputs` but validates the target cone's full
   non-fixed support. Branch template hits are `B#` evidence only. It may
   surface local experience hits as provenance notes, but emitted candidates
   still come from template fitting plus validation, not from trusting retrieval.
4. `infer_native_expr(target, inputs=None, mode="auto")` for native TABLE-I AST
   candidates only. This is a medium-cost enumeration tool; do not use it when
   `validate_expr` can verify an explicit hypothesis. Small support does not
   guarantee a cheap search if the native candidate pool is large; the tool may
   report `native exhaustive verification pruned`, meaning it used directed
   samples to filter candidates before exhaustive survivor validation. If that
   returns no candidate, move to hypothesis-driven scripts or target
   decomposition rather than rerunning the same broad native search.
5. `check_polynomial_lowbits(target, inputs=None, max_low_bits=8)` for bounded
   low-bit polynomial evidence. Respect skip reasons; do not raise caps blindly.
6. `split_control_cases(target, controls=None, inputs=None, detail=True)` for
   scalar control or mux-like behavior. Recover branches after understanding
   the cases. It emits balanced full-control assignment coverage for selected
   controls. Treat marginal rows as hints only; branch hypotheses must be
   validated under full control assignments such as
   `fixed_inputs={"c0": 0, "c1": 1}`.
7. `infer_pysr_expr(target, inputs=None, force=True)` only as a heavy fallback
   after template/native attempts fail on a narrowed target.
8. `assemble_rtl(candidates=None, timeout_s=300)` only after accepted local
   candidates exist. It consumes cached candidate ids and does not recover
   missing outputs automatically.
9. `list_candidates()` whenever you need to know which global candidates,
   branch-only evidence, or failures are currently known.
10. `combine_case_expr(target, controls, cases, inputs=None)` after branch
   expressions are validated. It builds a full `?:` expression, validates it
   globally, and caches it if accepted.
11. `influence_profile(target, fixed_inputs, inputs=None, base_inputs=None)`
   after a CEX or suspicious branch formula. It perturbs support bits/words
   under the fixed branch and reports signed/unsigned deltas plus zero-influence
   inputs. Use it to decide which omitted inputs must return to full-support
   validation; it does not generate or cache formulas. Zero influence is only
   evidence for the tested base pattern(s): product, mask, and enable terms can
   disappear at an all-zero base. For branch formulas with possible
   interactions, use at least the default multi-base profile or pass explicit
   nonzero `base_inputs` before removing a support word.
12. `verify_rtl_candidate(candidate_code, timeout_s=300)` when you manually
   assemble or edit a candidate RTL module.
13. `analyze_rtl_cost(candidate_code="", path="", top_n=10)` after a full RTL
   candidate is equivalent or nearly ready. It reports cost hotspots,
   duplicate RHS expressions, declaration-initializer costs, repeated sign/zero
   extensions, concat bit-patterns, output/predicate relation opportunities,
   and rewrite family hints. It does not run CEC and does not accept
   candidates.
14. `propose_rtl_rewrites(candidate_code="", path="", strategies="all")` to
   generate bounded local optimization candidates such as exact CSE, extension
   hoisting, concat-to-affine / narrow modular-subtract /
   conditional-subtract fitting, signed-alias collapse, output-relation
   predicate factoring, and mux-plus-common-add factoring. These candidates are
   untrusted until CEC.
15. `optimize_rtl_round(candidate_code="", path="", timeout_s=60)` for one
   cost-guided rewrite round. It computes cost for each proposed candidate and
   runs short CEC only on lower-cost candidates. Use `format=json` to retrieve
   best candidate code, then run final `verify_rtl_candidate(timeout_s=300)`.
16. `record_recovery_graph_node(node_type, target, summary, data=None,
   links=None, promotable=False)` whenever a failure, experiment, observation,
   inferred relation, or validation result should be durable. Use node types
   `problem`, `experiment`, `observation`, `relation`, `validation`, and
   `experience`. Store compact word-level facts only; do not store raw tool
   text, full transcripts, or bit-level patterns. `record_recovery_note(...)`
   remains a compatibility helper for small observations, but the graph-node
   tool is preferred because it forces the reasoning role.
17. `query_recovery_experience(...)` retrieves promoted experience graph records:
   triggers, suggested small experiments, induced `template_family` records,
   relation families, validation source, and negative stop rules. It never
   returns an accepted candidate or a case answer. Treat hits as prompts for
   the next small experiment, such as "scan offset comparator constants", then
   validate your derived hypothesis normally.
18. `export_recovery_ir()` to inspect the graph-only snapshot when debugging
   failure or preparing a reproducible report. Despite the compatibility name,
   this is not a raw tool trace.
19. `promote_recovery_experience(run_id=None, dry_run=True)` only after reviewing
   the graph. Positive promotion requires a verified `relation -> validation`
   chain. During promotion GateSpy induces a parameterized template family from
   the verified relation (for example `X_ext CMP (Y_ext +/- CONST)`), not a
   reusable answer formula. `expr_only`, ordinary tool output, failed
   candidates, and unverified observations cannot become positive experience;
   selected/promotable problems may become negative experience.

Do not use `verify_rtl_candidate` or `edit` as a formula brute-force loop. If
two nearby whole-module candidates produce CEXs, stop, summarize the shared
failure pattern, and return to `probe_target`, `simulate` with directed fixed
inputs, or `plan_recovery(feedback=...)`.

If you are unsure about tool cost, call `recovery_tool_guide()` or read the
`effort/search/risk` lines in `plan_recovery(detail=True)`.

`infer_expression`, `recover_expression`, and `recover_rtl` are not exposed as
MCP recovery tools. Do not use broad search or full-module baseline as an
escape hatch when a target looks complex. Complex means split controls, fix
branch inputs, validate smaller hypotheses, combine cases, and only then
assemble.

The old broad recovery functions may still exist internally for library tests
or developer baselines, but they are not part of the agent workflow and should
not appear in an MCP recovery trace.

The small-tool flow separates:

- word/support analysis from backward cones,
- probe-only target profiling,
- explicit hypothesis validation with no search,
- restricted linear/product/comparator fitting,
- native TABLE-I AST candidate generation,
- bounded polynomial-rewriting evidence over low bits,
- scalar-control case splitting,
- explicit forced PySR fallback,
- candidate-cache assembly and, for Verilog sources, ABC CEC.

## Building Big Discoveries From Small Clues

The agent's job is not to find one magic formula. It should accumulate local
facts and reuse them:

1. Confirm simple predicates first, such as signed comparisons or equality
   tests, using `validate_expr`.
2. Call `list_candidates` before moving to a harder target. Treat `C#`
   candidates and recovered output names as reusable semantic building blocks.
3. When a larger output cone uses an already recovered output or predicate,
   write hypotheses using that name, e.g. `out1 = out4 ? expr_a : expr_b`.
   `validate_expr` can expand accepted candidates by target name or `C#` id.
4. For control-heavy targets, run `split_control_cases`, validate each branch
   with `fixed_inputs`, then call `combine_case_expr`. For two or more scalar
   controls, `fixed_inputs` must include the full assignment for every listed
   control. If a branch-local report still shows scalar support in
   `template_inputs` or a `partial_control_assignment` diagnostic, the branch is
   not fully fixed yet.
5. Only globally accepted combined expressions should feed `assemble_rtl`.
   Branch-only `B#` evidence is not a complete RTL candidate.
6. A `read_file` reload resets the session candidate cache. Re-validate known
   predicates or check `list_candidates` before referencing `C#` ids or output
   names in later expressions.
7. Keep the durable trace in the reasoning graph. If reasoning exists only in
   chat context, convert it into a compact `problem`, `experiment`,
   `observation`, `relation`, or `validation` node, or reproduce it with a tool;
   do not let implicit context become accepted evidence.
8. Use local recovery experience as a curriculum, not an oracle. Positive hits
   suggest experiments and relation families to validate; negative hits suggest
   stop rules or likely wrong abstractions.

Trust levels:

- `cec-proved`: strongest result; candidate RTL is formally equivalent.
- `cec-timeout-assumed`: no counterexample found before timeout; acceptable in
  this project flow but not a strict proof.
- `exhaustive-exact`: exact for the enumerated support bits.
- `sample-exact` / `sample-masked`: strong evidence only when
  `sample_scope=full-support`; not proof.
- `slice-*`: expression-only or narrowed-scope evidence. It can suggest a
  hypothesis, but it is not a candidate for combination or assembly.
- RAG / experience hits and unverified graph observations: advisory context
  only. They never enter the candidate cache and cannot be promoted as positive
  experience without a later full-support validation or CEC.
- `failed` or failed candidates: debugging clues only.

### Phase 2b — Feedback and Plan Revision

When a recovery step fails, times out, or produces a CEC counterexample, stop
and summarize the failure before trying another recovery command:

- target
- attempted method/tool
- observed failure, timeout, or CEX
- falsified assumption or likely cause
- revised next step

Then call `plan_recovery(feedback=...)` or explicitly revise the mini-plan. Use
these rules:

- Hypothesis audit first: inspect `sample_scope`, `support_inputs`,
  `expression_inputs`, `omitted_support`, `template_inputs`, `fixed_inputs`,
  validation status, and any diagnostic notes. Do this before changing method or
  escalating search.
- Tool semantics audit: if a tool result contradicts a strong expectation, stop
  and inspect the tool contract before speculating. Read the MCP docstring, the
  relevant implementation, or a focused test when needed. For example, for
  `validate_expr`, check expression width, target width, signedness, expression
  identifiers, fixed inputs, and whether a failure is exact, masked, slice-only,
  training, or validation. Produce one concrete feedback item:
  `model hypothesis wrong`, `tool used different semantics than expected`,
  `tool report should expose a clearer diagnostic`, or `tool bug suspected with
  minimal repro`.
- Influence-profile audit: if a zero-influence conclusion is surprising, rerun
  it with multiple base patterns or a CEX-derived `base_inputs`. A single zero
  base can hide cross terms such as `a * b`, because flipping `a` has no effect
  while `b=0`.
- Minimal repro before blame: if tool behavior still looks wrong after reading
  the contract, create a small `/private/tmp/gate-spy-*` script or a focused
  pytest-style snippet that reproduces the mismatch with explicit inputs,
  expression, expected value, actual value, and tool output. Do not continue a
  long natural-language argument about the tool.
- CEX mismatch: prioritize the CEX-activated cone/control path; add the input
  word values and mismatching output to the feedback.
- Repeated CEXs from similar whole-module candidates: stop trying formula
  variants, make a small table of the distinguishing input patterns, and probe
  the local cone or control combinations that explain those rows.
- No candidate passed validation: shrink the target to a bit/part-select,
  make inputs explicit, and inspect the local cone before changing methods.
- `sample-exact` but CEC failed: demote the expression to a hypothesis and add
  the CEX pattern to the next directed simulation/recovery attempt.
- `slice-*` evidence: never combine or assemble it. Re-run the hypothesis with
  `validate_scope="support"` or use `influence_profile` to explain which omitted
  support inputs still affect the target.
- Timeout: split the cluster or reduce target width; do not interpret timeout
  as successful understanding.
- Control failure: identify scalar controls, enumerate cases, and recover each
  branch separately. With two or more controls, enumerate full control
  combinations; do not infer branch semantics from one-control marginal
  profiles.
- Missing diagnostic tool: write a small deterministic local script to classify
  the patterns or check a candidate, then turn the result into a tool call,
  regression test, reasoning graph node, or MCP/report improvement. Do not let
  the conclusion live only in chat context.

After every failure:

1. Record a `problem` node with the falsified hypothesis, target, CEX/timeout
   summary, or template miss.
2. Run the cheapest concrete experiment that can distinguish the next
   hypothesis: directed `simulate`, `influence_profile`, full-control branch
   `validate_expr`, or a temporary `/private/tmp/gate-spy-*` script.
3. Record the experiment and compact observation. Keep only word-level sample
   values or decoded CEX fields.
4. If the observation suggests a reusable relationship, record a `relation`
   node, then validate it with full-support `validate_expr` or CEC.
5. Only after validation should the relation be promoted into local experience.

Do not respond to template/native/PySR failure with a long natural-language
essay. The next step is an experiment that can falsify or refine the current
assumption.

### Phase 3 — Multi-Round Cost Optimization

After you have a functionally equivalent candidate RTL, do not stop at the
first CEC-proved form. Enter a cost-optimization loop. The goal is to lower the
ICCAD cost while preserving CEC equivalence.

You may use visual/manual RTL inspection, algebraic reasoning, pattern
matching, temporary scripts, and focused tool calls. Natural-language reasoning
can propose an optimization, but every accepted optimization must be backed by:

```text
candidate RTL -> scripts/cost.py -> CEC -> best-cost ledger update
```

Maintain an optimization ledger in your working notes:

- `best_code`: current lowest-cost equivalent RTL or edit state.
- `best_cost`: cost from `scripts/cost.py` or the edit report.
- `best_cec`: CEC status for `best_code`.
- `round_cec_timeout_s`: normally `60` for optimization-round candidates.
- `final_cec_timeout_s`: normally `300` for the final selected candidate.
- `round`: current optimization round number.
- `no_improve_rounds`: consecutive rounds that failed to lower `best_cost`.
- `attempts`: short table of tried transformations, cost, CEC status, and
  reason accepted/rejected.

Initialize the ledger from `assemble_rtl(detail=True)`, an existing recovered
RTL file, or the current edited source. If the candidate is a standalone RTL
file, run:

```bash
python scripts/cost.py <candidate.v> --debug
python scripts/yosys_cec.py <original.v> <candidate.v> --timeout 60
```

Prefer the MCP optimization tools over unaided guessing:

```text
analyze_rtl_cost -> propose_rtl_rewrites or optimize_rtl_round(timeout_s=60)
-> inspect accepted lower-cost candidates -> final verify_rtl_candidate(timeout_s=300)
```

`analyze_rtl_cost` identifies the current cost drivers. `optimize_rtl_round`
is the default one-round optimizer because it combines bounded rewrite
generation, cost measurement, and short CEC. Use manual temporary scripts only
when the tool output shows a rewrite family worth exploring but not yet
implemented. If a manual rewrite succeeds repeatedly, promote it as an
optimization experience and consider adding it to the optimizer.

When using a generated or temporary candidate, write it under
`/private/tmp/gate-spy-opt-*/` and keep only the final useful artifact in the
repo. If `yosys_cec.py` is unavailable or inconclusive, use `abc_cec.py` or the
project's verified CEC path and record the limitation.

Use short CEC for optimization rounds. At this stage the starting RTL has
normally already passed full recovery validation, so most candidates are local
rewrites of an equivalent RTL. Do not spend 300s on every round candidate:
use `--timeout 60` or `verify_rtl_candidate(timeout_s=60)` for per-candidate
screening. Record `cec-proved`, `cec-timeout-assumed`, or failed/CEX status in
the ledger. A lower-cost candidate that times out without a CEX at 60s may be
kept as provisional best for further local optimization, but it is not a final
result until the final selected candidate is checked with the long timeout.

Each optimization round should do all of the following:

1. Inspect the current best RTL and cost drivers. Start with
   `analyze_rtl_cost`; use `scripts/cost.py --debug`, direct reading, and small
   scripts only when the MCP analysis is insufficient. It is acceptable to use
   "stare at the RTL" reasoning, but convert every promising idea into a
   reproducible candidate file or tool call.
2. Propose one to three concrete transformations. Prefer local transformations
   that preserve the already proved semantics:
   - cross-output common-subexpression extraction,
   - replacing repeated `$signed({x[msb], x})` with signed declarations or
     signed intermediate wires,
   - factoring branch truth tables into nested `?:` controlled by bare control
     bits instead of `==`/`&&` case guards,
   - replacing a 4-way case tree with shared predicates plus a low-cost mux,
   - sharing affine differences, comparators, masks, shifts, or threshold
     predicates across outputs,
   - choosing `case` versus nested `?:` based on the cost table, not style,
   - pushing sign extension, bit selection, or negation into an already needed
     arithmetic expression when CEC confirms it,
   - using explicit narrow intermediate wires to preserve modular wrap before
     zero/sign extension when a concat bit pattern encodes a small arithmetic
     table,
   - rewriting output families together rather than optimizing each output in
     isolation.
3. Prefer `optimize_rtl_round(timeout_s=60)` for the candidate batch. For each
   manual candidate, compute cost and run the round CEC check with
   `round_cec_timeout_s=60` before accepting it for the loop.
4. Accept the candidate for the optimization loop only if the round CEC is
   `cec-proved` or `cec-timeout-assumed` and cost is lower than `best_cost`.
   Mark 60s timeout-based accepts as provisional. Update `best_code`,
   `best_cost`, `best_cec`, reset `no_improve_rounds=0`, and start another
   round from the improved RTL.
5. If no tried candidate in the round lowers cost, increment
   `no_improve_rounds` by 1. Use the failed attempts as negative evidence for
   the next round.

Stop the optimization loop only when one of these is true:

- `no_improve_rounds >= 3` after three consecutive non-improving rounds.
- A strict user budget or timeout is reached.
- CEC tooling is unavailable and no trustworthy equivalence check can be run.

Do not count an untested idea as a round. A round must include at least one
concrete candidate with cost and CEC results, or a small script that proves no
candidate in a clearly defined local rewrite family improves cost.

If an optimization candidate fails CEC, do not discard the information. Record
the distinguishing pattern or CEX, identify which algebraic assumption was
wrong, and either repair that candidate or mark the rewrite family as negative
evidence. Failed CEC candidates do not update `best_code`.

When the best candidate improves the assembled RTL in a reusable way, consider
turning the transformation into a regression test, a local experience-graph
record, or a future MCP post-processing pass. For example, if branch recovery
produces a correct but expensive 4-way `?:`, the optimization phase should try
shared predicate extraction and control-mux normalization before stopping.

When a tool-semantics audit finds that the model misunderstood a tool, update
the working notes with the corrected rule and apply it to the next candidate.
When it finds that the tool output is too easy to misread, propose a concrete
MCP/report improvement, such as an added diagnostic note, width warning, CEX
field, or negative experience entry.

### Phase 4 — Optional Source Editing

Only after candidate expressions are supported by recovery evidence, or after
the multi-round optimizer has produced a lower-cost equivalent RTL, edit the
source code with `edit(matches, replacements)`:

- Each `match` string must appear **exactly once** in the current code.
- Use `\n` for newlines in match/replacement strings.
- Replacing gate-level structures with word-level operators (`+`, `*`, `-`, `?:`, `{}`, etc.) reduces cost.
- The system runs CEC automatically and a counter-example is provided if the edited code is not equivalent.
- Rejected edits are not applied in the backend.
- Track progress: each successful edit reports cost change and reduction rate.

**Optimization heuristics** (ordered by impact):
1. Adder trees → `+` operator (biggest cost reduction).
2. Multiplier structures → `*` operator.
3. MUX / conditional logic → `?:` or `case`.
4. Bit-level logic chains → bit-wise `&` `|` `^` or reduction operators.
5. Shift registers → `<<` `>>`.
6. Repeated sub-circuits → extract as submodules with `module`.

**Helper tools during editing:**
- `show` — view current code (abridged by default; `detail=true` for full; `grep=<regex>` to search).
- `show(grep="...")` — find patterns in current code to build match strings.
- `revert(help=true)` — see modification history with cost deltas.
- `revert(depth=1)` — undo last edit; `revert(id=N)` — jump to a specific state.

### Phase 5 — Finalize

When no further optimizations seem possible:
1. Review the plan and optimization trace: successful recovery steps, failed
   steps, accepted/rejected optimization attempts, best cost, CEC status, and
   remaining unrecovered or unoptimized regions.
2. Confirm the stop rule: report `no_improve_rounds`, the last three
   non-improving rounds, or the explicit budget/tooling reason for stopping.
3. Run the final long CEC check on `best_code` with
   `final_cec_timeout_s=300`, for example
   `python scripts/yosys_cec.py <original.v> <best_candidate.v> --timeout 300`
   or `verify_rtl_candidate(timeout_s=300)`. If the final check fails, roll
   back to the previous best candidate with a valid long check or resume
   optimization from the last non-failing candidate.
4. `assemble_rtl(detail=True)`, `show(detail=true)`, or inspect the final
   candidate file — review final evidence or final edited code. Do not switch
   to old full-module recovery at finalize; missing candidates mean the plan
   still needs local recovery.
5. `dump <output_path>` — write edited source only if you used `edit`; otherwise
   keep or write the final verified candidate RTL requested by the user.

**Recommended output path**: use the input filename with `_recovered` suffix, e.g. `input_recovered.v`.

## Scoring Reference

Reduction rate must be ≥ 70% to score any points:

$$\text{reduction rate} = \left(1 - \frac{\text{cost}}{\text{gate count}}\right) \times 100\%$$

### TABLE I — Operator Costs

| Operator | Cost | Operator | Cost |
|---|---|---|---|
| `{}` `{{}}` concat/replication | 1 / symbol | `&` `\|` `^` `^~` `~^` bit-wise | 1 / bit |
| `[]` `[:]` bit/part-select | 1 | `&` `\|` `^` `~&` `~\|` `~^` reduction | 1 |
| `+` `-` `*` `/` `**` | 1 | `<<` `>>` `<<<` `>>>` | 1 |
| `%` | 1 | `?:` conditional | 1 |
| `>` `>=` `<` `<=` | 1 | `!` logical not | 1 |
| `==` `!=` `===` `!==` | 1 | `&&` `\|\|` | 1 |
| `~` bit-wise not | 1 / bit | | |

### TABLE II — Keyword Costs

| Keyword | Cost | Keyword | Cost |
|---|---|---|---|
| `if` | 1 | `case`/`casex`/`casez` | 2 / item (default excluded) |
| primitive gate instance | 1 | submodule instance | module cost of submodule |
| `signed` `unsigned` `wire` `reg` `assign` `always` `begin` `end` `else` `input` `output` `parameter` `module` `endmodule` | 0 | | |

## Hints

- **Favor word-level semantics.**  A single `+` or `*` replaces dozens of gate instantiations
  and drops cost dramatically.  Always prefer the highest-level abstraction the circuit supports.
- **Do not skip support discovery.**  For large circuits, first restrict each
  target to its support words; unconstrained symbolic regression does not scale.
- **Use control decomposition.**  If a target has scalar control inputs or mux
  behavior, run `split_control_cases` and recover branch behavior before
  accepting a complex formula. For multiple controls, use full fixed-input
  assignments for each branch; marginal profiles are only sampling hints.
- **Keep evidence paired.**  A final conclusion should mention both cone/support
  evidence and expression/CEC evidence.
- **Reuse intermediate signals.**  When a computed output appears as a sub-expression elsewhere, reference the output wire directly instead of recomputing — each reused signal saves operators and reduces cost.
- **Revert if stuck.**  `revert(help=true)` shows history; roll back bad edits and try another path.
- **Optimize until the stop rule.**  Before dumping, run the multi-round
  optimization loop. Do not stop after the first equivalent RTL. Stop only after
  three consecutive non-improving rounds, a user budget/tooling limit, or
  unavailable equivalence checking. External comparison baselines are not stop
  conditions and must not cap the search.
- **Trust CEC over manual simulation.**  Once you have a rough hypothesis, `edit` it directly rather than exhaustively simulating corner cases.  Rejected edits return precise counterexamples that pinpoint the flaw — iterate on those instead of guessing patterns from random input vectors.
- **Declare signed wires instead of using `$signed()`.**  When a comparison needs signed semantics, declare intermediate wires as `wire signed [W-1:0]` and compare them directly.  Verilog then uses implicit signed comparison, and the cost function counts only the real arithmetic operators.
