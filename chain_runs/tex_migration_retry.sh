#!/usr/bin/env bash
# Wait for tex_migration.sh, then retry fetch-source for arXiv papers that lack TeX text.
set -u
cd "$(dirname "$0")/.."
LOG="chain_runs/tex_migration_retry.log"
PID_FILE="chain_runs/tex_migration.pid"
export PYTHONUNBUFFERED=1

{
  echo "=== tex migration retry start $(date -Iseconds) ==="

  if [[ -f "$PID_FILE" ]]; then
    main_pid="$(cat "$PID_FILE")"
    echo "waiting for main migration pid=$main_pid ..."
    while kill -0 "$main_pid" 2>/dev/null; do
      sleep 30
    done
    echo "main migration finished"
  fi

  echo ""
  echo "=== failed papers (arxiv but no tex source) ==="
  mapfile -t PIDS < <(.venv/bin/python <<'PYEOF'
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
