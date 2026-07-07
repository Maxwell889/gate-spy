#!/usr/bin/env bash
set -euo pipefail

# Installation script for ICCAD22 Problem A test environment
# Sets up test cases and skills for gate-spy

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== ICCAD22 Problem A test environment setup ==="
echo

# 1. Create symlink for iccad22 skill
echo "[1/3] Setting up iccad22 skill..."
SKILL_SRC="skills/iccad22.md"
SKILL_DEST=".claude/skills/iccad22/SKILL.md"

if [[ ! -f "$SKILL_SRC" ]]; then
    echo "Error: Skill source file not found: $SKILL_SRC"
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

# 2. Download and extract ICCAD22 test cases
echo
echo "[2/3] Downloading ICCAD22 test cases..."
GDRIVE_ID="1axlAw1rPXbTb_kYsvbwKshcEMF37SQWz"
ZIP_FILE="ICCAD22_Problem_A.zip"
EXTRACT_DIR="examples"

if [[ -d "$EXTRACT_DIR/ICCAD22_Problem_A" ]]; then
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

if [[ ! -d "$EXTRACT_DIR/ICCAD22_Problem_A" ]]; then
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

# 3. Generate .mcp.json
echo
echo "[3/3] Generating .mcp.json..."

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
echo "  ✓ iccad22 skill available at .claude/skills/iccad22.md"
echo "  ✓ ICCAD22 test cases in examples/ICCAD22_Problem_A/"
echo "  ✓ .mcp.json configured for MCP server"
echo
echo "Usage:"
echo "  - Run tests: uv run pytest test/test_circuit.py::test_iccad22_*"
echo "  - Use skill: /iccad22 <command>"
echo "  - MCP server: Configured in .mcp.json"

