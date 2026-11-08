# Local skill availability: global links and project bindings of every connected
# library. Project lists live in each project's Git; registered paths, global
# choices and link ownership stay in local state that is never published. One
# state covers all libraries; a skill name in several of them is linked from the
# first library in the configured order unless another one is preferred for it.
bindings_file="${XDG_STATE_HOME:-$HOME/.local/state}/agent-skills/bindings.json"
project_list=.agent-skills.json
agent_folders=(.agents/skills .claude/skills)
# A project entry: "name", or "owner/repo:name" for the copy of one library.
entry_pattern='^([A-Za-z0-9._-]+/[A-Za-z0-9._-]+:)?[a-z0-9]+(-[a-z0-9]+)*$'

# The catalog: every library whose skills reach this machine, in priority order (index i).
cat_names=() cat_paths=() cat_urls=() cat_ids=() cat_available=() cat_configured=() cat_skills=()
# Skill name -> space-separated indices of the libraries having it, the winning index,
# and the library name preferred on this machine.
declare -gA cat_copies=() cat_winner=() cat_preferred=()

# Fills the catalog: the connected libraries and, when it is not one of them, the library
# this command was given ($1, default $repo; last). A name present in several libraries
# goes to the first of them in the configured order, unless a library is preferred for it.
init_catalog() {
    local current=${1-${command_library-$repo}} name path url id origin physical available i skill chosen index skills
    cat_names=() cat_paths=() cat_urls=() cat_ids=() cat_available=() cat_configured=() cat_skills=()
    cat_copies=() cat_winner=() cat_preferred=()
    while IFS= read -r -d '' name && IFS= read -r -d '' path && IFS= read -r -d '' url; do
        id=$(repo_id_of_url "$url")
        available=false
        if is_library "$path" && physical=$(cd -- "$path" 2>/dev/null && pwd -P); then
            available=true; path=$physical
            origin=$(library_repo_id "$path")
            [[ -z "$origin" ]] || id=$origin
        fi
        cat_names+=("$name"); cat_paths+=("$path"); cat_urls+=("$url"); cat_ids+=("$id")
        cat_available+=("$available"); cat_configured+=(true)
    done < <(configured_rows)
    if [[ -n "$current" ]]; then
        for path in ${cat_paths[@]+"${cat_paths[@]}"}; do
            if [[ "$path" == "$current" ]]; then current=''; break; fi
        done
    fi
    if [[ -n "$current" ]]; then
        id=$(library_repo_id "$current")
        if [[ -n "$id" ]]; then name=${id#*/}; else name=${current##*/}; fi
        for i in "${!cat_names[@]}"; do
            if [[ "${cat_names[i]}" == "$name" ]]; then name=${current##*/}; break; fi
        done
        cat_names+=("$name"); cat_paths+=("$current"); cat_urls+=(''); cat_ids+=("$id")
        cat_available+=(true); cat_configured+=(false)
    fi
    while IFS= read -r -d '' skill && IFS= read -r -d '' name; do
        cat_preferred[$skill]=$name
    done < <(preferred_libraries | jq -j 'to_entries[] | select(.key != "") | .key, "\u0000", .value, "\u0000"')
    for i in "${!cat_names[@]}"; do
        cat_skills[i]=''
        [[ "${cat_available[i]}" == true ]] || continue
        if ! skills=$(repo=${cat_paths[i]}; library_names); then
            # An unreadable registry must not unlink the library's skills: treat it as offline.
            echo "WARNING: ${cat_names[i]}: invalid skill registry in ${cat_paths[i]}/sources.json; its skills are skipped." >&2
            cat_available[i]=false
            continue
        fi
        cat_skills[i]=$skills
        while IFS= read -r skill; do
            [[ -n "$skill" ]] || continue
            cat_copies[$skill]="${cat_copies[$skill]:+${cat_copies[$skill]} }$i"
        done <<<"$skills"
    done
    for skill in "${!cat_copies[@]}"; do
        chosen=${cat_copies[$skill]%% *}
        if [[ -n "${cat_preferred[$skill]+present}" ]]; then
            for index in ${cat_copies[$skill]}; do
                if [[ "${cat_names[index]}" == "${cat_preferred[$skill]}" ]]; then chosen=$index; break; fi
            done
        fi
        cat_winner[$skill]=$chosen
    done
}

# Index of the catalog library with this name; fails when there is none.
find_catalog_library() {
    local i
    for i in "${!cat_names[@]}"; do
        if [[ "${cat_names[i]}" == "$1" ]]; then printf '%s\n' "$i"; return 0; fi
    done
    return 1
}

# Index of the library whose skills folder holds a link target; fails outside every library.
find_target_library() {
    local i
    [[ -n "$1" ]] || return 1
    for i in "${!cat_paths[@]}"; do
        if [[ "$1" == "${cat_paths[i]}/skills/"* ]]; then printf '%s\n' "$i"; return 0; fi
    done
    return 1
}

# Winning library index of a skill name; empty when no library has it.
winner_of() {
    [[ -n "$1" ]] || return 0
    printf '%s\n' "${cat_winner[$1]-}"
}

# A project entry: "name" (the copy linked globally) or "owner/repo:name" (that library's
# copy). Sets r_name, r_lib (catalog index, empty when not connected) and r_id.
resolve_entry() {
    local i
    r_lib='' r_id=''
    if [[ "$1" =~ ^(.+):([a-z0-9]+(-[a-z0-9]+)*)$ ]]; then
        r_id=${BASH_REMATCH[1]} r_name=${BASH_REMATCH[2]}
        for i in "${!cat_ids[@]}"; do
            if [[ -n "${cat_ids[i]}" && "${cat_ids[i],,}" == "${r_id,,}" ]]; then r_lib=$i; break; fi
        done
    else
        r_name=$1
        r_lib=$(winner_of "$1")
        if [[ -z "$r_lib" && -n "${state-}" ]]; then
            # A library that is only unavailable (a disconnected drive) has no winners, yet
            # the links recorded into it still say which copy the entry means.
            local target owner
            while IFS= read -r target; do
                if owner=$(find_target_library "$target") && [[ "${cat_available[owner]}" != true ]]; then
                    r_lib=$owner; break
                fi
            done < <(jq -r --arg s "$1" '.links[] | select(.skill == $s) | .target // empty' <<<"$state")
        fi
    fi
}

load_bindings() {
    local primary
    init_catalog
    # "library" names the first library for older engines; the state covers all of them.
    primary=${cat_paths[0]:-$repo}
    if [[ ! -e "$bindings_file" ]]; then
        state=$(jq -cn --arg library "$primary" \
            '{version: 1, library: $library, projects: {}, global: {}, links: {}, excludes: {}}') || return 1
        saved_state=''
        return 0
    fi
    state=$(jq -c 'select(.version == 1 and (.library | type) == "string" and
        ([.projects, .global, .links, .excludes] | all(type == "object")))' "$bindings_file" 2>/dev/null)
    [[ -n "$state" ]] || { echo "Invalid local bindings state preserved: $bindings_file" >&2; return 1; }
    saved_state=$state
    mutate --arg library "$primary" '.library = $library'
}

# Bindings span all libraries, so their changes wait for each other, not per library.
# The lock stays held until the process exits.
enter_bindings_lock() {
    local folder=${bindings_file%/*}
    mkdir -p -- "$folder" || return 1
    exec {bindings_lock_fd}>"$folder/bindings.lock" || return 1
    if ! flock -w "${AGENT_SKILLS_LOCK_TIMEOUT:-300}" "$bindings_lock_fd"; then
        echo 'Skill links are being updated by another run; nothing changed. Retry later.' >&2; return 1
    fi
}

# A path in the temporary folder that no longer exists: what a test run leaves behind.
is_disposable_library() {
    local temporary=${TMPDIR:-/tmp}
    temporary=${temporary%/}
    [[ -n "$1" && ! -e "$1" && ( "$1" == "$temporary"/* || "$1" == /tmp/* ) ]]
}

save_bindings() {
    [[ "$state" != "$saved_state" ]] || return 0
    local folder=${bindings_file%/*} temporary
    mkdir -p -- "$folder" || return 1
    temporary="$folder/.bindings.$$.tmp"
    if jq . <<<"$state" >"$temporary" && mv -f -- "$temporary" "$bindings_file"; then
        saved_state=$state; return 0
    fi
    rm -f -- "$temporary"; return 1
}

mutate() {
    local next
    next=$(jq -c "$@" <<<"$state") || return 1
    state=$next
}

global_enabled() {
    jq -e --arg s "$1" 'if .global[$s] then .global[$s].enabled else true end' <<<"$state" >/dev/null
}

global_json() {
    jq -c --arg s "$1" '.global[$s] // {enabled: true, manual: false, pending: null}' <<<"$state"
}

# A skill of a library (catalog index $2; default: the library winning the name).
library_skill() {
    local name=$1 index=${2-}
    [[ -n "$index" ]] || index=$(winner_of "$name")
    if [[ -z "$index" || "${cat_available[index]}" != true ]]; then
        echo "Skill is not in a connected library: $name" >&2; return 1
    fi
    (repo=${cat_paths[index]}; check_skill "$name")
}

# Folder of a skill in a library (catalog index $2; default: the library winning the name).
skill_target() {
    local name=$1 index=${2-} root=$repo
    [[ -n "$index" ]] || index=$(winner_of "$name")
    [[ -z "$index" ]] || root=${cat_paths[index]}
    printf '%s\n' "$root/skills/$name"
}

# Registered folders are compared by their physical path; missing ones lexically.
project_key() {
    local path=$1
    if [[ -d "$path" ]]; then (cd -- "$path" && pwd -P); return; fi
    [[ "$path" == /* ]] || path="$PWD/$path"
    while [[ "$path" == */ && "$path" != / ]]; do path=${path%/}; done
    printf '%s\n' "$path"
}

read_project_list() {
    local file="$1/$project_list"
    [[ -e "$file" || -L "$file" ]] || return 0
    # "name", or "owner/repo:name" for the copy of one library.
    jq -rs --arg pattern "$entry_pattern" 'if length == 1 then .[0] else null end
        | if type == "object" and .version == 1 and (keys - ["skills", "version"] == []) and
              (.skills | type) == "array" and
              all(.skills[]; type == "string" and test($pattern) and (split(":") | last | length) <= 64)
           then .skills | unique[] else error("invalid") end' "$file" 2>/dev/null || {
        echo "Invalid project skill list preserved: $file" >&2; return 1; }
}

