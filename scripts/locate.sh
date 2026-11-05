# Library location and format for the engine's entry points (sourced).
# The engine may live inside the library (legacy layout) or apart from it
# (installed Skillarium). Order: the --library argument, SKILLARIUM_LIBRARY,
# the engine folder itself when it is a library, then the "library" field of
# the user's Skillarium configuration.

# Highest library format (agent-skills.json "format", absent = 1) this engine
# understands. A newer library is used read-only until Skillarium is updated.
supported_format=1

skillarium_config() {
    printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/skillarium/config.json"
}

is_library() {
    [[ -f "$1/sources.json" && -f "$1/agent-skills.json" ]]
}

configured_library() {
    local config
    config=$(skillarium_config)
    [[ -f "$config" ]] || return 0
    if command -v jq >/dev/null; then
        jq -r '.library // empty' -- "$config"
    else
        python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("library") or "")' "$config"
    fi
}

# Sets repo to the library; $1 is the explicit --library value or empty.
resolve_library() {
    local candidate=$1
    if [[ -z "$candidate" ]]; then candidate=${SKILLARIUM_LIBRARY:-}; fi
    if [[ -z "$candidate" ]] && is_library "$engine"; then candidate=$engine; fi
    if [[ -z "$candidate" ]]; then candidate=$(configured_library) || return 1; fi
    if [[ -z "$candidate" ]]; then
        echo "Skill library not found: pass --library <folder> or set \"library\" in $(skillarium_config)" >&2
        return 1
    fi
    if ! repo=$(cd -- "$candidate" 2>/dev/null && pwd -P) || ! is_library "$repo"; then
        echo "Not a skill library: $candidate" >&2; return 1
    fi
}

# Library format of agent-skills.json at a revision (default: the working tree).
library_format() {
    local text format
    if [[ -n "${1:-}" ]]; then
        text=$(git -C "$repo" show "$1:agent-skills.json" 2>/dev/null) || { echo 1; return 0; }
    else
        text=$(cat -- "$repo/agent-skills.json") || return 1
    fi
    format=$(jq -r '.format // 1' <<< "$text") || return 1
    [[ "$format" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid library format: $format" >&2; return 1; }
    printf '%s\n' "$format"
}

# Fails with a clear message when the library needs a newer Skillarium.
assert_format() {
    local format
    format=$(library_format "${1:-}") || return 1
    if ((format > supported_format)); then
        echo "READ-ONLY: library format $format needs a newer Skillarium (this one supports $supported_format); skills keep working, edits stay local until you update." >&2
        return 1
    fi
}

# Splits --library <folder> / --library=<folder> out of the arguments;
# sets library_arg and the remaining arguments in rest (array).
split_library_arg() {
    library_arg=''
    rest=()
    while (($#)); do
        case "$1" in
            --library) (($# >= 2)) || { echo 'Missing folder after --library' >&2; return 2; }; library_arg=$2; shift 2 ;;
            --library=*) library_arg=${1#--library=}; shift ;;
            *) rest+=("$1"); shift ;;
        esac
    done
}
