# ICCAD22 Recovery Patterns

## Output Grouping

Group outputs before formulas. Use shared support, adjacent bit index, similar
cone structure, common arithmetic tree, common select/control signals, and
common CEX behavior. Word-level recovery usually gets worse when arithmetic
outputs are split too early into independent scalar bits.

## Signedness And Width

Signedness is an interpretation property, not necessarily a gate-level
structure. Audit:

- operand declared width;
- result width;
- truncation or extension point;
- whether constants are sized;
- whether part-select removes signedness.

Prefer explicit signed intermediate wires:

```verilog
wire signed [7:0] sa = a;
wire signed [7:0] sb = b;
assign out = sa * sb;
```

If only low bits are needed, make truncation explicit and prove it:

```verilog
wire signed [15:0] prod = sa * sb;
assign out = prod[15:0];
```

## Counterexample Discipline

Do not move from one guessed formula to another just because a random sample
mismatched. Every failed hypothesis must record:

- mismatch bit or output word;
- input valuation;
- old/new values;
- candidate subterm values;
- suspected cause;
- next bounded experiment.

Use `trace_counterexample` to decide whether to change pairing, signedness,
width/truncation, selector logic, constants, missing terms, or shared
intermediates.

## Method Selection

Use `template` first when structure matches common arithmetic, compare, slice,
shift, MUX, or product-add patterns. Use `polynomial` when exact structural
expansion is likely and the budget estimate is feasible. Use `symbolic` when
sample behavior is compact but structure is unclear.

If all lanes miss, write a small deterministic script to mine support sets,
candidate basis terms, repeated assignments, or sample correlations. The script
may guide the next hypothesis but never replaces `check_hypothesis` or CEC.

## Multi-Output Edits

For multi-output modules, avoid editing a single output in isolation unless all
other outputs are explicitly preserved. If outputs share intermediate wires,
validate them together. A formula that is individually correct can still be
globally non-minimal.

## Common Cost Reductions

Treat cost reduction as a local optimization loop over the current proved or
accepted RTL. Keep that RTL as the baseline. If a cheaper candidate fails
sample checking or CEC, reject/revert only that candidate and continue from the
baseline. If it proves, make it the new baseline and audit again.

- Share repeated products and sums through zero-cost wires.
- Try `**` for powers such as cubes.
- Factor sibling outputs around a shared base.
- Replace power-of-two multiplication/division with shifts when equivalent.
- Compare `?:`, compare, reduction, `%`, and `case` forms against expanded RTL.
- Remove explicit zero extensions if compact RTL has the same CEC result and
  lower contest cost.

For each final output group, write down one concrete cost-audit decision: a
cheaper form that was tried and failed, or the reason no cheaper equivalent form
is plausible.
