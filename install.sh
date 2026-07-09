#!/usr/bin/env bash
set -euo pipefail

# GateSpy local setup:
#   1. install/check project dependencies
#   2. expose the iccad22 Claude skill
#   3. generate the local MCP configuration
#
# This script intentionally does not download testcase data.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== GateSpy local environment setup ==="
echo

if ! command -v uv >/dev/null 2>&1; then
    echo "Error: uv is required but was not found in PATH."
    echo "Install uv first, then rerun: bash install.sh"
    exit 1
fi

# 1. Install project dependencies, including newly added PySR/SymPy packages.
echo "[1/3] Installing project dependencies..."
uv sync --dev

echo "  Verifying dependency availability..."
uv run python -c 'import importlib.util as u; missing=[m for m in ("mcp","langfuse","pysr","sympy","pytest") if u.find_spec(m) is None]; raise SystemExit("missing dependencies: "+", ".join(missing) if missing else 0)'
echo "  Dependencies are available."
echo "  Recovery IR/local RAG uses Python standard-library JSON/path/hash utilities; no vector DB or embedding dependency is required."

# 2. Create symlink for iccad22 skill.
echo
echo "[2/3] Setting up iccad22 skill..."
SKILL_SRC="skills/iccad22.md"
SKILL_DEST=".claude/skills/iccad22/SKILL.md"

if [[ ! -f "$SKILL_SRC" ]]; then
    echo "Error: skill source file not found: $SKILL_SRC"
    exit 1
fi

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

# 3. Generate .mcp.json.
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
echo "=== GateSpy local environment setup complete ==="
echo
echo "Summary:"
echo "  - Dependencies installed with uv sync --dev"
echo "  - iccad22 skill available at .claude/skills/iccad22/SKILL.md"
echo "  - .mcp.json configured for the GateSpy MCP server"
echo "  - Testcase data was not downloaded or modified"
echo
echo "Usage:"
echo "  - Run tests: uv run pytest"
echo "  - Start MCP server: uv run python mcp_server.py"
