#!/usr/bin/env bash
# Snapshot soak state (funding log + engine log tail) to the soak-data branch.
set -e
cd "$(dirname "$0")/.."
U=$(cat ~/.vault/github-pat-crypto-arb-engine-824827/username)
T=$(cat ~/.vault/github-pat-crypto-arb-engine-824827/password)
# OPS: never put the PAT in the git URL (visible in `ps` and in stderr on
# failure). One-shot credential header; push stderr goes nowhere public.
AUTH="http.extraHeader=Authorization: Basic $(printf '%s:%s' "$U" "$T" | base64 -w0)"
URL="https://github.com/alejandroldz/crypto-arb-engine.git"
WT=/tmp/soak-data-wt
rm -rf "$WT"
git worktree prune
git -c "$AUTH" fetch -q "$URL" soak-data 2>/dev/null && HAS=1 || HAS=0
if [ "$HAS" = 1 ]; then
  git worktree add -q "$WT" soak-data
else
  git worktree add -q --detach "$WT"
  git -C "$WT" checkout -q --orphan soak-data
  git -C "$WT" rm -rq . 2>/dev/null || true
fi
[ -f funding_log.csv ] && cp funding_log.csv "$WT/"
[ -f engine.log ] && tail -2000 engine.log > "$WT/engine.log"
# op.#10: positions backup via SQLite's own consistent backup (never a raw
# copy of a live DB, which can tear against the WAL)
DB="data/arb_state.db"; [ -f "$DB" ] || DB="arb_state.db"
if [ -f "$DB" ]; then
  uv run python - "$DB" "$WT/arb_state_backup.db" <<'PY' 2>/dev/null || true
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
c = sqlite3.connect(src)
c.execute(f"VACUUM INTO '{dst}'")
c.close()
PY
fi
# The engine exports its funding history hourly (it owns the write lock, so
# an outside read-only DuckDB connect always fails). Pick up that export so
# a sandbox recycle does not lose the soak's core analysis data.
FH="data/funding_history_export.csv"; [ -f "$FH" ] || FH="funding_history_export.csv"
[ -f "$FH" ] && cp "$FH" "$WT/funding_history.csv"
cd "$WT"
git add -A
git commit -q -m "soak snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)" || true
git -c "$AUTH" push -q "$URL" soak-data:soak-data 2>/dev/null || echo "push failed (stderr suppressed: may contain credentials)"
cd - >/dev/null
git worktree remove --force "$WT"
echo "synced $(date -u +%H:%M:%S)"
