#!/usr/bin/env bash
# Controller on i3: approve permitted upstream updates and publish the registry.
set -euo pipefail
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
mode=${1:---check}
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
case "$mode" in
    --install-timer)
        path=$repo
        path=${path//\\/\\\\}; path=${path//\"/\\\"}
        path=${path//%/%%}; path=${path//\$/\$\$}
        mkdir -p -- "$unit_dir"
        for unit in agent-skills-check.service agent-skills-check.timer; do
            if [[ -e "$unit_dir/$unit" ]] &&
                [[ $(head -n 1 "$unit_dir/$unit") != '# Managed by agent-skills checker' ]]; then
                echo "Existing unmanaged unit preserved: $unit" >&2; exit 1
            fi
        done
        cat >"$unit_dir/agent-skills-check.service" <<EOF
# Managed by agent-skills checker
[Unit]
Description=Check upstream skills and publish approved versions
[Service]
Type=oneshot
ExecStart=/usr/bin/bash "$path/check-updates.sh" --apply-auto
TimeoutStartSec=15min
EOF
        cat >"$unit_dir/agent-skills-check.timer" <<'EOF'
# Managed by agent-skills checker
[Unit]
Description=Check upstream skill sources daily
[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=10min
[Install]
WantedBy=timers.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now agent-skills-check.timer
        exit 0 ;;
    --uninstall-timer)
        systemctl --user disable --now agent-skills-check.timer
        for unit in agent-skills-check.service agent-skills-check.timer; do
            if [[ -f "$unit_dir/$unit" ]] &&
                [[ $(head -n 1 "$unit_dir/$unit") == '# Managed by agent-skills checker' ]]; then
                rm -- "$unit_dir/$unit"
            fi
        done
        systemctl --user daemon-reload
        exit 0 ;;
    --check|--apply-auto) ;;
    *) echo 'Usage: check-updates.sh [--check|--apply-auto|--install-timer|--uninstall-timer]' >&2; exit 2 ;;
esac
lock_path=$(git -C "$repo" rev-parse --git-path agent-skills-sync.lock)
[[ "$lock_path" = /* ]] || lock_path="$repo/$lock_path"
exec 9>"$lock_path"
if ! flock -n 9; then echo 'Another sync/check is running; skipped.'; exit 0; fi
if [[ -n $(git -C "$repo" status --porcelain) ]]; then
    echo 'Repository has local changes; checker stopped.' >&2; exit 1
fi
export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=Never
export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -oBatchMode=yes -oConnectTimeout=15}"
git -C "$repo" pull --ff-only
if [[ "$mode" == --apply-auto ]]; then
    python3 "$repo/scripts/skills.py" check --apply-auto --publish
else
    python3 "$repo/scripts/skills.py" check
fi
