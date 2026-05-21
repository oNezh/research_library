#!/usr/bin/env bash
# Retry fetch+index for arXiv papers that still lack TeX source (macOS bash compatible).
set -u
cd "$(dirname "$0")/.."
LOG="chain_runs/tex_migration_retry.log"
export PYTHONUNBUFFERED=1

{
  echo "=== tex migration retry start $(date -Iseconds) ==="

  echo ""
  echo "=== failed papers (arxiv but no tex source) ==="
  PIDS=()
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && PIDS+=("$pid")
  done < <(.venv/bin/python <<'PYEOF'
from research_library.library import db
conn = db.connect()
rows = conn.execute("""
SELECT id FROM papers
WHERE arxiv_id IS NOT NULL AND TRIM(arxiv_id) != ''
  AND (
    source_text_relpath IS NULL OR TRIM(source_text_relpath) = ''
    OR source_kind IS NULL OR TRIM(source_kind) = ''
    OR source_kind != 'tex'
  )
ORDER BY id
""").fetchall()
for (pid,) in rows:
    print(pid)
PYEOF
)
  echo "count=${#PIDS[@]}"
  if ((${#PIDS[@]} == 0)); then
    echo "nothing to retry"
    echo "=== tex migration retry done $(date -Iseconds) ==="
    exit 0
  fi

  echo ""
  echo "=== Phase 1: fetch-source --force (failed only) ==="
  args=()
  for pid in "${PIDS[@]}"; do
    args+=(--paper-id "$pid")
  done
  .venv/bin/research-lib library fetch-source --force "${args[@]}" || echo "fetch-source retry finished with errors"

  echo ""
  echo "=== Phase 2: semantic-index --force (retried papers) ==="
  idx_args=()
  for pid in "${PIDS[@]}"; do
    idx_args+=(--paper-id "$pid")
  done
  .venv/bin/research-lib library semantic-index --force "${idx_args[@]}" || echo "semantic-index retry finished with errors"

  echo ""
  echo "=== summary ==="
  .venv/bin/python <<'PYEOF'
from research_library.library import db
conn = db.connect()
tex = conn.execute("SELECT COUNT(*) FROM papers WHERE source_kind='tex'").fetchone()[0]
fail = conn.execute("""
SELECT COUNT(*) FROM papers
WHERE arxiv_id IS NOT NULL AND TRIM(arxiv_id)!=''
  AND (source_text_relpath IS NULL OR TRIM(source_text_relpath)='' OR source_kind!='tex')
""").fetchone()[0]
print(f"source_kind=tex: {tex}")
print(f"arxiv without tex after retry: {fail}")
PYEOF

  echo "=== tex migration retry done $(date -Iseconds) ==="
} >> "$LOG" 2>&1
