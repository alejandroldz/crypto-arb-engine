#!/usr/bin/env bash
# Restore soak state from the soak-data branch after a sandbox recycle.
set -e
cd "$(dirname "$0")/.."
U=$(cat ~/.vault/github-pat-crypto-arb-engine-824827/username)
T=$(cat ~/.vault/github-pat-crypto-arb-engine-824827/password)
# OPS: never put the PAT in the git URL (visible in `ps` and in stderr on
# failure). Use a one-shot credential header instead.
AUTH="http.extraHeader=Authorization: Basic $(printf '%s:%s' "$U" "$T" | base64 -w0)"
REPO="https://github.com/alejandroldz/crypto-arb-engine.git"
if git -c "$AUTH" fetch -q "$REPO" soak-data:soak-data 2>/dev/null; then
  git show soak-data:funding_log.csv > funding_log.csv 2>/dev/null && echo "restored $(($(wc -l < funding_log.csv) - 1)) funding samples"
  # OPS: restore the positions DB too (sync backs it up via VACUUM INTO);
  # without it a recycle silently loses open carries
  mkdir -p data
  if git show soak-data:arb_state_backup.db > data/arb_state.db 2>/dev/null; then
    echo "restored positions DB ($(du -h data/arb_state.db | cut -f1))"
  fi
fi
