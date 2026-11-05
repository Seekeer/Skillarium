#!/usr/bin/env bash
# Local project bindings of library skills; the backend of the "Projects" UI.
set -euo pipefail
engine=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source "$engine/scripts/locate.sh"
usage() {
    cat <<'EOF'
Usage: bash projects.sh [--library <folder>] <command> ...
       bash projects.sh status
       bash projects.sh register <folder> | unregister <folder>
       bash projects.sh bind <folder> <skill>
       bash projects.sh unbind <folder> <skill> [global|library]
       bash projects.sh global <skill> on|off
       bash projects.sh conflicts | resolve <request.json>
Exit codes: 3 the last binding needs a global-access choice (JSON on stdout);
            4 the binding is saved but not applied (see status);
            5 the synchronization conflict changed; nothing was merged.
EOF
}
split_library_arg "$@" || exit 2
set -- ${rest[@]+"${rest[@]}"}
command=${1:-}
case "$command:$#" in
    status:1|conflicts:1|resolve:2|register:2|unregister:2|global:3|bind:3|unbind:3|unbind:4) shift ;;
    help:1|--help:1|-h:1) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

resolve_library "$library_arg" || exit 1
source "$engine/scripts/client.sh"
source "$engine/scripts/bindings.sh"
if [[ "$command" != status && "$command" != conflicts ]]; then
    # The background client uses the same lock; wait for it instead of skipping.
    lock_path=$(git -C "$repo" rev-parse --git-path agent-skills-sync.lock)
    [[ "$lock_path" = /* ]] || lock_path="$repo/$lock_path"
    exec 9>"$lock_path"
    if ! flock -w "${AGENT_SKILLS_LOCK_TIMEOUT:-300}" 9; then
        echo 'Synchronization is still running; nothing changed. Retry later.' >&2; exit 1
    fi
fi
load_bindings || exit 1
result=0
"cmd_$command" "$@" || result=$?
exit "$result"
