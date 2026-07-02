# ICCAD22 Scoring Reference

Reduction rate must be at least 70% to score:

```text
reduction_rate = (1 - cost / gate_count) * 100%
```

Allowed operator costs:

| Operators | Cost |
| --- | --- |
| `{}` and `{{}}` concatenation / replication | 1 per symbol |
| `[]` and `[:]` bit-select / part-select | 1 |
| `+`, `-`, `*`, `/`, `**` | 1 |
| `%` | 1 |
| `>`, `>=`, `<`, `<=` | 1 |
| `!`, `&&`, `||`, `==`, `!=`, `===`, `!==` | 1 |
| bit-wise `~`, `&`, `|`, `^`, `^~`, `~^` | 1 per bit |
| reduction `&`, `~&`, `|`, `~|`, `^`, `~^`, `^~` | 1 |
| `<<`, `>>`, `<<<`, `>>>` | 1 |
| `?:` | 1 |

Allowed keyword costs:

| Keywords | Cost |
| --- | --- |
| `and`, `buf`, `nand`, `nor`, `not`, `or`, `xor`, `xnor` primitive instantiation | 1 |
| `case`, `casex`, `casez` | 2 per non-default item |
| `if` | 1 |
| `always`, `assign`, `begin`, `default`, `else`, `end`, `endcase`, `endmodule`, `for`, `input`, `integer`, `module`, `output`, `parameter`, `reg`, `signed`, `unsigned`, `wire` | 0 |

This is a scoring model, not a style guide. Prefer the equivalent RTL with the
lowest contest cost even if it is less conventional. Use `scripts/cost.py` or
GateSpy's reported cost to compare alternatives; then use CEC to prove
equivalence.

Cost-audit prompts:

- Can repeated products/sums be shared through a zero-cost `wire`?
- Is `**` cheaper than repeated multiplication?
- Is a shift cheaper than multiplication by a power of two?
- Is a `?:`, compare, reduction, `%`, or `case` cheaper than expanded logic?
- Are part-selects or concats necessary, or are they artifacts of width padding?
- Can sibling outputs share a common base and differ only by a constant or MUX?
