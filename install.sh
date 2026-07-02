#!/usr/bin/env bash
set -euo pipefail

# Installation script for ICCAD22 Problem A test environment
# Sets up test cases and skills for gate-spy

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

have() {
    command -v "$1" >/dev/null 2>&1
}

fail_missing() {
    local name="$1"
    local hint="$2"
    echo "  Error: $name is required but could not be installed automatically."
    echo "  $hint"
    exit 1
}

brew_available() {
    have brew
}

install_with_brew() {
    local package="$1"
    local mode="${2:-formula}"
    if ! brew_available; then
        return 1
    fi
    echo "  Installing $package with Homebrew..."
    if [[ "$mode" == "cask" ]]; then
        brew install --cask "$package"
    else
        brew install "$package"
    fi
}

add_julia_app_to_path() {
    local julia_bin
    for julia_bin in /Applications/Julia-*.app/Contents/Resources/julia/bin/julia; do
        if [[ -x "$julia_bin" ]]; then
            export PATH="$(dirname "$julia_bin"):$PATH"
            return 0
        fi
    done
    return 1
}

ensure_uv() {
    if have uv; then
        return
    fi
    echo "  uv not found."
    if install_with_brew uv; then
        have uv && return
    fi
    fail_missing "uv" "Install uv manually: https://docs.astral.sh/uv/"
}

ensure_julia() {
    add_julia_app_to_path || true
    if have julia; then
        return
    fi
    echo "  Julia not found."
    if install_with_brew julia cask || install_with_brew julia; then
        add_julia_app_to_path || true
        have julia && return
    fi
    fail_missing "Julia" "On macOS install Homebrew, then run: brew install --cask julia"
}

ensure_yosys() {
    if ! have yosys; then
        echo "  yosys not found."
        if ! install_with_brew yosys; then
            fail_missing "yosys" "On macOS install Homebrew, then run: brew install yosys"
        fi
    fi
    if ! have yosys-abc; then
        echo "  yosys-abc not found after checking yosys."
        if brew_available; then
            echo "  Reinstalling yosys once to restore bundled yosys-abc..."
            brew reinstall yosys || true
        fi
    fi
    have yosys || fail_missing "yosys" "On macOS install Homebrew, then run: brew install yosys"
    have yosys-abc || fail_missing "yosys-abc" "Install a Yosys build that includes yosys-abc; Homebrew's yosys package normally provides it."
}

echo "=== ICCAD22 Problem A test environment setup ==="
echo

# 1. Install Python dependencies and check external tools
echo "[1/4] Installing Python dependencies and checking tools..."
ensure_uv

uv sync --dev

uv run python -c "import numpy, pandas, sympy; print('  Core Python inference deps OK')"

ensure_julia
julia --version | sed 's/^/  /'
uv run python -c "import pysr; print('  PySR import OK')"

ensure_yosys
echo "  Yosys tools OK"

# 2. Create symlink for iccad22 skill
echo
echo "[2/4] Setting up iccad22 skill..."
SKILL_SRC="skills/iccad22.md"
SKILL_DEST=".claude/skills/iccad22/SKILL.md"
REF_SRC="skills/references"
REF_DEST=".claude/skills/iccad22/references"

if [[ ! -f "$SKILL_SRC" ]]; then
    echo "Error: Skill source file not found: $SKILL_SRC"
    exit 1
fi
if [[ ! -d "$REF_SRC" ]]; then
    echo "Error: Skill reference directory not found: $REF_SRC"
    exit 1
fi

# Create directory if it doesn't exist
mkdir -p "$(dirname "$SKILL_DEST")"

if [[ -L "$SKILL_DEST" ]]; then
    echo "  Symlink already exists: $SKILL_DEST"
elif [[ -e "$SKILL_DEST" ]]; then
    echo "  Warning: $SKILL_DEST exists but is not a symlink, backing up..."
    mv "$SKILL_DEST" "${SKILL_DEST}.bak"
    ln -sf "../../../$SKILL_SRC" "$SKILL_DEST"
    echo "  Created symlink: $SKILL_DEST -> $SKILL_SRC"
else
    ln -sf "../../../$SKILL_SRC" "$SKILL_DEST"
    echo "  Created symlink: $SKILL_DEST -> $SKILL_SRC"
fi

if [[ -L "$REF_DEST" ]]; then
    ln -sfn "../../../$REF_SRC" "$REF_DEST"
    echo "  Reference symlink exists: $REF_DEST -> $REF_SRC"
