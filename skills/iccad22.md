---
name: iccad22-rtl-recovery
description: Uses GateSpy to recover low-cost word-level RTL from ICCAD22 Problem A style primitive-gate Verilog. Use when the task mentions gate-level netlists, top_primitive.v, top_recovered.v, CEC, contest cost, or reverse engineering arithmetic/comparator/MUX circuits.
---

# GateSpy Skill: Gate-Level to Word-Level Recovery

Recover word-level RTL from flattened primitive-gate Verilog. The final source
must be equivalent to the loaded design and low under the ICCAD22 cost model.

GateSpy is a hypothesis workbench. Tools inspect structure, sample behavior,
fit expressions, check hypotheses, trace failures, apply edits, and measure
cost. The LLM is responsible for grouping outputs, choosing methods, inventing
templates, recording evidence, and auditing cost.

## Core Invariants

1. Do not proceed without a concrete Verilog path.
2. Random simulation is evidence, not proof.
3. Only CEC `proved` is final proof. `counterexample`, `timeout_assumed`, and
   `error` are investigation states. If the user explicitly accepts timeout,
   label the result as timeout-assumed, not proved.
4. Always audit width, truncation, constants, and signedness for arithmetic,
   comparator, shift, and slice logic.
5. Recover output groups when outputs share support, structure, select signals,
   arithmetic trees, or CEX behavior. Recover scalar outputs only when isolated.
6. A proved RTL is not final until plausible lower-cost rewrites have been
   tried and rejected.
7. Never treat `run_method`, `infer_candidates`, `fit_*`, `simulate`, or
   `scripts/cost.py` as source modification or equivalence proof.

## Reference Files

Use these direct references when the task needs the detail:

- `references/iccad22-tools.md`: available GateSpy tools, arguments, returns,
  and state changes.
- `references/iccad22-scoring.md`: full contest operator/keyword cost model.
- `references/iccad22-patterns.md`: signedness, output grouping, CEX
  refinement, fallback scripting, and cost-audit patterns.

## Required Workflow

Keep this checklist live during recovery:

```text
- [ ] Verilog path confirmed
- [ ] Design loaded and port/gate summary recorded
- [ ] Outputs grouped before formula recovery
- [ ] Evidence record maintained for each output group
- [ ] Strategy lane chosen with reason
- [ ] Hypothesis sample-checked
- [ ] Width/signedness/boundary audit done
- [ ] CEX traced or CEC proved
- [ ] Cost audit decision recorded for each final output group
- [ ] Accepted RTL dumped, or unresolved state reported
```

## Phase 0 - Setup

For a fresh repository session, run setup before using GateSpy tools unless the
user explicitly says setup is already complete:

```text
bash install.sh
```

For ordinary code development, `uv sync --dev` is enough.

## Phase 1 - Load And Inspect

If no concrete Verilog path is provided, search the current repository for a
unique `top_primitive.v`. If exactly one candidate exists, use it and record
the path. If none or multiple candidates exist, ask the user to choose. Do not
recover from screenshots, guessed filenames, or unconfirmed files.

```text
read_file("<path/to/top_primitive.v>")
print_adder_stats(detail=True)
print_xor_stats(detail=True)
simulate(pattern_num=16, seed=1)
```

After `read_file`, first identify top module name, input/output ports and
widths, output naming pattern, gate count, and whether outputs are scalar bits
or packed words. Then use `get_node` and `find_cone` on representative
input/output ports; do not assume an output is named `out1`.

Record adder/CSA/CPA trees, XOR chains, MUX arrays, comparators, bit slices,
repeated cones, and unused inputs.

## Phase 2 - Group Outputs And Keep Evidence

Before recovering formulas, group outputs by shared support, adjacent bit index,
similar cone structure, common arithmetic tree, common select/control signals,
or common CEX behavior.

For each output group, maintain:

```text
- output group:
- bit range:
- support words:
- structural evidence:
- suspected operator family:
- signedness status:
- current best hypothesis:
- sample result:
- CEC result:
- known counterexamples:
- current cost:
- cheaper alternatives tried:
- final status:
```

Every failed hypothesis must record the smallest known contradiction: mismatch
bit, input valuation, and suspected cause. Classify failures as pairing,
signedness, width/truncation, selector, constant, missing term, or missing
shared intermediate before changing direction.

## Phase 3 - Select A Strategy Lane

Use strategy ranking as a hint, not an authority:

```text
propose_strategy(output="out3", detail=True)
```

Available lanes:

- `template`: built-in templates plus LLM-defined basis/template fitting.
- `polynomial`: bounded PO-to-PI structural rewrite; useful but can explode.
- `symbolic`: sample-driven symbolic regression and compact word sketches.

Run a lane only after the evidence record says why:

```text
run_method(output="out3", method="template", sample_num=256)
run_method(output="out3", method="polynomial", budget={"max_expr_chars": 24000})
run_method(output="out3", method="symbolic", budget={"max_expr_size": 6})
```

If `polynomial` is over budget, narrow the cone/output group or force it only
with a concrete reason. If `symbolic` exceeds budget, construct smaller basis
terms, split the cone, or write a targeted temporary script; do not fall back to
blind guessing.

## Phase 4 - Generate Hypotheses

Start with automatic candidates:

