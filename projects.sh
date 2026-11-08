#!/usr/bin/env bash
# Local project bindings of library skills; the backend of the "Projects" UI.
set -euo pipefail
engine=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$engine/scripts/locate.sh"
usage() {
    cat <<'EOF'
Usage: bash projects.sh [--library <folder|name>] <command> ...
       bash projects.sh status
       bash projects.sh register <folder> | unregister <folder>
       bash projects.sh bind <folder> <skill> [<library>]
       bash projects.sh unbind <folder> <skill> [global|library]
       bash projects.sh global <skill> on|off
       bash projects.sh conflicts | resolve <request.json>
       bash projects.sh policy <skill> auto|confirm
       bash projects.sh libraries | order <library>... | prefer <skill> <library>|auto
       bash projects.sh disconnect <library> [--force]
--library picks the library of policy, conflicts and resolve (a folder or a connected
library's name); links and bindings always cover every connected library.
Exit codes: 3 the last binding needs a global-access choice (JSON on stdout);
            4 the binding is saved but not applied (see status);
            5 the synchronization conflict changed; nothing was merged.
EOF
}
split_library_arg "$@" || exit 2
set -- ${rest[@]+"${rest[@]}"}
command=${1:-}
case "$command:$#" in
    status:1|conflicts:1|resolve:2|register:2|unregister:2|global:3|bind:3|bind:4|unbind:3|unbind:4|policy:3) shift ;;
    libraries:1|prefer:3|disconnect:2|disconnect:3) shift ;;
    order:*) (($# >= 2 && $# <= 21)) || { usage >&2; exit 2; }; shift ;;
    help:1|--help:1|-h:1) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

check_config || exit 1
# After the last library was disconnected, listings show nothing instead of failing.
if [[ ("$command" == status || "$command" == libraries) && -z "$library_arg" && -z "${SKILLARIUM_LIBRARY:-}" ]] &&
    ! is_library "$engine" && [[ $(configured_libraries | jq length) -eq 0 ]] && libraries_configured; then
    repo=''
else
    resolve_library "$library_arg" || exit 1
fi
command_library=''
if [[ -n "$library_arg" || -n "${SKILLARIUM_LIBRARY:-}" ]] || is_library "$engine"; then command_library=$repo; fi
source "$engine/scripts/client.sh"
source "$engine/scripts/bindings.sh"
if [[ "$command" == resolve || "$command" == policy ]]; then
    # The background client uses the same lock; wait for it instead of skipping.
    lock_path=$(git -C "$repo" rev-parse --git-path agent-skills-sync.lock)
    [[ "$lock_path" = /* ]] || lock_path="$repo/$lock_path"
    exec 9>"$lock_path"
    if ! flock -w "${AGENT_SKILLS_LOCK_TIMEOUT:-300}" 9; then
        echo 'Synchronization is still running; nothing changed. Retry later.' >&2; exit 1
    fi
fi
# Links and bindings span all libraries; their own lock comes after the library's.
case "$command" in status|conflicts|libraries) ;; *) enter_bindings_lock || exit 1 ;; esac
load_bindings || exit 1
cmd_policy() { set_update_policy "$1" "$2"; }
result=0
"cmd_$command" "$@" || result=$?
exit "$result"