# Current list of an available project, or the last one seen while it was available.
project_skills() {
    if [[ -d "$1" ]] && read_project_list "$1" 2>/dev/null; then return 0; fi
    jq -r --arg p "$1" '.projects[$p].skills // [] | .[]' <<<"$state"
}

write_project_list() {
    local project=$1 file="$1/$project_list" temporary content; shift
    content=$(printf '%s\n' "$@" | jq -R . | jq -s '{version: 1, skills: (map(select(length > 0)) | unique)}') || return 1
    if [[ -f "$file" && ! -L "$file" && "$(<"$file")" == "$content" ]]; then return 0; fi
    temporary="$project/.agent-skills.json.$$.tmp"
    # Only the working tree changes: staging and commits stay with the project workflow.
    if printf '%s\n' "$content" >"$temporary" && mv -f -- "$temporary" "$file"; then return 0; fi
    rm -f -- "$temporary"; return 1
}

# Prints the exclude file and path prefix of a Git work tree; nothing otherwise.
exclude_location() {
    local file prefix
    git -C "$1" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
    file=$(git -C "$1" rev-parse --path-format=absolute --git-path info/exclude) || return 1
    prefix=$(git -C "$1" rev-parse --show-prefix) || return 1
    printf '%s\n%s\n' "$file" "$(sed 's/[][*?\\]/\\&/g' <<<"$prefix")"
}

