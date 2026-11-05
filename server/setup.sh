#!/usr/bin/env bash
# `skillarium server ...`: check what the library server needs on this Linux
# machine, then run server/setup.py (repository, Gitea settings, timers).
set -euo pipefail
engine=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

for argument in "$@"; do
    if [[ "$argument" == --help || "$argument" == -h ]]; then exec python3 "$engine/server/setup.py" --help; fi
done
no_timers=false
for argument in "$@"; do [[ "$argument" == --no-timers ]] && no_timers=true; done

install_hint() {
    if command -v apt-get >/dev/null; then echo "sudo apt-get install -y $*"
    elif command -v dnf >/dev/null; then echo "sudo dnf install -y $*"
    elif command -v pacman >/dev/null; then echo "sudo pacman -S --needed $*"
    else echo "install: $*"; fi
}

missing=()
for tool in git python3 jq curl flock; do
    command -v "$tool" >/dev/null || missing+=("$tool")
done
if ((${#missing[@]})); then
    packages=${missing[*]}
    packages=${packages//flock/util-linux}
    echo "Missing: ${missing[*]}. Install them first:" >&2
    echo "  $(install_hint "$packages")" >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
    echo 'Python 3.9 or newer is required.' >&2; exit 1
fi
if ! $no_timers; then
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        echo 'systemd --user is not available in this session; log in as this user (not su) or pass --no-timers.' >&2
        exit 1
    fi
    if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || true)" != yes ]]; then
        echo 'Timers of this user must run without a login session. Enable it once:' >&2
        echo "  sudo loginctl enable-linger $USER" >&2
        exit 1
    fi
fi
exec python3 "$engine/server/setup.py" "$@"
