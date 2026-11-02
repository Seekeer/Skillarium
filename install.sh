#!/usr/bin/env bash
# Install per-user links and an hourly systemd timer.
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
mode=${1:---install}
case "$mode" in
    --install|--sync-only|--link-only|--uninstall) ;;
    --help|-h)
        echo 'Usage: bash install.sh [--install|--sync-only|--link-only|--uninstall]'
        exit 0 ;;
    *) echo "Unknown mode: $mode" >&2; exit 2 ;;
esac
if (($# > 1)); then echo 'Expected one mode argument.' >&2; exit 2; fi

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [[ "$mode" == --uninstall ]]; then
    systemctl --user disable --now agent-skills.timer
    # Leave skills and links available after disabling automatic updates.
    for unit in agent-skills.service agent-skills.timer; do
        if [[ -f "$unit_dir/$unit" ]] && rg_marker=$(head -n 1 "$unit_dir/$unit") &&
            [[ "$rg_marker" == '# Managed by agent-skills' ]]; then
            rm -- "$unit_dir/$unit"
        fi
    done
    systemctl --user daemon-reload
    exit 0
fi

lock_path=$(git -C "$repo" rev-parse --git-path agent-skills-sync.lock)
[[ "$lock_path" = /* ]] || lock_path="$repo/$lock_path"
exec 9>"$lock_path"
if ! flock -n 9; then echo 'Another sync is running; skipped.'; exit 0; fi

source "$repo/scripts/client.sh"
source "$repo/scripts/bindings.sh"
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=Never
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -oBatchMode=yes -oConnectTimeout=15}"
result=0
if [[ "$mode" != --link-only ]]; then sync_repository || result=1; fi
# Global links and registered project bindings follow the local choices.
{ load_bindings && apply_bindings; } || result=1
if ((result)); then exit "$result"; fi
if [[ "$mode" != --install ]]; then exit 0; fi

# Quote systemd ExecStart syntax, including its percent and dollar expansion.
unit_path=$repo
unit_path=${unit_path//\\/\\\\}
unit_path=${unit_path//\"/\\\"}
unit_path=${unit_path//%/%%}
unit_path=${unit_path//\$/\$\$}
for unit in agent-skills.service agent-skills.timer; do
    if [[ -e "$unit_dir/$unit" ]] &&
        [[ $(head -n 1 "$unit_dir/$unit") != '# Managed by agent-skills' ]]; then
        echo "Existing unmanaged unit preserved: $unit_dir/$unit" >&2; exit 1
    fi
done
mkdir -p -- "$unit_dir"
cat >"$unit_dir/agent-skills.service" <<EOF
# Managed by agent-skills
[Unit]
Description=Synchronize personal agent skills
[Service]
Type=oneshot
ExecStart=/usr/bin/bash "$unit_path/install.sh" --sync-only
TimeoutStartSec=5min
EOF
cat >"$unit_dir/agent-skills.timer" <<'EOF'
# Managed by agent-skills
[Unit]
Description=Synchronize personal agent skills every hour
[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=2min
[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now agent-skills.timer
echo 'Installed. Inspect: systemctl --user status agent-skills.timer'
