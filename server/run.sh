#!/usr/bin/env bash
# Daily server run. Maintenance itself works in a disposable worktree of
# origin/main. manage.py finds the library (SKILLARIUM_LIBRARY, the legacy
# layout or the Skillarium configuration); `--repo <folder>` may precede the command.
set -euo pipefail
engine=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=Never
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -oBatchMode=yes -oConnectTimeout=15}"
if (($# == 0)) || { (($# == 2)) && [[ "$1" == --repo ]]; }; then set -- "$@" maintain --apply-auto; fi
exec python3 "$engine/server/manage.py" "$@"
