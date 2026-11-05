#!/usr/bin/env bash
# Install the daily server timer of the unified library on the assigned machine.
# A managed timer of the former separate agent-skills-updater is switched off
# first, so old and new maintenance never run side by side.
set -euo pipefail
engine=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$engine/scripts/locate.sh"
split_library_arg "$@" || exit 2
set -- ${rest[@]+"${rest[@]}"}
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
marker='# Managed by agent-skills-server'
legacy_marker='# Managed by agent-skills-updater'
mode=${1:---install}
machine=${2:-}
case "$mode" in
    --install|--uninstall) ;;
    *) echo 'Usage: bash server/install.sh [--library <folder>] [--install [<machine-name>]|--uninstall]' >&2; exit 2 ;;
esac

remove_managed() {
    local expected=$1 unit
    shift
    for unit in "$@"; do
        if [[ -f "$unit_dir/$unit" ]] && [[ $(head -n 1 "$unit_dir/$unit") == "$expected" ]]; then
            rm -- "$unit_dir/$unit"; echo "Removed $unit"
        fi
    done
}

if [[ "$mode" == --uninstall ]]; then
    systemctl --user disable --now agent-skills-server.timer || true
    remove_managed "$marker" agent-skills-server.service agent-skills-server.timer
    systemctl --user daemon-reload
    exit 0
fi
resolve_library "$library_arg" || exit 2
library=$repo

for unit in agent-skills-server.service agent-skills-server.timer; do
    if [[ -e "$unit_dir/$unit" ]] && [[ $(head -n 1 "$unit_dir/$unit") != "$marker" ]]; then
        echo "Existing unmanaged unit preserved: $unit" >&2; exit 1
    fi
done
for unit in agent-skills-updater.service agent-skills-updater.timer; do
    if [[ -e "$unit_dir/$unit" ]] && [[ $(head -n 1 "$unit_dir/$unit") != "$legacy_marker" ]]; then
        echo "Unrecognized legacy unit preserved; switch it off manually: $unit" >&2; exit 1
    fi
done
if [[ -n "$machine" ]]; then
    git -C "$library" config agent-skills.machine "$machine"
fi
if [[ -f "$unit_dir/agent-skills-updater.timer" ]]; then
    # The separate updater checkout itself, with any local work, stays untouched.
    systemctl --user disable --now agent-skills-updater.timer || true
    systemctl --user stop agent-skills-updater.service || true
    remove_managed "$legacy_marker" agent-skills-updater.service agent-skills-updater.timer
fi
unit_quote() {
    local value=$1
    value=${value//\\/\\\\}; value=${value//\"/\\\"}
    value=${value//%/%%}; value=${value//\$/\$\$}
    printf '"%s"' "$value"
}
# The engine runs the library given here; the legacy layout keeps its old line.
exec_start="/usr/bin/bash $(unit_quote "$engine/server/run.sh")"
if [[ "$engine" != "$library" ]]; then exec_start+=" --repo $(unit_quote "$library")"; fi
mkdir -p -- "$unit_dir"
cat >"$unit_dir/agent-skills-server.service" <<EOF
$marker
[Unit]
Description=Maintain the shared skill library on the assigned server
[Service]
Type=oneshot
ExecStart=$exec_start
TimeoutStartSec=15min
EOF
cat >"$unit_dir/agent-skills-server.timer" <<EOF
$marker
[Unit]
Description=Maintain the shared skill library daily
[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=10min
[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now agent-skills-server.timer
echo "Installed server timer for machine $(git -C "$library" config --get agent-skills.machine || hostname -s)."
echo "The assigned server is set in agent-skills.json; see: python3 $engine/server/manage.py --repo $library status"