```text
infer_candidates(output="", sample_num=256, detail=True)
```

For many-output cases, pass emitted `assignments_json` directly to
`check_hypothesis` for whole-module validation. `none fitted` means the built-in
search was too narrow, not that the circuit is unrecoverable.

Use `fit_basis` when useful terms are known but coefficients are not:

```text
fit_basis(
  output="out1",
  basis=["in1", "in2", "in1 * in2", "sel ? in5 : 0", "in8 << 3"],
  sample_num=512
)
```

Use `fit_hypothesis` when the formula shape has unknown parameters:

```text
fit_hypothesis(
  output="out3",
  template="A * (b0 + b1*in2 + b2*in3 + b3*in4) + e0 + e1*in5",
  unknowns=["A", "b0", "b1", "b2", "b3", "e0", "e1"]
)
```

Use `check_hypothesis` for LLM-proposed assignments:

```text
check_hypothesis({
  "out3": "in1 * (in2 + in3 + in4) + in5"
}, run_cec=True)
```

`check_hypothesis` does not modify source. It sample-checks assignments,
computes cost, can share repeated common expressions, and can run CEC for
whole-module hypotheses. Use whole-module assignments when possible. Partial
assignments are useful for debugging samples, but final proof must cover all
outputs. Passing samples is only permission to try CEC or edit; it is not
strong enough to finalize a formula. The sample checker supports
`$signed(...)`, `$unsigned(...)`, and simple `wire signed [...] name = expr;`
local declarations, but complex Verilog signed propagation still requires
explicit width/signedness audit.

## Phase 5 - Boundary, Width, And Signedness Audit

Before accepting or rejecting arithmetic/comparator logic, test targeted
patterns: zero, one-hot, all-ones, max positive, sign bit only, values around
the sign bit, one active product/comparator operand at a time, selector values
for each MUX arm, and output overflow/truncation boundaries.

Signedness is an interpretation property, not necessarily visible in the gate
structure. Prefer explicit signed intermediates:

```verilog
wire signed [7:0] sa = a;
wire signed [7:0] sb = b;
assign out = sa * sb;
```

For low-bit outputs, make truncation explicit:

```verilog
wire signed [15:0] prod = sa * sb;
assign out = prod[15:0];
```

## Phase 6 - Refine With Counterexamples

When samples or CEC fail, do not restart blindly:

```text
explain_failure(output="out3", depth=4)
trace_counterexample(output="out3", depth=4)
```

Update the evidence record with mismatch bit/word, exact input valuation,
old/new values, candidate subterm values, related cone evidence, and suspected
cause. Use the contradiction to add a basis term, alter a selector, change
signedness/truncation, split an output group, or switch lane.

## Phase 7 - Apply Edit And Prove

Before `edit`, ensure the RTL is complete for the module. Prefer
complete-module `rewrite` for final transformations. Use `matches` and
`replacements` only for small local cleanups where the match string is unique
and unchanged outputs are explicitly preserved. For multi-output modules, do
not edit one output unless the tool explicitly preserves all other outputs. If
outputs share intermediate wires, validate them together.

```text
edit(rewrite="<complete RTL>")
```

Rejected edits are not applied. Timeout is rejected unless
`accept_timeout=True`; if timeout is accepted by explicit user instruction,
record final status as timeout-assumed, not proved. Use `revert(help=True)` and
`revert(depth=1)` if a path becomes worse.

## Phase 8 - Cost Reduction Audit

Correctness is not enough. Phase 8 is a local optimization loop over the
current proved or accepted RTL, not a restart of recovery. Keep the current
accepted RTL as the safety baseline: if a cheaper candidate fails samples or
CEC, reject/revert that candidate and continue from the baseline; if it proves,
make it the new baseline and audit again. Before `dump`, perform three passes:

1. Re-read recovered RTL as source. Look for repeated terms, removable
   extensions, avoidable slices/concats, cheaper operators, or factoring.
2. Re-check outputs together. Look for common bases, sibling outputs differing
   by constants/selectors, and shared intermediate wires.
3. Compare plausible cheaper rewrites with `scripts/cost.py`, then validate the
   cheaper form with `check_hypothesis` or `edit`.

For each final output group, record at least one cost-audit decision: either a
cheaper rewrite that was tried and failed, or a reason no cheaper rewrite is
plausible.

Use the full contest operator set. For example, `in1 ** 3` can be cheaper than
`in1 * in1 * in1`; shifts, reductions, compares, `%`, `case`, and `?:` may also
win. Do not finalize with only "equivalent"; state that no obvious lower-cost
equivalent form remains.

## Phase 9 - Dump Or Report Unsolved

Review current source:

```text
show(detail=True)
```

Dump only accepted recovered RTL:

```text
dump("<path/to/top_recovered.v>")
```

If unresolved, report the evidence ledger: current best hypothesis, exact
failure, known CEX, methods tried, and next bounded experiment.

## Fallback Scripting

Use local scripts when MCP tools are too rigid for grouping outputs, mining
support sets, enumerating basis terms, simplifying assignments, or comparing
many traces.

Rules: put one-off scripts in `/tmp` or a clearly named temporary path; use
fixed input paths and seeds; keep output small; do not overwrite source or
recovered RTL; do not treat script results as proof. If a script becomes
generally useful, propose moving it into `scripts/` with tests.
