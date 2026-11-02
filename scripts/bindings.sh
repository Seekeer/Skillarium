# Local skill availability: global links and project bindings of this library.
# Project lists live in each project's Git; registered paths, global choices
# and link ownership stay in local state that is never published.
bindings_file="${XDG_STATE_HOME:-$HOME/.local/state}/agent-skills/bindings.json"
project_list=.agent-skills.json
agent_folders=(.agents/skills .claude/skills)

load_bindings() {
    if [[ ! -e "$bindings_file" ]]; then
        state=$(jq -cn --arg library "$repo" \
            '{version: 1, library: $library, projects: {}, global: {}, links: {}, excludes: {}}') || return 1
        saved_state=''
        return 0
    fi
    state=$(jq -c 'select(.version == 1 and (.library | type) == "string" and
        ([.projects, .global, .links, .excludes] | all(type == "object")))' "$bindings_file" 2>/dev/null)
    [[ -n "$state" ]] || { echo "Invalid local bindings state preserved: $bindings_file" >&2; return 1; }
    if [[ $(jq -r .library <<<"$state") != "$repo" ]]; then
        echo "Local bindings belong to another library: $(jq -r .library <<<"$state")" >&2; return 1
    fi
    saved_state=$state
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

library_skill() {
    jq -e --arg name "$1" '.skills | has($name)' "$repo/sources.json" >/dev/null 2>&1 && check_skill "$1"
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
    jq -rs 'if length == 1 then .[0] else null end
        | if type == "object" and .version == 1 and (keys - ["skills", "version"] == []) and
              (.skills | type) == "array" and
              all(.skills[]; type == "string" and length <= 64 and test("^[a-z0-9]+(-[a-z0-9]+)*$"))
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

link_project_skill() {
    local project=$1 name=$2 target="$repo/skills/$2" folder link line errors=0
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
        record_link "$link" "$(readlink -- "$link")" "$name" "$project" "$exclude_file" "$line" || return 1
    done
    return "$errors"
}

reconcile_project() {
    local project=$1 names name link location errors=0
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
    while IFS= read -r link; do
        [[ -n "$link" ]] || continue
        remove_owned_link "$link" || errors=1
    done < <(jq -r --arg p "$project" '.projects[$p].skills as $keep | .links | to_entries[]
        | .value.skill as $s | select(.value.project == $p and ([$keep[] | select(. == $s)] == [])) | .key' <<<"$state")
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        if ! library_skill "$name" 2>/dev/null; then
            echo "NOT IN LIBRARY (not applied): $name in $project" >&2; errors=1; continue
        fi
        if link_project_skill "$project" "$name"; then
            # The automatic global choice of a UI connection waits for its success.
            mutate --arg s "$name" --arg p "$project" \
                'if .global[$s].pending == $p then .global[$s] = {enabled: false, manual: false, pending: null} else . end' || return 1
        else
            errors=1
        fi
    done <<<"$names"
    return "$errors"
}

set_global_links() {
    local name=$1 target="$repo/skills/$1" folder link errors=0
    if global_enabled "$name"; then
        link_version "$name" || errors=1
        for folder in "${agent_folders[@]}"; do
            link="$HOME/$folder/$name"
            if [[ -L "$link" && "$link" -ef "$target" ]]; then
                record_link "$link" "$(readlink -- "$link")" "$name" '' || return 1
            fi
        done
        return "$errors"
    fi
    for folder in "${agent_folders[@]}"; do
        link="$HOME/$folder/$name"
        if [[ -L "$link" && "$link" -ef "$target" ]]; then
            rm -- "$link" || { errors=1; continue; }
            echo "UNLINKED: $link"
        elif [[ -e "$link" || -L "$link" ]]; then
            echo "Foreign global install kept: $link" >&2
        fi
        forget_link "$link" || return 1
    done
    return "$errors"
}

# Brings links of every registered project and global folder to the saved choices.
apply_bindings() {
    local project name names errors=0
    names=$(skill_names) || return 1
    while IFS= read -r project; do
        [[ -n "$project" ]] || continue
        reconcile_project "$project" || errors=1
    done < <(jq -r '.projects | keys[]' <<<"$state")
    # A pending automatic choice is dropped together with the binding that requested it.
    mutate '.projects as $p | .global |= with_entries(.key as $k
        | if .value.pending != null and ([($p[.value.pending].skills // [])[] | select(. == $k)] == [])
          then .value.pending = null else . end)' || errors=1
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        set_global_links "$name" || errors=1
    done <<<"$names"
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

binding_applied() {
    local folder
    for folder in "${agent_folders[@]}"; do
        [[ $(link_state "$1/$folder/$2" "$repo/skills/$2") == linked ]] || return 1
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
    local name names
    [[ -d "$1" ]] || { echo "Project folder not found: $1" >&2; return 1; }
    project=$(project_key "$1") || return 1
    if [[ "$project" == "$repo" || "$project" == "$repo"/* ]]; then
        echo 'The library itself cannot be a project.' >&2; return 1
    fi
    names=$(read_project_list "$project") || return 1
    mutate --arg p "$project" '.projects[$p] //= {available: true, skills: []}' || return 1
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        request_auto_off "$name" "$project" || return 1
    done <<<"$names"
    save_bindings || return 1
    apply_bindings || echo 'Some bindings need attention; see status.' >&2
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        binding_applied "$project" "$name" || { echo "Binding not applied: $name in $project" >&2; return 4; }
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

cmd_bind() {
    local name=$2 names
    require_project "$1" || return 1
    require_available || return 1
    library_skill "$name" || { echo "Skill is not in the library: $name" >&2; return 1; }
    names=$(read_project_list "$project") || return 1
    request_auto_off "$name" "$project" && save_bindings || return 1
    # shellcheck disable=SC2086 # validated skill names
    write_project_list "$project" $names "$name" || return 1
    apply_bindings || echo 'Some other bindings need attention; see status.' >&2
    binding_applied "$project" "$name" || { echo "Binding saved but not applied: $name in $project" >&2; return 4; }
}

cmd_unbind() {
    local name=$2 choice=${3:-} names other others=0
    case "$choice" in ''|global|library) ;; *) echo "Unknown choice: $choice" >&2; return 2 ;; esac
    require_project "$1" || return 1
    require_available || return 1
    names=$(read_project_list "$project") || return 1
    if grep -qxF -- "$name" <<<"$names"; then
        while IFS= read -r other; do
            [[ -n "$other" && "$other" != "$project" ]] || continue
            if project_skills "$other" | grep -qxF -- "$name"; then others=1; break; fi
        done < <(jq -r '.projects | keys[]' <<<"$state")
        if ((others == 0)) && [[ -z "$choice" ]]; then
            # The UI asks; cancelling keeps the binding because nothing changed yet.
            jq -cn --arg s "$name" --arg p "$project" --argjson g "$(global_json "$name")" \
                '{decision: "last-project", skill: $s, project: $p, global: $g, choices: ["global", "library"]}'
            return 3
        fi
    fi
    if [[ -n "$choice" ]]; then
        set_global_choice "$name" "$([[ "$choice" == global ]] && echo true || echo false)" && save_bindings || return 1
    fi
    # shellcheck disable=SC2046 # validated skill names
    write_project_list "$project" $(grep -vxF -- "$name" <<<"$names") || return 1
    apply_bindings || echo 'Some other bindings need attention; see status.' >&2
    local folder
    for folder in "${agent_folders[@]}"; do
        jq -e --arg l "$project/$folder/$name" '.links | has($l)' <<<"$state" >/dev/null && return 1
    done
    return 0
}

cmd_global() {
    local name=$1 value=$2
    case "$value" in on) value=true ;; off) value=false ;; *) echo 'Expected on or off.' >&2; return 2 ;; esac
    library_skill "$name" || { echo "Skill is not in the library: $name" >&2; return 1; }
    set_global_choice "$name" "$value" && save_bindings || return 1
    apply_bindings || echo 'Some other bindings need attention; see status.' >&2
    local folder linked
    for folder in "${agent_folders[@]}"; do
        linked=false
        [[ $(link_state "$HOME/$folder/$name" "$repo/skills/$name") == linked ]] && linked=true
        [[ "$linked" == "$value" ]] || return 1
    done
    return 0
}

cmd_status() {
    local name project names folder rows=() projects=() entries state_name links
    names=$(skill_names) || return 1
    while IFS= read -r project; do
        [[ -n "$project" ]] || continue
        local available=true error=null skills=() list
        if [[ ! -d "$project" ]]; then
            available=false; list=$(project_skills "$project")
        elif ! list=$(read_project_list "$project" 2>/dev/null); then
            error='"invalid-list"'; list=$(project_skills "$project")
        fi
        while IFS= read -r name; do
            [[ -n "$name" ]] || continue
            links='{}'; state_name=applied
            if [[ "$available" == false ]]; then
                state_name=unavailable
            elif ! library_skill "$name" 2>/dev/null; then
                state_name=missing-skill
            else
                for folder in "${agent_folders[@]}"; do
                    entries=$(link_state "$project/$folder/$name" "$repo/skills/$name")
                    links=$(jq -c --arg l "$project/$folder/$name" --arg s "$entries" '.[$l] = $s' <<<"$links")
                    if [[ "$entries" == conflict ]]; then state_name=conflict
                    elif [[ "$entries" == missing && "$state_name" == applied ]]; then state_name=pending; fi
                done
            fi
            skills+=("$(jq -cn --arg n "$name" --arg s "$state_name" --argjson l "$links" '{name: $n, state: $s, links: $l}')")
        done <<<"$list"
        projects+=("$(printf '%s\n' "${skills[@]}" | jq -cs --arg p "$project" --argjson a "$available" --argjson e "$error" \
            'map(select(. != null)) | {path: $p, available: $a, error: $e, skills: .}')")
    done < <(jq -r '.projects | keys[]' <<<"$state")
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        links='{}'
        for folder in "${agent_folders[@]}"; do
            links=$(jq -c --arg l "$HOME/$folder/$name" --arg s "$(link_state "$HOME/$folder/$name" "$repo/skills/$name")" \
                '.[$l] = $s' <<<"$links")
        done
        rows+=("$(jq -cn --arg n "$name" --argjson g "$(global_json "$name")" --argjson l "$links" \
            '{name: $n, global: ($g + {links: $l}), projects: []}')")
    done <<<"$names"
    printf '%s\n' "${projects[@]}" | jq -cs --arg library "$repo" --arg file "$bindings_file" \
        --argjson skills "$(printf '%s\n' "${rows[@]}" | jq -cs 'map(select(. != null))')" '
        map(select(. != null)) as $projects
        | {library: $library, state: $file,
           skills: [$skills[] | .name as $n | .projects = [$projects[] | select(any(.skills[]; .name == $n)) | .path]],
           projects: $projects}'
}
