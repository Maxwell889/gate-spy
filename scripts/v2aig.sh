#!/usr/bin/env bash
set -euo pipefail

# v2aig — Convert Verilog to binary AIGER via Yosys + ABC.  No optimisation.

usage() {
    cat <<EOF
Usage: $0 <input.v> <output.aig> [-t <top_module>] [-l <liberty_file>]

Arguments:
  input.v          Input Verilog file
  output.aig       Output binary AIGER file

Options:
  -t, --top MODULE Top module name (default: auto-top)
  -l, --lib FILE   Liberty cell library for technology mapping
EOF
    exit 0
}

INPUT=""
OUTPUT=""
TOP_MODULE=""
LIBERTY=""

# Positional args
if [[ $# -gt 0 && "$1" != -* ]]; then INPUT="$1"; shift; fi
if [[ $# -gt 0 && "$1" != -* ]]; then OUTPUT="$1"; shift; fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--top) TOP_MODULE="$2"; shift 2 ;;
        -l|--lib) LIBERTY="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Error: Unknown option '$1'"; usage ;;
    esac
done

if [[ -z "$INPUT" || -z "$OUTPUT" ]]; then
    echo "Error: <input.v> and <output.aig> are required"
    usage
fi
if [[ ! -f "$INPUT" ]]; then
    echo "Error: '$INPUT' not found"; exit 1
fi
if [[ -n "$LIBERTY" && ! -f "$LIBERTY" ]]; then
    echo "Error: liberty file '$LIBERTY' not found"; exit 1
fi

HIER="hierarchy -auto-top"
if [[ -n "$TOP_MODULE" ]]; then
    HIER="hierarchy -top $TOP_MODULE"
    echo "Converting $INPUT -> $OUTPUT (top: $TOP_MODULE)..."
else
    echo "Converting $INPUT -> $OUTPUT (auto-top)..."
fi

TMP_BLIF=$(mktemp /tmp/v2aig_XXXXXX.blif)

# If liberty file is provided, use it for technology mapping.
# Otherwise, skip read_liberty and work with primitive gates directly.
if [[ -n "$LIBERTY" ]]; then
    echo "  Liberty: $LIBERTY"
    # hierarchy first so auto-top picks the user's module, not a lib cell.
    # read_liberty after hierarchy so techmap can expand library cells.
    YOSYS_CMDS="$HIER; read_liberty $LIBERTY; proc; techmap; flatten; write_blif $TMP_BLIF"
else
    echo "  No liberty file (using primitive gates)"
    YOSYS_CMDS="$HIER; proc; techmap; flatten; write_blif $TMP_BLIF"
fi

yosys -q -p "$YOSYS_CMDS" "$INPUT"

# ABC: strash → write binary AIG
yosys-abc -c "read_blif $TMP_BLIF; strash; write_aiger $OUTPUT; print_stats"

rm -f "$TMP_BLIF"
echo "Done."
