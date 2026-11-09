# Library location and format for the engine's entry points (sourced).
# The engine may live inside the library (legacy layout) or apart from it
# (installed Skillarium). Order: the --library argument (a folder or the name of
# a connected library), SKILLARIUM_LIBRARY, the engine folder itself when it is
# a library, then the first of the libraries in the user's Skillarium
# configuration ("libraries", or the legacy single "library").

# Highest library format (agent-skills.json "format", absent = 1) this engine
# understands. A newer library is used read-only until Skillarium is updated.
supported_format=1

skillarium_config() {
    printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/skillarium/config.json"
}

is_library() {
    [[ -f "$1/sources.json" && -f "$1/agent-skills.json" ]]
}

# Raw entries of the configuration, NUL-separated name, path and url per library:
# "libraries" in priority order, or the legacy single "library" (empty name and url).
config_entries() {
    local config
    config=$(skillarium_config)
    [[ -f "$config" ]] || return 0
    if command -v jq >/dev/null; then
        jq -j 'if .libraries != null then [.libraries[]? | objects | select((.path | type) == "string" and .path != "")
                | [(.name // "" | tostring), .path, (.url // "" | tostring)]]
            elif (.library | type) == "string" and .library != "" then [["", .library, ""]] else [] end
            | .[] | .[] | ., "\u0000"' -- "$config"
    else
        python3 -c 'import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8-sig"))
items = []
if data.get("libraries") is not None:
    for item in data["libraries"] or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"]:
            items.append([str(item.get("name") or ""), item["path"], str(item.get("url") or "")])
elif isinstance(data.get("library"), str) and data["library"]:
    items.append(["", data["library"], ""])
sys.stdout.write("".join(value + "\0" for item in items for value in item))' "$config"
    fi
}

# Fails, with a message, when the configuration exists but cannot be read: an unreadable
# list must not look like "no library connected".
check_config() {
    local config
    config=$(skillarium_config)
    [[ -f "$config" ]] || return 0
    if command -v jq >/dev/null; then
        sed '1s/^\xEF\xBB\xBF//' -- "$config" | jq -e 'type == "object"' >/dev/null 2>&1 && return 0
    else
        python3 -c 'import json, sys
sys.exit(0 if isinstance(json.load(open(sys.argv[1], encoding="utf-8-sig")), dict) else 1)' "$config" 2>/dev/null && return 0
    fi
    echo "Invalid Skillarium configuration: $config" >&2
    return 1
}

# True once a configuration names its libraries, even none: every library was disconnected.
libraries_configured() {
    local config
    config=$(skillarium_config)
    [[ -f "$config" ]] || return 1
    sed '1s/^\xEF\xBB\xBF//' -- "$config" |
        jq -e '(.libraries != null) or ((.library | type) == "string" and .library != "")' >/dev/null 2>&1
}

# Name for a library folder ($1) not yet in the list, given the taken names on stdin (one
# per line): its repository name, else the folder name, numbered when taken.
new_library_name() {
    local taken id name base number=2
    taken=$(cat)
    id=$(library_repo_id "$1")
    if [[ -n "$id" ]]; then name=${id#*/}; else name=${1%/}; name=${name##*/}; fi
    if grep -qxF -- "$name" <<<"$taken"; then name=${1%/}; name=${name##*/}; fi
    base=$name
    while grep -qxF -- "$name" <<<"$taken"; do name="$base-$number"; number=$((number + 1)); done
    printf '%s\n' "$name"
}

# owner/name of a library URL: the same for every colleague, unlike the local name.
# A local remote (a folder) counts too: its last two parts.
repo_id_of_url() {
    local text=$1
    text=${text//\\//}
    text=${text#"${text%%[![:space:]]*}"}; text=${text%"${text##*[![:space:]]}"}
    while [[ "$text" == */ ]]; do text=${text%/}; done
    text=${text%.git}
    [[ "$text" =~ [/:]([^/:]+)/([^/:]+)$ ]] || return 0
    printf '%s/%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
}

library_repo_id() {
    local url
    url=$(git -C "$1" config --get remote.origin.url 2>/dev/null) || return 0
    [[ -n "$url" ]] || return 0
    repo_id_of_url "$url"
}

# Connected libraries in priority order, NUL-separated name, path and url per library;
# a missing name is the repository name of the clone's origin, else the folder name.
configured_rows() {
    local name path url id
    while IFS= read -r -d '' name && IFS= read -r -d '' path && IFS= read -r -d '' url; do
        if [[ -z "$name" ]]; then
            id=$(library_repo_id "$path")
            if [[ -n "$id" ]]; then name=${id#*/}; else name=${path%/}; name=${name##*/}; fi
        fi
        printf '%s\0%s\0%s\0' "$name" "$path" "$url"
    done < <(config_entries)
}

# The same as a JSON array of {name, path, url}.
configured_libraries() {
    configured_rows | jq -Rsc 'split("\u0000") | [range(0; (length / 3 | floor)) as $i
        | {name: .[$i * 3], path: .[$i * 3 + 1], url: .[$i * 3 + 2]}]'
}

# Path of the connected library with this name; fails when there is none.
find_configured_library() {
    local name path url
    while IFS= read -r -d '' name && IFS= read -r -d '' path && IFS= read -r -d '' url; do
        if [[ "$name" == "$1" ]]; then printf '%s\n' "$path"; return 0; fi
    done < <(configured_rows)
    return 1
}

# Skill name -> library name chosen on this machine over the order of the list (JSON object).
preferred_libraries() {
    local config
    config=$(skillarium_config)
    [[ -f "$config" ]] || { echo '{}'; return 0; }
    jq -c '.prefer // {} | if type == "object" then with_entries(.value |= tostring) else {} end' -- "$config"
}

# Writes the configuration: $1 the libraries (JSON array of {name, path, url}), $2 the
# preferred copies (JSON object; omitted: kept as is). "library" keeps the first library
# for older engines and apps; other fields stay.
write_skillarium_config() {
    local config current temporary
    config=$(skillarium_config)
    mkdir -p -- "${config%/*}" || return 1
    if [[ -f "$config" ]]; then current=$(cat -- "$config") || return 1; else current='{}'; fi
    temporary="$config.$$.new"
    if jq --argjson libraries "$1" --argjson prefer "${2:-null}" '
        .libraries = [$libraries[] | {name: (.name | tostring), path: (.path | tostring), url: (.url // "" | tostring)}]
        | if (.libraries | length) > 0 then .library = .libraries[0].path else del(.library) end
        | if $prefer == null then .
          elif ($prefer | length) > 0 then .prefer = ($prefer | to_entries | sort_by(.key) | from_entries)
          else del(.prefer) end' <<<"$current" >"$temporary" && mv -f -- "$temporary" "$config"; then
        return 0
    fi
    rm -f -- "$temporary"; return 1
}

# Sets repo to the library; $1 is the explicit --library value (a folder or the name of a
# connected library) or empty.
resolve_library() {
    local candidate=$1 named
    if [[ -z "$candidate" ]]; then candidate=${SKILLARIUM_LIBRARY:-}; fi
    # A connected library may also be given by its name.
    if [[ -n "$candidate" && "$candidate" != *[\\/:]* ]] && named=$(find_configured_library "$candidate" 2>/dev/null); then
        candidate=$named
    fi
    if [[ -z "$candidate" ]] && is_library "$engine"; then candidate=$engine; fi
    if [[ -z "$candidate" ]]; then
        # The first connected library that is there; an unavailable one (a disconnected
        # drive) must not block the others. Entries are name, path, url.
        local name path url first=''
        while IFS= read -r -d '' name && IFS= read -r -d '' path && IFS= read -r -d '' url; do
            [[ -n "$first" ]] || first=$path
            if is_library "$path"; then candidate=$path; break; fi
        done < <(config_entries)
        [[ -n "$candidate" ]] || candidate=$first
    fi
    if [[ -z "$candidate" ]]; then
        echo "Skill library not found: pass --library <folder> or connect one with skillarium client ($(skillarium_config))" >&2
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
    # Same rule as the PowerShell client and the server: absent or null is 1, else a whole number >= 1.
    format=$(jq -r '.format | if . == null then 1 elif type == "number" and . >= 1 and . == floor then . else "invalid: \(tojson)" end' \
        <<< "$text") || return 1
    [[ "$format" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid library format: ${format#invalid: }" >&2; return 1; }
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

# Merges, commits and publications need the working tree, the shared branch
# and any given revision (a proposal) in a format this engine understands.
assert_writable() {
    local revision
    assert_format || return 1
    if git -C "$repo" rev-parse --verify -q '@{upstream}' >/dev/null 2>&1; then
        assert_format '@{upstream}' || return 1
    fi
    for revision in "$@"; do assert_format "$revision" || return 1; done
}

# A value for a systemd unit line: quoted, with \, ", % and $ escaped.
unit_quote() {
    local value=$1
    value=${value//\\/\\\\}; value=${value//\"/\\\"}
    value=${value//%/%%}; value=${value//\$/\$\$}
    printf '"%s"' "$value"
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
