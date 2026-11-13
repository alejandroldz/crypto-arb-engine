#!/usr/bin/env bash
# Snapshot soak state (funding log + engine log tail) to the soak-data branch.
# Survives sandbox recycles: run from a wake; on a fresh sandbox, restore first.
set -e
cd "$(dirname "$0")/.."
U=$(cat ~/.vault/github-pat-crypto-arb-engine-824827/username)
T=$(cat ~/.vault/github-pat-crypto-arb-engine-824827/password)
URL="https://$U:$T@github.com/alejandroldz/crypto-arb-engine.git"
WT=/tmp/soak-data-wt
if ! git worktree list | grep -q soak-data-wt; then
  git fetch -q "$URL" soak-data:soak-data 2>/dev/null || true
  if git rev-parse --verify soak-data >/dev/null 2>&1; then
    git worktree add -q "$WT" soak-data
  else
    git worktree add -q --orphan soak-data "$WT" 2>/dev/null || { git checkout -q --orphan soak-data && git worktree add -q "$WT" soak-data && git checkout -q main; }
  fi
fi
[ -f funding_log.csv ] && cp funding_log.csv "$WT/"
[ -f engine.log ] && tail -2000 engine.log > "$WT/engine.log"
cd "$WT"
git add -A
git commit -q -m "soak snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)" || true
git push -q "$URL" soak-data:soak-data
echo "synced $(date -u +%H:%M:%S)"
