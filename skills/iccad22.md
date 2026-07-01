# ICCAD 2022 Problem A — Gate-Level to Word-Level Recovery

You are recovering word-level RTL from a flattened primitive-gate Verilog netlist. The goal is not just to make the circuit readable; the final RTL must be CEC-equivalent and must minimize the ICCAD22 contest cost.

## Agent Role

GateSpy is a hypothesis workbench, not a complete solver. Use tools to inspect structure, generate samples, fit expressions, check hypotheses, trace counterexamples, and measure cost. You remain responsible for strategy:

- invent new word-level hypotheses when built-in templates do not fit;
- decide when to group outputs, share intermediate wires, or rewrite expressions by hand;
- write small local analysis scripts when the MCP tools are too rigid;
- never treat random simulation, template fitting, or timeout as proof.

Only a strict CEC `proved` result is final proof. `counterexample`, `timeout_assumed`, and `error` are evidence to investigate.

## Prerequisites

If the user did not provide a Verilog file path, ask for it first. Do not proceed without one.

Use the repository setup first:

```bash
bash install.sh
```

The script installs Python dependencies, checks Julia/PySR and Yosys tooling, creates the Claude skill symlink, and prepares MCP configuration.

## Phase 1 — Load And Inspect

1. Load the primitive netlist:

   ```text
   read_file("<path/to/top_primitive.v>")
   ```

2. Inspect words and local structure:

   ```text
   get_node("out1")
   find_cone(["out1"], direction="backward", depth=3)
   print_adder_stats(detail=True)
   print_xor_stats(detail=True)
   simulate(pattern_num=16, seed=1)
   ```

Use these calls to identify support words, output grouping, adder/CSA trees, XOR chains, MUX arrays, comparators, bit slices, and repeated cones.

## Phase 2 — Choose A Recovery Lane

Before trying formulas, ask GateSpy to rank the available methods:

```text
propose_strategy(output="out3", detail=True)
```

The three lanes are tried from cheap to expensive:

1. `template`: structure-guided built-in templates plus LLM-defined basis/template fitting.
2. `polynomial`: bounded PO-to-PI structural rewrite. This can expose exact logic, but may explode; trust its budget estimate.
3. `symbolic`: sample-driven symbolic regression. Use it after template/polynomial miss, then refine with CEX.

You can run one lane explicitly:

```text
run_method(output="out3", method="template", sample_num=256)
run_method(output="out3", method="polynomial", budget={"max_expr_chars": 24000})
run_method(output="out3", method="symbolic", budget={"max_expr_size": 6})
```

These calls create hypotheses but do not edit source code. Treat their output as evidence for the next hypothesis.

Do not blindly launch expensive lanes on a large output. `polynomial` skips by default when the PO-to-PI rewrite estimate exceeds budget; only use `budget={"force": true, ...}` after you have a reason. `symbolic` does not immediately ask the LLM to guess: it first runs affine prefit, then a compact word-sketch search generated from the support words, including shifted affine forms, product-add-shift forms such as `(a * b + c) >> k`, and small product-of-sums forms. Only after these bounded WolFEx-style sketches fail does it try broader grammar search or return `budget_exceeded`. In that case, cut the cone or design smaller basis terms manually.

## Phase 3 — Generate Candidate Hypotheses

Start with the automatic candidate generator:

```text
infer_candidates(output="", sample_num=256, detail=True)
```

For many-output cases, `output=""` can return batch `assignments_json`. Feed that directly into:

```text
check_hypothesis(assignments=<assignments_json>, sample_num=512, run_cec=True)
```

Built-in candidates are only starting points. A `none fitted` result means the search space was too narrow, not that the circuit is unrecoverable. Move to `fit_basis`, polynomial rewrite, symbolic regression, or a temporary script.

## Phase 4 — Fit LLM-Defined Structure

When the built-in templates miss, create your own basis or template from structure and samples.

Use `fit_basis` when you can propose useful terms but do not know coefficients:

