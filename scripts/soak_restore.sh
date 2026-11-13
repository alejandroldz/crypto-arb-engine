#!/usr/bin/env bash
# Restore soak state from the soak-data branch after a sandbox recycle.
set -e
cd "$(dirname "$0")/.."
U=$(cat ~/.vault/github-pat-crypto-arb-engine-824827/username)
T=$(cat ~/.vault/github-pat-crypto-arb-engine-824827/password)
URL="https://$U:$T@github.com/alejandroldz/crypto-arb-engine.git"
if git fetch -q "$URL" soak-data:soak-data 2>/dev/null; then
  git show soak-data:funding_log.csv > funding_log.csv 2>/dev/null && echo "restored $(($(wc -l < funding_log.csv) - 1)) funding samples"
fi
