#!/usr/bin/env bash
set -euo pipefail
updater=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
lock_path=$(git -C "$updater" rev-parse --git-path updater-maintain.lock)
[[ "$lock_path" = /* ]] || lock_path="$updater/$lock_path"
exec 9>"$lock_path"
if ! flock -n 9; then echo 'Updater already running; skipped.'; exit 0; fi
if [[ -n $(git -C "$updater" status --porcelain) ]]; then
    echo 'Updater has local changes; preserved.' >&2; exit 1
fi
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=Never
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -oBatchMode=yes -oConnectTimeout=15}"
git -C "$updater" pull --ff-only
if (($# == 0)); then set -- maintain --apply-auto --publish; fi
python3 "$updater/server/manage.py" "$@"