```text
fit_basis(
  output="out1",
  basis=[
    "in3 ? (in1 + in2 + in19 + in20) : (in5 + in6 + in21 + in22)",
    "in7", "in8", "in9", "in10",
    "in23", "in24", "in25"
  ],
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

Use `check_hypothesis` when you already have an expression:

```text
check_hypothesis({
  "out3": "in1 * (in2 + in3 + in4) + in5"
}, run_cec=True)
```

If CEC or samples fail, call:

```text
explain_failure(output="out3", depth=4)
trace_counterexample(output="out3", depth=4)
```

Then compare mismatch bits, word-level input values, candidate subterms, and the fan-in cone. Adjust the hypothesis around the contradiction instead of restarting blindly.

## Phase 5 — Mandatory Cost Reduction Pass

Correctness is not enough. After a hypothesis passes samples or CEC, do not dump immediately. Run a cost-reduction pass and ask whether the same function can be written with fewer contest operators. Only finalize after this pass has been attempted.

Use `check_hypothesis(..., share_common=True)` unless there is a reason not to. It extracts conservative repeated additive subexpressions into `gs_cse*` wires and reruns sample checks before CEC.

Still inspect the result manually. Common cost mistakes:

- duplicated bases such as `x + y + k * z` repeated across many outputs;
- explicit zero-extension or concat that does not change semantics but increases cost;
- expanded MUX arms that can share a base wire;
- recomputing an output expression instead of reusing the output or an intermediate wire;
- using `case` when nested `?:` is cheaper.
- performing the expensive operator inside every branch instead of selecting operands first.

Use this reasoning loop:

1. Count expensive operators in the proven expression (`*`, large condition trees, shifts, compares, bit-selects).
2. Ask whether the same expensive operator is repeated across branches or outputs.
3. If so, try moving selectors to operands instead of results, or extracting a shared intermediate.
4. Estimate cost before and after using the contest rules.
5. Validate the cheaper form with `check_hypothesis` before accepting it.

Common abstract rewrites to consider:

- `sel ? a * b : a * c` → `a * (sel ? b : c)`
- `sel ? a + b : a + c` → `a + (sel ? b : c)`
- multi-branch choices among related products → choose the operands first, then apply one product;
- repeated branch suffix/prefix terms → extract a shared wire;
- repeated shifted/scaled bases → compute the base once and share it.

If the simplification is not obvious, write a temporary script to enumerate algebraic variants, measure them with `scripts/cost.py`, and sample-check the best few with `check_hypothesis(run_cec=false)`. Keep this script in `/tmp` unless it becomes a reusable project tool.

If the tool expands an expression and raises cost, simplify it yourself or write a temporary optimizer script, then re-run `check_hypothesis`. If a lower-cost variant passes samples, rerun CEC or `edit` on that variant before dumping.

## Fallback Scripting Rules

You may write local scripts during recovery when the existing MCP tools are insufficient. Use this for tasks like grouping outputs, mining repeated support sets, enumerating candidate basis terms, simplifying generated assignments, or comparing many sample traces.

Rules:

- Put one-off scripts in `/tmp` or a clearly named temporary path.
- Make scripts deterministic: fixed input paths, fixed seeds, concise printed output.
- Do not overwrite source or recovered RTL from an exploratory script.
- Keep script outputs small enough to inspect.
- If a script becomes generally useful, propose moving it into `scripts/` with tests.
- Delete or ignore scratch scripts after use; do not leave unmanaged project clutter.
- A script result is never proof. Validate the final formula with `check_hypothesis` or `edit` CEC.

## Phase 6 — Apply, Revert, And Dump

Use `edit` only when a rewrite is ready to modify the current source:

```text
edit(rewrite="<complete RTL>")
```

The backend runs CEC. Rejected edits are not applied. If a path gets worse:

```text
revert(help=True)
revert(depth=1)
```

Review final code before dumping:

```text
show(detail=True)
dump("<path/to/top_recovered.v>")
```

## Scoring Reference

Reduction rate must be at least 70% to score:

```text
reduction_rate = (1 - cost / gate_count) * 100%
```

Operator costs are intentionally simple: `+`, `-`, `*`, shift, compare, `?:`, bit/part select, concat, reduction, and most logical operators cost 1 per operator use or bit as defined by the contest script. Declarations such as `wire`, `assign`, `input`, and `output` cost 0.

Use `scripts/cost.py` or the cost shown by GateSpy tools to compare alternatives.

## Practical Hints

- Prefer word-level semantics: one `+` or `*` can replace hundreds of gates.
- For large linear sums, use `fit_basis` or the large affine path in `fit_hypothesis`; do not manually guess dozens of weights.
- For MUX arrays, identify the select signal and shared true/false bases separately.
- For bit slices, be explicit about output width, truncation, and unsigned versus signed interpretation.
- Prefer declaring signed intermediate wires over `$signed(...)` when signed comparison is required.
- Use CEX as a debugging target: the wrong bit and input valuation usually point to the missing term, wrong selector, or width error.
- Do not stop at the first CEC-proved formula if the cost is obviously bloated; simplify and prove again.
- If `polynomial` reports budget overflow, do not force it globally; cut the cone, run it on fewer bits, or switch to LLM-designed basis terms.
- If `symbolic` returns nearest sample matches, use them to identify missing operators, then verify the revised formula with `check_hypothesis`.
