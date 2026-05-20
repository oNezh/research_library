#!/usr/bin/env bash
# Full-library TeX migration: fetch ar5iv sources, then re-embed all indexable papers.
set -u
cd "$(dirname "$0")/.."
LOG="chain_runs/tex_migration.log"
export PYTHONUNBUFFERED=1

{
  echo "=== tex migration start $(date -Iseconds) ==="
  echo "cwd=$(pwd)"

  echo ""
  echo "=== Phase 1: fetch-source --all --force ==="
  .venv/bin/research-lib library fetch-source --all --force || echo "fetch-source finished with errors (continuing)"

  echo ""
  echo "=== Phase 2: semantic-index --force ==="
  .venv/bin/research-lib library semantic-index --force || echo "semantic-index finished with errors"

  echo ""
  echo "=== tex migration done $(date -Iseconds) ==="
} >> "$LOG" 2>&1
