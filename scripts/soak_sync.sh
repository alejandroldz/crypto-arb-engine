#!/usr/bin/env bash
# Snapshot soak state (funding log + engine log tail) to the soak-data branch.
set -e
cd "$(dirname "$0")/.."
U=$(cat ~/.vault/github-pat-crypto-arb-engine-824827/username)
T=$(cat ~/.vault/github-pat-crypto-arb-engine-824827/password)
URL="https://$U:$T@github.com/alejandroldz/crypto-arb-engine.git"
WT=/tmp/soak-data-wt
rm -rf "$WT"
git worktree prune
git fetch -q "$URL" soak-data 2>/dev/null && HAS=1 || HAS=0
if [ "$HAS" = 1 ]; then
  git worktree add -q "$WT" soak-data
else
  git worktree add -q --detach "$WT"
  git -C "$WT" checkout -q --orphan soak-data
  git -C "$WT" rm -rq . 2>/dev/null || true
fi
[ -f funding_log.csv ] && cp funding_log.csv "$WT/"
[ -f engine.log ] && tail -2000 engine.log > "$WT/engine.log"
cd "$WT"
git add -A
git commit -q -m "soak snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)" || true
git push -q "$URL" soak-data:soak-data
cd - >/dev/null
git worktree remove --force "$WT"
echo "synced $(date -u +%H:%M:%S)"
