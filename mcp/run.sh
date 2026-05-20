#!/usr/bin/env bash
# Wrapper for OpenClaw stdio MCP (same pattern as quantitative_finance/qf_mcp/run.sh).
# Ensures cwd is repo root so config.find_repo_root + .env apply; prefers project .venv.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
if [[ -x "$DIR/.venv/bin/python" ]]; then
  exec "$DIR/.venv/bin/python" -m research_library.mcp_server "$@"
fi
export PYTHONPATH="$DIR/src"
# Resolve a usable Python:
#   1. RESEARCH_LIB_PYTHON if it points to an executable
#   2. python3.11 / python3 from PATH
#   3. Common Homebrew locations (Apple Silicon / Intel)
candidates=()
if [[ -n "${RESEARCH_LIB_PYTHON:-}" ]]; then
  candidates+=("$RESEARCH_LIB_PYTHON")
fi
for cmd in python3.11 python3.12 python3.10 python3; do
  if command -v "$cmd" >/dev/null 2>&1; then
    candidates+=("$(command -v "$cmd")")
  fi
done
for fallback in /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  candidates+=("$fallback")
done
for py in "${candidates[@]}"; do
  if [[ -x "$py" ]]; then
    exec "$py" -m research_library.mcp_server "$@"
  fi
done
echo "mcp/run.sh: no usable Python found (set RESEARCH_LIB_PYTHON or install python3)" >&2
exit 127
