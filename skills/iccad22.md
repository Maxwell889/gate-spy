# ICCAD 2022 Problem A — Gate-Level to Word-Level Recovery

Given a flattened gate-level netlist (primitive gates only), you must recover the original word-level arithmetic operations and output functionally-equivalent RTL.
The scoring metric is **cost** fewer operators and keywords means a higher reduction rate.  A reduction rate below 70% scores zero.

You are an agent performing this recovery iteratively: analyze the circuit, propose edits
that replace gate structures with word-level operators, and verify each step via CEC.

## Prerequisites

If the user did **not** provide a Verilog file path, ask for it first. Do not proceed without one.

## Workflow

### Phase 1 — Load & Understand

1. `read_file <path>` — loads the netlist, builds circuit IR, saves source code.
2. Inspect the circuit:
   - `get_node` on I/O ports to see top-level structure.
   - `find_cone` (backward from outputs) to discover dataflow boundaries.
   - `print_adder_stats` to locate adder trees (half/full adders).
   - `print_xor_stats` to find XOR chains (often part of arithmetic).
   - `simulate` with a few patterns to get a rough sense of the function.

### Phase 2 — Iterative Optimization

Edit the source code with `edit(matches, replacements)`:

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

### Phase 3 — Finalize

When no further optimizations seem possible:
1. `show(detail=true)` — review the final code.
2. `dump <output_path>` — write the result.

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
- **Reuse intermediate signals.**  When a computed output appears as a sub-expression elsewhere, reference the output wire directly instead of recomputing — each reused signal saves operators and reduces cost.
- **Revert if stuck.**  `revert(help=true)` shows history; roll back bad edits and try another path.
- **Review before dump.**  Before dumping, review the final code for trivial simplifications (e.g., common sub-expressions, redundant parentheses, mergeable operations). Dump when you've tried several ideas and none yield further improvements.
- **Trust CEC over manual simulation.**  Once you have a rough hypothesis, `edit` it directly rather than exhaustively simulating corner cases.  Rejected edits return precise counterexamples that pinpoint the flaw — iterate on those instead of guessing patterns from random input vectors.
- **Declare signed wires instead of using `$signed()`.**  When a comparison needs signed semantics, declare intermediate wires as `wire signed [W-1:0]` and compare them directly.  Verilog then uses implicit signed comparison, and the cost function counts only the real arithmetic operators.
