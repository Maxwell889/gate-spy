#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------------------
# yosys_synth.sh — Synthesize a Verilog design with Yosys
# ------------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: $0 <input.v> <output.v> [-t <top_module>] [-l <liberty_file>] [--flatten] [--no-synth]

Arguments:
  input.v                Input Verilog file (required, first positional)
  output.v               Output synthesized Verilog file (required, second positional)

Options:
  -t, --top MODULE       Top module name (default: auto-top)
  -l, --lib FILE         Liberty cell library file for technology mapping
  --flatten              Flatten the design hierarchy
  --no-synth             Skip tech mapping, only run front-end elaboration
  -h, --help             Show this help message and exit
EOF
    exit 0
}

# --- Defaults ----------------------------------------------------------------
INPUT=""
OUTPUT=""
TOP_MODULE=""
LIBERTY_FILE=""
FLATTEN=0
NO_SYNTH=0

# --- Argument parsing --------------------------------------------------------
# Parse positional arguments first
if [[ $# -gt 0 && "$1" != -* ]]; then
    INPUT="$1"; shift
fi
if [[ $# -gt 0 && "$1" != -* ]]; then
    OUTPUT="$1"; shift
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--top)
            TOP_MODULE="$2"; shift 2 ;;
        -l|--lib)
            LIBERTY_FILE="$2"; shift 2 ;;
        --flatten)
            FLATTEN=1; shift ;;
        --no-synth)
            NO_SYNTH=1; shift ;;
        -h|--help)
            usage ;;
        *)
            echo "Error: Unknown option '$1'"
            usage ;;
    esac
done

# --- Validation --------------------------------------------------------------
if [[ -z "$INPUT" ]]; then
    echo "Error: Input Verilog file is required as the first positional argument"
    usage
fi

if [[ -z "$OUTPUT" ]]; then
    echo "Error: Output file is required as the second positional argument"
    usage
fi

if [[ ! -f "$INPUT" ]]; then
    echo "Error: Input file '$INPUT' not found"
    exit 1
fi

if [[ -n "$LIBERTY_FILE" && ! -f "$LIBERTY_FILE" ]]; then
    echo "Error: Liberty file '$LIBERTY_FILE' not found"
    exit 1
fi

# --- Build Yosys script ------------------------------------------------------
SCRIPT_CONTENT=""

# Read and elaborate
SCRIPT_CONTENT+="read_verilog $INPUT\n"

if [[ -n "$TOP_MODULE" ]]; then
    SCRIPT_CONTENT+="hierarchy -check -top $TOP_MODULE\n"
else
    SCRIPT_CONTENT+="hierarchy -check -auto-top\n"
fi

SCRIPT_CONTENT+="proc\n"

# Optional flatten
if [[ $FLATTEN -eq 1 ]]; then
    SCRIPT_CONTENT+="flatten\n"
fi

# Synthesis or front-end only
if [[ $NO_SYNTH -eq 0 ]]; then
    if [[ -n "$TOP_MODULE" ]]; then
        SCRIPT_CONTENT+="synth -top $TOP_MODULE"
    else
        SCRIPT_CONTENT+="synth -auto-top"
    fi

    # Technology mapping with liberty file if provided
    if [[ -n "$LIBERTY_FILE" ]]; then
        SCRIPT_CONTENT+="\ndfflibmap -liberty $LIBERTY_FILE\n"
        SCRIPT_CONTENT+="abc -liberty $LIBERTY_FILE\n"
    fi

    SCRIPT_CONTENT+="\n"
fi

# Write output
SCRIPT_CONTENT+="write_verilog $OUTPUT\n"

# --- Run Yosys ---------------------------------------------------------------
echo "=== Yosys Synthesis ==="
echo " Input:    $INPUT"
echo " Output:   $OUTPUT"
[[ -n "$TOP_MODULE" ]]  && echo " Top:      $TOP_MODULE" || echo " Top:      auto-top"
[[ $FLATTEN -eq 1 ]]    && echo " Flatten:  yes"
[[ -n "$LIBERTY_FILE" ]] && echo " Lib:      $LIBERTY_FILE"
[[ $NO_SYNTH -eq 1 ]]   && echo " Synth:    skipped (front-end only)"
echo ""

printf '%b' "$SCRIPT_CONTENT" | yosys -s /dev/stdin