add_exclude() {
    local file=$1 line=$2
    [[ -n "$file" ]] || return 0
    if [[ -f "$file" ]] && grep -qxF -- "$line" "$file"; then return 0; fi
    mkdir -p -- "${file%/*}" || return 1
    if [[ -s "$file" && -n $(tail -c 1 -- "$file") ]]; then printf '\n' >>"$file" || return 1; fi
    printf '%s\n' "$line" >>"$file" || return 1
    mutate --arg f "$file" --arg l "$line" '.excludes[$f] = ((.excludes[$f] // []) + [$l] | unique)'
}

# Remove only exclude lines this client added and no recorded link still needs.
prune_excludes() {
    local file line temporary errors=0
    while IFS=$'\t' read -r file line; do
        [[ -n "$file" ]] || continue
        if [[ -f "$file" ]]; then
            temporary="$file.agent-skills.$$"
            if ! line=$line awk '!done && $0 == ENVIRON["line"] { done = 1; next } { print }' "$file" >"$temporary" ||
                ! mv -f -- "$temporary" "$file"; then
                rm -f -- "$temporary"; errors=1; continue
            fi
        fi
        mutate --arg f "$file" --arg l "$line" \
            '.excludes[$f] -= [$l] | if .excludes[$f] == [] then del(.excludes[$f]) else . end' || return 1
    done < <(jq -r '[.links[] | .exclude | select(. != null)] as $used
        | .excludes | to_entries[] | .key as $f | .value[] as $l
        | select([$used[] | select(.file == $f and .line == $l)] == [])
        | "\($f)\t\($l)"' <<<"$state")
    return "$errors"
}

record_link() {
    mutate --arg link "$1" --arg target "$2" --arg skill "$3" --arg project "$4" --arg file "${5:-}" --arg line "${6:-}" \
        '.links[$link] = {target: $target, skill: $skill,
            project: (if $project == "" then null else $project end),
            exclude: (if $file == "" then null else {file: $file, line: $line} end)}'
}

forget_link() {
    mutate --arg link "$1" 'del(.links[$link])'
}

# Removes a link only while it still points where this client created it.
remove_owned_link() {
    local link=$1 recorded
    recorded=$(jq -r --arg l "$link" '.links[$l].target // empty' <<<"$state")
    [[ -n "$recorded" ]] || return 0
    if [[ -L "$link" && $(readlink -- "$link") == "$recorded" ]]; then
        rm -- "$link" || return 1
        echo "UNLINKED: $link"
    elif [[ -e "$link" || -L "$link" ]]; then
        echo "Changed link preserved: $link" >&2
    fi
    forget_link "$link"
}

# Links a project to a skill of one library; $4 is the entry recorded for the links.
link_project_skill() {
    local project=$1 name=$2 entry=$4 target folder link line errors=0
    target=$(skill_target "$name" "$3")
    for folder in "${agent_folders[@]}"; do
        link="$project/$folder/$name"
        line="/$exclude_prefix$folder/$name"
        if [[ -L "$link" && "$link" -ef "$target" ]]; then
            add_exclude "$exclude_file" "$line" || { errors=1; continue; }
        elif [[ -e "$link" || -L "$link" ]]; then
            echo "CONFLICT (kept): $link" >&2; errors=1; continue
        else
            # Exclude first so Git never reports the machine-specific link.
            add_exclude "$exclude_file" "$line" || { errors=1; continue; }
            if ! mkdir -p -- "$project/$folder" || ! ln -sT -- "$target" "$link"; then
                echo "Link failed: $link" >&2; errors=1; continue
            fi
            echo "LINKED: $link"
        fi
        record_link "$link" "$(readlink -- "$link")" "$entry" "$project" "$exclude_file" "$line" || return 1
    done
    return "$errors"
}

