#!/usr/bin/env bash
# Finish migration: index missing chunks, then fetch+index arxiv-without-tex.
set -u
cd "$(dirname "$0")/.."
LOG="chain_runs/tex_migration_finish.log"
export PYTHONUNBUFFERED=1

{
  echo "=== tex migration finish start $(date -Iseconds) ==="
  .venv/bin/python scripts/finish_tex_migration.py
  echo "=== tex migration finish done $(date -Iseconds) ==="
} >> "$LOG" 2>&1
