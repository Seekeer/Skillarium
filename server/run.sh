#!/usr/bin/env bash
# Daily server run. Maintenance itself works in a disposable worktree of
# origin/main. manage.py finds the library (SKILLARIUM_LIBRARY, the legacy
# layout or the Skillarium configuration); `--repo <folder>` may precede the command.
set -euo pipefail
engine=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=Never
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -oBatchMode=yes -oConnectTimeout=15}"
if (($# == 0)) || { (($# == 2)) && [[ "$1" == --repo ]]; } || { (($# == 1)) && [[ "$1" == --repo=* ]]; }; then
    # After a self-update the library's PR check follows this engine's release.
    python3 "$engine/server/manage.py" "$@" pin-policy || echo 'Policy engine not pinned; retried tomorrow.' >&2
    set -- "$@" maintain --apply-auto
fi
exec python3 "$engine/server/manage.py" "$@"
