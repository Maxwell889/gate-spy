# GateSpy Tool Reference

State boundary:

- `read_file` loads the design state and current recovered source.
- Observation tools inspect the loaded design and do not modify source.
- Hypothesis tools create evidence/candidates and do not modify source.
- `edit` is the only MCP tool that changes the current recovered source.
- `dump` only writes the current accepted recovered source to disk.
- `scripts/cost.py` measures contest cost but proves nothing.

| Tool | Purpose | Typical call | Returns | State change |
| --- | --- | --- | --- | --- |
| `read_file(path)` | Load primitive Verilog/AIGER. | `read_file("examples/testcase/test01/top_primitive.v")` | Circuit summary, PI/PO words, gate counts. | Loads current circuit/source. |
| `get_node(node, depth=2, detail=False)` | Inspect a signal, bus, prefix, or wildcard. | `get_node("out1", detail=True)` | Gate kind, fan-in/fan-out, bus summary, match groups. | None. |
| `find_cone(signals, direction, stop_at=None, depth=-1, detail=False)` | Trace fan-in/fan-out boundary. | `find_cone(["out1"], "backward", depth=3)` | Layered cone, support/boundary summary, gate kinds. | None. |
| `simulate(pattern_num=100, fixed_inputs=None, watch=None, seed=None)` | Generate behavioral samples. | `simulate(16, fixed_inputs={"in1": 3}, seed=1)` | Input/output rows and optional watched internal values. | None. |
| `print_adder_stats(detail=False)` | Detect HA/FA and adder trees. | `print_adder_stats(detail=True)` | HA/FA counts, CSA/CPA/tree hints, boundary signals. | None. |
| `print_xor_stats(detail=False)` | Detect XOR chains. | `print_xor_stats(detail=True)` | XOR counts and chain hints. | None. |
| `propose_strategy(output="", detail=False, budget=None)` | Rank recovery lanes. | `propose_strategy(output="out3", detail=True)` | Method ranking, cone/support metrics, budget estimates. | None. |
| `run_method(output, method, sample_num=256, budget=None, detail=False)` | Run one lane: `template`, `polynomial`, or `symbolic`. | `run_method("out3", "symbolic")` | Candidate expressions, sample status, hypothesis ids, budget status. | Stores candidates only. |
| `infer_candidates(output="", methods=None, sample_num=256, detail=False)` | Generate built-in template candidates. | `infer_candidates(output="", detail=True)` | Support words, fitted candidates, optional `assignments_json`. | Stores candidates only. |
| `fit_basis(output, basis, include_constant=True, coefficient_limit=4096, sample_num=256)` | Fit `const + sum(ci*basis_i)` from LLM-provided terms. | `fit_basis("out1", ["in1", "in2", "in1*in2"])` | Coefficients, expression, sample status, or no-fit reason. | Stores candidate. |
| `fit_hypothesis(output, template, unknowns=None, sample_num=256)` | Solve unknown integer parameters in a template. | `fit_hypothesis("out1", "A*in1+B", ["A","B"])` | Solved expression or no-fit reason. | Stores candidate. |
| `check_hypothesis(assignments, declarations="", sample_num=256, run_cec=True, share_common=True)` | Validate LLM-proposed assignments against the loaded design. | `check_hypothesis({"out1": "in1 + in2"})` | Sample result, CEC status when all outputs are assigned, cost, mismatch/CEX notes. | None. |
| `trace_counterexample(hypothesis_id="", output="", bits=None, depth=3)` | Replay latest failed hypothesis/CEX. | `trace_counterexample(output="out3", depth=4)` | Mismatch bits, input valuation, subterm values, related cone. | None. |
| `explain_failure(hypothesis_id="", output="", depth=3)` | Summarize why a hypothesis failed. | `explain_failure(output="out3")` | Failure state, likely cause, suggested next tool/action. | None. |
| `edit(matches=None, replacements=None, rewrite="", begin="", end="", accept_timeout=False)` | Apply a complete or targeted source edit and run CEC. | `edit(rewrite="<complete module>")` | Accepted/rejected status, CEC state, cost before/after, edit id. | Changes source only if accepted. |
| `revert(depth=1, id=0, help=False)` | Inspect or undo accepted edits. | `revert(help=True)` | Edit history or revert confirmation. | Reverts accepted source state. |
| `show(detail=False, grep="")` | Read current recovered source. | `show(detail=True)` | Full/abridged/filtered source. | None. |
| `dump(path)` | Write accepted recovered source. | `dump("examples/.../top_recovered.v")` | File path and size. | Writes file. |

Treat `propose_strategy` as a prioritization hint, not an authority. Cross-check
the suggested lane against structure: flattened multipliers can look like
AND/XOR trees, comparator/subtractor logic can resemble arithmetic trees, and
MUX select signals can be buried in control cones.

`check_hypothesis` sample checking understands `$signed(...)`, `$unsigned(...)`,
and simple local signed declarations. Partial assignments are useful for sample
debugging, but final proof must cover every output. It still validates an
expression, not a human explanation; if samples fail, use `explain_failure` or
`trace_counterexample` before changing the template.