reconcile_project() {
    local project=$1 names entry link skill target keep i errors=0
    local -a item_entries=() item_names=() item_libraries=()
    local -A taken=()
    if [[ ! -d "$project" ]]; then
        echo "UNAVAILABLE (kept): $project" >&2
        mutate --arg p "$project" '.projects[$p].available = false'; return
    fi
    if ! names=$(read_project_list "$project"); then
        mutate --arg p "$project" '.projects[$p].available = true'; return 1
    fi
    location=$(exclude_location "$project") || return 1
    exclude_file=$(sed -n 1p <<<"$location"); exclude_prefix=$(sed -n 2p <<<"$location")
    mutate --arg p "$project" --arg n "$names" \
        '.projects[$p] = {available: true, skills: ($n | split("\n") | map(select(length > 0)))}' || return 1
    while IFS= read -r entry; do
        [[ -n "$entry" ]] || continue
        resolve_entry "$entry"
        if [[ -n "${taken[$r_name]+present}" ]]; then
            echo "DUPLICATE (not applied): $entry in $project; ${taken[$r_name]} is bound already" >&2; errors=1; continue
        fi
        taken[$r_name]=$entry
        item_entries+=("$entry"); item_names+=("$r_name"); item_libraries+=("$r_lib")
    done <<<"$names"
    # A link stays while its entry still resolves to the same copy; an offline library keeps it.
    while IFS= read -r -d '' link && IFS= read -r -d '' skill && IFS= read -r -d '' target; do
        keep=0
        for i in "${!item_entries[@]}"; do
            [[ "$skill" == "${item_entries[i]}" && -n "${item_libraries[i]}" ]] || continue
            if [[ "${cat_available[${item_libraries[i]}]}" != true ]]; then keep=1; continue; fi
            if grep -Fxq -- "${item_names[i]}" <<<"${cat_skills[${item_libraries[i]}]}" &&
               [[ "$target" == "$(skill_target "${item_names[i]}" "${item_libraries[i]}")" ]]; then keep=1; fi
        done
        ((keep)) || remove_owned_link "$link" || errors=1
    done < <(jq -j --arg p "$project" '.links | to_entries[] | select(.value.project == $p)
        | .key, "\u0000", (.value.skill // "" | tostring), "\u0000", (.value.target // "" | tostring), "\u0000"' <<<"$state")
    for i in "${!item_entries[@]}"; do
        entry=${item_entries[i]}
        if [[ -z "${item_libraries[i]}" ]]; then
            echo "LIBRARY NOT CONNECTED (not applied): $entry in $project" >&2; errors=1; continue
        fi
        if [[ "${cat_available[${item_libraries[i]}]}" != true ]]; then
            echo "LIBRARY UNAVAILABLE (kept): $entry in $project" >&2; continue
        fi
        if ! library_skill "${item_names[i]}" "${item_libraries[i]}" 2>/dev/null; then
            echo "NOT IN LIBRARY (not applied): $entry in $project" >&2; errors=1; continue
        fi
        if link_project_skill "$project" "${item_names[i]}" "${item_libraries[i]}" "$entry"; then
            # The automatic global choice of a UI connection waits for its success.
            mutate --arg s "${item_names[i]}" --arg p "$project" \
                'if .global[$s].pending == $p then .global[$s] = {enabled: false, manual: false, pending: null} else . end' || return 1
        else
            errors=1
        fi
    done
    return "$errors"
}

# Links the winning copy of a skill into the global folders.
set_winner_links() {
    [[ -n "$2" ]] || { echo "Skill is not in a connected library: $1" >&2; return 1; }
    local repo=${cat_paths[$2]}
    link_version "$1"
}

set_global_links() {
    local name=$1 winner target folder link errors=0
    winner=$(winner_of "$name")
    target=$(skill_target "$name" "$winner")
    if global_enabled "$name"; then
        # A link someone else made to the same folder stays theirs: only links this run
        # created, or ones recorded before, become the client's own.
        local -A foreign=()
        for folder in "${agent_folders[@]}"; do
            link="$HOME/$folder/$name"
            if [[ -e "$link" || -L "$link" ]] && ! jq -e --arg l "$link" '.links | has($l)' <<<"$state" >/dev/null; then
                foreign[$link]=1
            fi
        done
        set_winner_links "$name" "$winner" || errors=1
        for folder in "${agent_folders[@]}"; do
            link="$HOME/$folder/$name"
            if [[ -z "${foreign[$link]+x}" && -L "$link" && "$link" -ef "$target" ]]; then
                record_link "$link" "$(readlink -- "$link")" "$name" '' || return 1
            fi
        done
        return "$errors"
    fi
    local recorded
    for folder in "${agent_folders[@]}"; do
        link="$HOME/$folder/$name"
        recorded=$(jq -r --arg l "$link" '.links[$l].target // empty' <<<"$state")
        if [[ -L "$link" && "$link" -ef "$target" && -n "$recorded" && $(readlink -- "$link") == "$recorded" ]]; then
            rm -- "$link" || { errors=1; continue; }
            echo "UNLINKED: $link"
        elif [[ -e "$link" || -L "$link" ]]; then
            echo "Foreign global install kept: $link" >&2
        fi
        forget_link "$link" || return 1
    done
    return "$errors"
}

# Global links whose copy no longer wins its name, of a skill the library dropped, or into
# a vanished temporary library. Links of an offline or foreign library are kept.
remove_stale_links() {
    local link skill target owner winner errors=0
    while IFS= read -r -d '' link && IFS= read -r -d '' skill && IFS= read -r -d '' target; do
        if owner=$(find_target_library "$target"); then
            [[ "${cat_available[owner]}" == true ]] || continue
            winner=$(winner_of "$skill")
            [[ -n "$winner" && "${cat_paths[winner]}" == "${cat_paths[owner]}" ]] && continue
        elif ! is_disposable_library "$target"; then
            continue
        fi
        remove_owned_link "$link" || errors=1
    done < <(jq -j '.links | to_entries[] | select(.value.project == null)
        | .key, "\u0000", (.value.skill // "" | tostring), "\u0000", (.value.target // "" | tostring), "\u0000"' <<<"$state")
    return "$errors"
}

# Brings links of every registered project and global folder to the saved choices.
apply_bindings() {
    local project name errors=0
    while IFS= read -r project; do
        [[ -n "$project" ]] || continue
        reconcile_project "$project" || errors=1
    done < <(jq -r '.projects | keys[]' <<<"$state")
    # A pending automatic choice is dropped together with the binding that requested it.
    mutate '.projects as $p | .global |= with_entries(.key as $k
        | if .value.pending != null and ([($p[.value.pending].skills // [])[] | split(":") | last | select(. == $k)] == [])
          then .value.pending = null else . end)' || errors=1
    remove_stale_links || errors=1
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        set_global_links "$name" || errors=1
    done < <(printf '%s\n' "${!cat_winner[@]}" | LC_ALL=C sort)
    prune_excludes || errors=1
    save_bindings || errors=1
    return "$errors"
}

# A UI connection hides a default global skill once the connection is applied.
request_auto_off() {
    mutate --arg s "$1" --arg p "$2" \
        'if (.global[$s].manual // false) or .global[$s].enabled == false or .global[$s].pending != null
         then . else .global[$s] = {enabled: true, manual: false, pending: $p} end'
}

set_global_choice() {
    mutate --arg s "$1" --argjson e "$2" '.global[$s] = {enabled: $e, manual: true, pending: null}'
}

link_state() {
    if [[ -L "$1" && "$1" -ef "$2" ]]; then echo linked
    elif [[ -e "$1" || -L "$1" ]]; then echo conflict
    else echo missing; fi
}

# The entry ($2) of a project ($1) is linked in every agent folder.
binding_applied() {
    local folder target
    resolve_entry "$2"
    [[ -n "$r_lib" ]] || return 1
    target=$(skill_target "$r_name" "$r_lib")
    for folder in "${agent_folders[@]}"; do
        [[ $(link_state "$1/$folder/$r_name" "$target") == linked ]] || return 1
    done
}

require_project() {
    project=$(project_key "$1") || return 1
    jq -e --arg p "$project" '.projects | has($p)' <<<"$state" >/dev/null || {
        echo "Project is not registered: $1" >&2; return 1; }
}

require_available() {
    [[ -d "$project" ]] || { echo "Project folder unavailable; registration kept: $project" >&2; return 1; }
}

cmd_register() {
    local entry names path
    [[ -d "$1" ]] || { echo "Project folder not found: $1" >&2; return 1; }
    project=$(project_key "$1") || return 1
    for path in ${cat_paths[@]+"${cat_paths[@]}"}; do
        if [[ "$project" == "$path" || "$project" == "$path"/* ]]; then
            echo 'The library itself cannot be a project.' >&2; return 1
        fi
    done
    names=$(read_project_list "$project") || return 1
    mutate --arg p "$project" '.projects[$p] //= {available: true, skills: []}' || return 1
    while IFS= read -r entry; do
        [[ -n "$entry" ]] || continue
        resolve_entry "$entry"
        request_auto_off "$r_name" "$project" || return 1
    done <<<"$names"
    save_bindings || return 1
    apply_bindings || echo 'Some bindings need attention; see status.' >&2
    while IFS= read -r entry; do
        [[ -n "$entry" ]] || continue
        binding_applied "$project" "$entry" || { echo "Binding not applied: $entry in $project" >&2; return 4; }
    done <<<"$names"
}

cmd_unregister() {
    local link
    require_project "$1" || return 1
    while IFS= read -r link; do
        [[ -n "$link" ]] || continue
        if [[ -d "$project" ]]; then remove_owned_link "$link" || return 1; else forget_link "$link" || return 1; fi
    done < <(jq -r --arg p "$project" '.links | to_entries[] | select(.value.project == $p) | .key' <<<"$state")
    mutate --arg p "$project" 'del(.projects[$p]) | .global |= map_values(if .pending == $p then .pending = null else . end)' || return 1
    save_bindings || return 1
    apply_bindings || true
}

# bind <folder> <skill> [<library>]: a named library writes "owner/repo:name", the same for
# every colleague; without it the project gets the copy linked globally.
cmd_bind() {
    local name=$2 library=${3:-} entry=$2 index names
    local -a kept=()
    require_project "$1" || return 1
    require_available || return 1
    if [[ -n "$library" ]]; then
        index=$(find_catalog_library "$library") || { echo "Library is not connected: $library" >&2; return 1; }
        [[ -n "${cat_ids[index]}" ]] || { echo "Library $library has no origin; a project cannot name it." >&2; return 1; }
        library_skill "$name" "$index" || { echo "Skill is not in library $library: $name" >&2; return 1; }
        entry="${cat_ids[index]}:$name"
    else
        library_skill "$name" || { echo "Skill is not in a connected library: $name" >&2; return 1; }
    fi
    names=$(read_project_list "$project") || return 1
    # Another copy of the same skill in this project is replaced.
    mapfile -t kept < <(while IFS= read -r item; do
        [[ -n "$item" && "${item##*:}" != "$name" ]] && printf '%s\n' "$item"
    done <<<"$names")
    request_auto_off "$name" "$project" && save_bindings || return 1
    write_project_list "$project" ${kept[@]+"${kept[@]}"} "$entry" || return 1
    apply_bindings || echo 'Some other bindings need attention; see status.' >&2
    binding_applied "$project" "$entry" || { echo "Binding saved but not applied: $entry in $project" >&2; return 4; }
}

cmd_unbind() {
    local name=$2 choice=${3:-} names skill other others=0 match folder
    local -a kept=()
    case "$choice" in ''|global|library) ;; *) echo "Unknown choice: $choice" >&2; return 2 ;; esac
    require_project "$1" || return 1
    require_available || return 1
    names=$(read_project_list "$project") || return 1
    # The plain skill name also removes its library-qualified entry.
    if ! grep -qxF -- "$name" <<<"$names"; then
        match=$(while IFS= read -r item; do
            [[ -n "$item" && "${item##*:}" == "$name" ]] && printf '%s\n' "$item"
        done <<<"$names")
        if [[ -n "$match" && "$match" != *$'\n'* ]]; then name=$match; fi
    fi
    resolve_entry "$name"
    skill=$r_name
    if grep -qxF -- "$name" <<<"$names"; then
        while IFS= read -r other; do
            [[ -n "$other" && "$other" != "$project" ]] || continue
            if project_skills "$other" | sed 's/.*://' | grep -qxF -- "$skill"; then others=1; break; fi
        done < <(jq -r '.projects | keys[]' <<<"$state")
        if ((others == 0)) && [[ -z "$choice" ]]; then
            # The UI asks; cancelling keeps the binding because nothing changed yet.
            jq -cn --arg s "$skill" --arg p "$project" --argjson g "$(global_json "$skill")" \
                '{decision: "last-project", skill: $s, project: $p, global: $g, choices: ["global", "library"]}'
            return 3
        fi
    fi
    if [[ -n "$choice" ]]; then
        set_global_choice "$skill" "$([[ "$choice" == global ]] && echo true || echo false)" && save_bindings || return 1
    fi
    mapfile -t kept < <(grep -vxF -- "$name" <<<"$names" || true)
    write_project_list "$project" ${kept[@]+"${kept[@]}"} || return 1
    apply_bindings || echo 'Some other bindings need attention; see status.' >&2
    for folder in "${agent_folders[@]}"; do
        jq -e --arg l "$project/$folder/$skill" '.links | has($l)' <<<"$state" >/dev/null && return 1
    done
    return 0
}

cmd_global() {
    local name=$1 value=$2
    case "$value" in on) value=true ;; off) value=false ;; *) echo 'Expected on or off.' >&2; return 2 ;; esac
    library_skill "$name" || { echo "Skill is not in a connected library: $name" >&2; return 1; }
    set_global_choice "$name" "$value" && save_bindings || return 1
    apply_bindings || echo 'Some other bindings need attention; see status.' >&2
    local folder linked target
    target=$(skill_target "$name")
    for folder in "${agent_folders[@]}"; do
        linked=false
        [[ $(link_state "$HOME/$folder/$name" "$target") == linked ]] && linked=true
        [[ "$linked" == "$value" ]] || return 1
    done
    return 0
}

# External source and update policy of the skills of one library (catalog index $1), as a
# JSON object by name; pending: a local switch not yet merged into main.
library_updates() {
    local repo=${cat_paths[$1]} merged_skills
    merged_skills=$(git -C "$repo" show "refs/remotes/origin/$main_branch:sources.json" 2>/dev/null | jq -c '.skills' 2>/dev/null) ||
        merged_skills=''
    [[ -n "$merged_skills" && "$merged_skills" != null ]] || merged_skills='{}'
    # `//` would take a merged false for a missing value, so presence is tested explicitly.
    jq -c --argjson merged "$merged_skills" '.skills | with_entries(select(.value.source.kind != "local")
        | .key as $k | (.value.auto_update == true) as $local
        | .value = {source: (.value.source.url + (if (.value.source.path // ".") == "." then "" else " " + .value.source.path end)),
                    policy: (if $local then "auto" else "confirm" end),
                    pending: ((if ($merged | has($k)) then ($merged[$k].auto_update == true) else $local end) != $local)})' \
        "$repo/sources.json" 2>/dev/null || echo '{}'
}

# One library for status: order, origin, format and the result of its last synchronization.
library_status() {
    local i=$1 format=null sync=null here remote count=0
    # A library whose registry cannot be read is unavailable for linking, yet its folder is there.
    if [[ -d "${cat_paths[i]}" ]]; then
        if here=$(repo=${cat_paths[i]}; library_format 2>/dev/null) &&
            remote=$(repo=${cat_paths[i]}; library_format "refs/remotes/origin/$main_branch" 2>/dev/null); then
            format=$(jq -cn --argjson h "$here" --argjson r "$remote" --argjson s "$supported_format" \
                '([$h, $r] | max) as $f | {library: $f, supported: $s, readOnly: ($f > $s)}')
        fi
        sync=$(read_sync_status "${cat_paths[i]}")
        [[ -n "${cat_skills[i]}" ]] && count=$(grep -c . <<<"${cat_skills[i]}")
    fi
    jq -cn --arg n "${cat_names[i]}" --arg p "${cat_paths[i]}" --arg u "${cat_urls[i]}" --arg r "${cat_ids[i]}" \
        --argjson a "${cat_available[i]}" --argjson c "${cat_configured[i]}" --argjson k "$count" \
        --argjson f "$format" --argjson s "$sync" \
        '{name: $n, path: $p, url: $u, repo: (if $r == "" then null else $r end), available: $a, configured: $c,
          skills: $k, format: $f, sync: $s}'
}

cmd_status() {
    local name project entry folder state_name links i index winner active key shadowed copies updates library_name
    local -a rows=() projects=() libraries=()
    while IFS= read -r project; do
        [[ -n "$project" ]] || continue
        local available=true error=null skills=() list target
        if [[ ! -d "$project" ]]; then
            available=false; list=$(project_skills "$project")
        elif ! list=$(read_project_list "$project" 2>/dev/null); then
            error='"invalid-list"'; list=$(project_skills "$project")
        fi
        while IFS= read -r entry; do
            [[ -n "$entry" ]] || continue
            resolve_entry "$entry"
            library_name=''
            [[ -z "$r_lib" ]] || library_name=${cat_names[r_lib]}
            links='{}'; state_name=applied
            if [[ "$available" == false ]]; then
                state_name=unavailable
            elif [[ -z "$r_lib" ]]; then
                state_name='library-missing'
            elif [[ "${cat_available[r_lib]}" != true ]]; then
                state_name='library-unavailable'
            elif ! library_skill "$r_name" "$r_lib" 2>/dev/null; then
                state_name=missing-skill
            else
                target=$(skill_target "$r_name" "$r_lib")
                for folder in "${agent_folders[@]}"; do
                    key=$(link_state "$project/$folder/$r_name" "$target")
                    links=$(jq -c --arg l "$project/$folder/$r_name" --arg s "$key" '.[$l] = $s' <<<"$links")
                    if [[ "$key" == conflict ]]; then state_name=conflict
                    elif [[ "$key" == missing && "$state_name" == applied ]]; then state_name=pending; fi
                done
            fi
            skills+=("$(jq -cn --arg n "$entry" --arg k "$r_name" --arg l "$library_name" --arg s "$state_name" --argjson x "$links" \
                '{name: $n, skill: $k, library: (if $l == "" then null else $l end), state: $s, links: $x}')")
        done <<<"$list"
        projects+=("$(printf '%s\n' ${skills[@]+"${skills[@]}"} | jq -cs --arg p "$project" --argjson a "$available" --argjson e "$error" \
            'map(select(. != null)) | {path: $p, available: $a, error: $e, skills: .}')")
    done < <(jq -r '.projects | keys[]' <<<"$state")
    for i in "${!cat_names[@]}"; do
        libraries+=("$(library_status "$i")")
        [[ "${cat_available[i]}" == true ]] || continue
        updates=$(library_updates "$i")
        while IFS= read -r name; do
            [[ -n "$name" ]] || continue
            winner=$(winner_of "$name")
            active=false
            [[ -n "$winner" && "${cat_paths[winner]}" == "${cat_paths[i]}" ]] && active=true
            links='{}'
            for folder in "${agent_folders[@]}"; do
                links=$(jq -c --arg l "$HOME/$folder/$name" --arg s "$(link_state "$HOME/$folder/$name" "${cat_paths[i]}/skills/$name")" \
                    '.[$l] = $s' <<<"$links")
            done
            key=$name
            [[ "$active" == true || -z "${cat_ids[i]}" ]] || key="${cat_ids[i]}:$name"
            shadowed=''
            [[ "$active" == true || -z "$winner" ]] || shadowed=${cat_names[winner]}
            copies=$(for index in ${cat_copies[$name]}; do printf '%s\n' "${cat_names[index]}"; done | jq -Rsc 'split("\n") | map(select(length > 0))')
            rows+=("$(jq -cn --arg n "$name" --arg l "${cat_names[i]}" --arg e "$key" --argjson a "$active" --arg s "$shadowed" \
                --argjson c "$copies" --argjson p "$([[ -n "${cat_preferred[$name]+present}" ]] && echo true || echo false)" \
                --argjson g "$(global_json "$name")" --argjson x "$links" \
                --argjson u "$(jq -c --arg n "$name" '.[$n] // null' <<<"$updates")" \
                '{name: $n, library: $l, entry: $e, active: $a, shadowedBy: (if $s == "" then null else $s end), copies: $c,
                  preferred: $p, projects: [], update: $u, global: ($g + {links: $x})}')")
        done <<<"${cat_skills[i]}"
    done
    local here remote format=null
    # Nothing is connected once the last library was disconnected.
    if [[ -n "$repo" ]]; then
        here=$(library_format) || return 1
        remote=$(library_format "refs/remotes/origin/$main_branch") || return 1
        format=$(( here > remote ? here : remote ))
    fi
    printf '%s\n' ${projects[@]+"${projects[@]}"} | jq -cs --arg library "$repo" --arg file "$bindings_file" \
        --argjson format "$format" --argjson supported "$supported_format" \
        --argjson libraries "$(printf '%s\n' ${libraries[@]+"${libraries[@]}"} | jq -cs 'map(select(. != null))')" \
        --argjson skills "$(printf '%s\n' ${rows[@]+"${rows[@]}"} | jq -cs 'map(select(. != null))')" '
        map(select(. != null)) as $projects
        | {library: $library, state: $file,
           format: (if $format == null then null else {library: $format, supported: $supported, readOnly: ($format > $supported)} end),
           libraries: $libraries,
           skills: [$skills[] | .name as $n | .library as $l
                    | .projects = [$projects[] | select(any(.skills[]; .skill == $n and .library == $l)) | .path]],
           projects: $projects}'
}

# --- Connected libraries: order, preferred copies, disconnection -------------------------

sync_status_path() {
    local directory
    directory=$(git -C "$1" rev-parse --absolute-git-dir 2>/dev/null) || return 1
    printf '%s\n' "$directory/agent-skills-sync-status.json"
}

# Result of the last synchronization of a library clone: {time, ok, error}.
write_sync_status() {
    local file temporary
    file=$(sync_status_path "$1") || return 0
    temporary="$file.$$.tmp"
    if jq -cn --arg t "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson ok "$2" --arg e "${3:-}" \
        '{time: $t, ok: $ok, error: (if $ok then null else $e end)}' >"$temporary" && mv -f -- "$temporary" "$file"; then
        return 0
    fi
    rm -f -- "$temporary"
    echo "Synchronization status not saved: $file" >&2
}

# Prints the last synchronization status of a library clone, or null.
read_sync_status() {
    local file
    if file=$(sync_status_path "$1") && [[ -f "$file" ]] && jq -ce 'objects' "$file" 2>/dev/null; then return 0; fi
    echo null
}

assert_configured_library() {
    find_configured_library "$1" >/dev/null && return 0
    echo "Library is not connected: $1 (connected: $(configured_libraries | jq -r 'map(.name) | join(", ")'))" >&2
    return 1
}

cmd_libraries() {
    local i line sync name index mark others
    if ((${#cat_names[@]} == 0)); then echo 'No library is connected.'; return 0; fi
    for i in "${!cat_names[@]}"; do
        line="$((i + 1)). ${cat_names[i]}  ${cat_paths[i]}"
        [[ -z "${cat_ids[i]}" ]] || line+="  (${cat_ids[i]})"
        [[ "${cat_configured[i]}" == true ]] || line+='  [this command only]'
        if [[ "${cat_available[i]}" != true ]]; then
            line+='  UNAVAILABLE'
        else
            sync=$(read_sync_status "${cat_paths[i]}")
            if [[ $(jq -r 'if . == null then "none" elif .ok then "ok" else "failed" end' <<<"$sync") == ok ]]; then
                line+="  synced $(jq -r '.time' <<<"$sync")"
            elif [[ "$sync" != null ]]; then
                line+="  SYNC FAILED $(jq -r '"\(.time): \(.error)"' <<<"$sync")"
            fi
        fi
        printf '%s\n' "$line"
    done
    while IFS= read -r name; do
        [[ -n "$name" && "${cat_copies[$name]}" == *' '* ]] || continue
        mark=''
        [[ -z "${cat_preferred[$name]+present}" ]] || mark=' (preferred)'
        others=''
        for index in ${cat_copies[$name]}; do
            [[ "$index" == "${cat_winner[$name]}" ]] || others+="${others:+, }${cat_names[index]}"
        done
        echo "CONFLICT $name: linked from ${cat_names[${cat_winner[$name]}]}$mark; also in $others"
    done < <(printf '%s\n' "${!cat_copies[@]}" | LC_ALL=C sort)
    return 0
}

# order <library>...: these libraries first, in this order; an earlier one wins a skill name.
cmd_order() {
    local name ordered ok=0
    for name in "$@"; do assert_configured_library "$name" || return 1; done
    if [[ -n $(printf '%s\n' "$@" | LC_ALL=C sort | uniq -d) ]]; then echo 'Each library is named once.' >&2; return 1; fi
    ordered=$(configured_libraries | jq -c --args '. as $all
        | [$ARGS.positional[] as $n | $all[] | select(.name == $n)]
          + [$all[] | select(.name as $x | any($ARGS.positional[]; . == $x) | not)]' "$@") || return 1
    write_skillarium_config "$ordered" "$(preferred_libraries)" || return 1
    init_catalog
    apply_bindings || ok=1
    cmd_libraries
    if ((ok)); then echo 'Some links need attention; see status.' >&2; return 4; fi
    return 0
}

# prefer <skill> <library>|auto: take this library's copy of a skill on this machine.
cmd_prefer() {
    local name=$1 library=$2 prefer index found=0 ok=0 winner
    [[ -n "$name" ]] || { echo 'Name a skill.' >&2; return 1; }
    prefer=$(preferred_libraries) || return 1
    if [[ "$library" == auto ]]; then
        prefer=$(jq -c --arg s "$name" 'del(.[$s])' <<<"$prefer") || return 1
    else
        assert_configured_library "$library" || return 1
        for index in ${cat_copies[$name]-}; do
            [[ "${cat_names[index]}" != "$library" ]] || found=1
        done
        ((found)) || { echo "Skill $name is not in library $library." >&2; return 1; }
        prefer=$(jq -c --arg s "$name" --arg l "$library" '.[$s] = $l' <<<"$prefer") || return 1
    fi
    write_skillarium_config "$(configured_libraries)" "$prefer" || return 1
    init_catalog
    apply_bindings || ok=1
    winner=$(winner_of "$name")
    [[ -z "$winner" ]] || echo "$name is linked from ${cat_names[winner]}"
    if ((ok)); then echo 'Some links need attention; see status.' >&2; return 4; fi
    return 0
}

# disconnect <library> [--force]: removes the library's links and its configuration entry;
# the clone stays on disk.
cmd_disconnect() {
    local name=$1 flag=${2:-} configured index path prefix link skill folder target current errors=0
    if [[ -n "$flag" && "$flag" != --force && "$flag" != -Force ]]; then echo "Unknown option: $flag" >&2; return 1; fi
    assert_configured_library "$name" || return 1
    configured=$(configured_libraries) || return 1
    index=$(find_catalog_library "$name") || return 1
    path=${cat_paths[index]}
    if [[ -z "$flag" && -d "$path" ]]; then
        if [[ -n $(git -C "$path" status --porcelain -- skills sources.json 2>/dev/null) ||
              -n $(git -C "$path" rev-list '@{upstream}..HEAD' 2>/dev/null) ]]; then
            echo "Library $name has unpublished changes; let them publish (sync) first, or pass --force. The clone stays in $path either way." >&2
            return 1
        fi
    fi
    prefix="$path/skills/"
    while IFS= read -r -d '' link && IFS= read -r -d '' target; do
        [[ "$target" == "$prefix"* ]] || continue
        remove_owned_link "$link" || errors=1
    done < <(jq -j '.links | to_entries[] | .key, "\u0000", (.value.target // "" | tostring), "\u0000"' <<<"$state")
    # Links this client did not record are not its own: they stay, with a note.
    while IFS= read -r skill; do
        [[ -n "$skill" ]] || continue
        for folder in "${agent_folders[@]}"; do
            link="$HOME/$folder/$skill"
            [[ -L "$link" ]] || continue
            target=$(readlink -- "$link") || continue
            if [[ "$target" == "$prefix"* || "$(readlink -f -- "$link" 2>/dev/null)" == "$prefix"* ]]; then
                echo "WARNING: Unrecorded link into the disconnected library kept: $link" >&2
            fi
        done
    done <<<"${cat_skills[index]}"
    if ((errors)); then
        save_bindings
        echo "Some links of $name could not be removed; the library stays connected." >&2; return 1
    fi
    write_skillarium_config "$(jq -c --arg n "$name" 'map(select(.name != $n))' <<<"$configured")" \
        "$(preferred_libraries | jq -c --arg n "$name" 'with_entries(select(.value != $n))')" || return 1
    current=$repo
    [[ "$repo" != "$path" ]] || current=''
    init_catalog "$current"
    if ((${#cat_paths[@]})); then mutate --arg l "${cat_paths[0]}" '.library = $l' || return 1; fi
    apply_bindings || echo 'Some links need attention; see status.' >&2
    echo "DISCONNECTED: $name; the clone stays in $path"
    return 0
}
