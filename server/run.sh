#!/usr/bin/env bash
# Daily server run. Code updates arrive through the normal client sync of this
# copy; maintenance itself works in a disposable worktree of origin/main.
set -euo pipefail
library=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=Never
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -oBatchMode=yes -oConnectTimeout=15}"
if (($# == 0)); then set -- maintain --apply-auto; fi
exec python3 "$library/server/manage.py" "$@"