elif [[ -e "$REF_DEST" ]]; then
    echo "  Warning: $REF_DEST exists but is not a symlink, backing up..."
    mv "$REF_DEST" "${REF_DEST}.bak"
    ln -sfn "../../../$REF_SRC" "$REF_DEST"
    echo "  Created reference symlink: $REF_DEST -> $REF_SRC"
else
    ln -sfn "../../../$REF_SRC" "$REF_DEST"
    echo "  Created reference symlink: $REF_DEST -> $REF_SRC"
fi

# 3. Download and extract ICCAD22 test cases
echo
echo "[3/4] Downloading ICCAD22 test cases..."
GDRIVE_ID="1axlAw1rPXbTb_kYsvbwKshcEMF37SQWz"
ZIP_FILE="ICCAD22_Problem_A.zip"
EXTRACT_DIR="examples"

if compgen -G "$EXTRACT_DIR/testcase/test*/top_primitive.v" > /dev/null; then
    echo "  ICCAD22 test cases already exist in $EXTRACT_DIR/testcase/"
elif [[ -d "$EXTRACT_DIR/ICCAD22_Problem_A" ]]; then
    echo "  ICCAD22 test cases already exist in $EXTRACT_DIR/ICCAD22_Problem_A"
    read -p "  Do you want to re-download? [y/N] " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "  Skipping download."
    else
        rm -rf "$EXTRACT_DIR/ICCAD22_Problem_A"
        echo "  Removed existing directory."
    fi
fi

if [[ ! -d "$EXTRACT_DIR/ICCAD22_Problem_A" ]] && ! compgen -G "$EXTRACT_DIR/testcase/test*/top_primitive.v" > /dev/null; then
    echo "  Downloading from Google Drive..."

    # Google Drive requires cookie handling for large files
    curl -c /tmp/cookies.txt -s -L "https://drive.google.com/uc?export=download&id=$GDRIVE_ID" > /tmp/intermezzo.html
    CODE="$(awk '/download/ {print $NF}' /tmp/cookies.txt | tail -1)"
    curl -Lb /tmp/cookies.txt "https://drive.google.com/uc?export=download&confirm=${CODE}&id=$GDRIVE_ID" -o "$ZIP_FILE"
    rm -f /tmp/cookies.txt /tmp/intermezzo.html

    if [[ ! -f "$ZIP_FILE" || ! -s "$ZIP_FILE" ]]; then
        echo "  Error: Download failed or file is empty."
        echo "  Please download manually from:"
        echo "  https://drive.google.com/file/d/$GDRIVE_ID/view"
        rm -f "$ZIP_FILE"
        exit 1
    fi

    echo "  Extracting to $EXTRACT_DIR/..."
    # Google Drive may serve gzip-compressed tar files
    FILE_TYPE=$(file -b "$ZIP_FILE")
    if [[ "$FILE_TYPE" == *"gzip"* ]]; then
        # It's a gzipped tar file
        gunzip -c "$ZIP_FILE" | tar -xf - -C "$EXTRACT_DIR/"
    elif [[ "$FILE_TYPE" == *"Zip"* ]]; then
        # It's a regular zip file
        unzip -q "$ZIP_FILE" -d "$EXTRACT_DIR/"
    else
        echo "  Error: Unknown file type: $FILE_TYPE"
        rm "$ZIP_FILE"
        exit 1
    fi

    rm "$ZIP_FILE"
    echo "  Done. Test cases extracted to $EXTRACT_DIR/ICCAD22_Problem_A"
fi

# 4. Generate .mcp.json
echo
echo "[4/4] Generating .mcp.json..."

cat > .mcp.json << 'EOF'
{
  "mcpServers": {
    "gate-spy": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "python", "mcp_server.py"]
    }
  }
}
EOF

echo "  Created .mcp.json"

echo
echo "=== ICCAD22 test environment setup complete! ==="
echo
echo "Summary:"
echo "  ✓ iccad22 skill available at .claude/skills/iccad22/SKILL.md"
echo "  ✓ iccad22 references available at .claude/skills/iccad22/references/"
echo "  ✓ ICCAD22 test cases available under examples/"
echo "  ✓ .mcp.json configured for MCP server"
echo
echo "Usage:"
echo "  - Run tests: uv run pytest test/test_circuit.py::test_iccad22_*"
echo "  - Use skill: /iccad22 <command>"
echo "  - MCP server: Configured in .mcp.json"
