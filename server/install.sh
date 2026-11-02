#!/usr/bin/env bash
set -euo pipefail
updater=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
case "${1:---install}" in
    --uninstall)
        systemctl --user disable --now agent-skills-updater.timer
        for unit in agent-skills-updater.service agent-skills-updater.timer; do
            if [[ -f "$unit_dir/$unit" ]] && [[ $(head -n 1 "$unit_dir/$unit") == '# Managed by agent-skills-updater' ]]; then
                rm -- "$unit_dir/$unit"
            fi
        done
        systemctl --user daemon-reload
        exit 0 ;;
    --install) ;;
    *) echo 'Usage: bash server/install.sh [--install|--uninstall]' >&2; exit 2 ;;
esac
mkdir -p -- "$unit_dir"
for unit in agent-skills-updater.service agent-skills-updater.timer; do
    if [[ -e "$unit_dir/$unit" ]] && [[ $(head -n 1 "$unit_dir/$unit") != '# Managed by agent-skills-updater' ]]; then
        echo "Existing unmanaged unit preserved: $unit" >&2; exit 1
    fi
done
path=$updater
path=${path//\\/\\\\}; path=${path//\"/\\\"}
path=${path//%/%%}; path=${path//\$/\$\$}
cat >"$unit_dir/agent-skills-updater.service" <<EOF
# Managed by agent-skills-updater
[Unit]
Description=Maintain skill catalogs and distribute native client updates
[Service]
Type=oneshot
ExecStart=/usr/bin/bash "$path/server/run.sh"
TimeoutStartSec=15min
EOF
cat >"$unit_dir/agent-skills-updater.timer" <<'EOF'
# Managed by agent-skills-updater
[Unit]
Description=Maintain skill catalogs daily
[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=10min
[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now agent-skills-updater.timer
