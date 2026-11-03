# Git-only Linux client. External sources are handled exclusively by the server.
cache="${XDG_CACHE_HOME:-$HOME/.cache}/agent-skills"

skill_names() {
    jq -er '.version == 2 and (.skills | type == "object")' "$repo/sources.json" >/dev/null || return 1
    jq -r '.skills | keys[]' "$repo/sources.json"
}

check_windows_paths() {
    local name=$1 path part stem prefix key char code units index
    local LC_ALL=C.UTF-8
    local invalid='[<>:"\\|?*]' controls=$'[\001-\037]'
    local devices=$'^(CON|PRN|AUX|NUL|CONIN\\$|CONOUT\\$|(COM|LPT)[1-9\u00b9\u00b2\u00b3])$'
    local -a parts=()
    local -A seen=()
    while IFS= read -r -d '' path; do
        [[ -e "$repo/$path" || -L "$repo/$path" ]] || continue
        if [[ "$path" =~ $controls ]]; then echo "Windows-incompatible skill path: $path" >&2; return 1; fi
        IFS=/ read -r -a parts <<< "$path"
        prefix=''
        for part in "${parts[@]}"; do
            stem=${part%%.*}
            while [[ "$stem" == *' ' ]]; do stem=${stem% }; done
            if [[ "$part" =~ $invalid || "$part" == *' ' || "$part" == *. ||
                  "${stem^^}" =~ $devices || "${part,,}" == .git || "${part,,}" == .gitattributes ]]; then
                echo "Windows-incompatible skill path: $path" >&2; return 1
            fi
            if ((${#part} > 127)); then
                units=0
                for ((index=0; index<${#part}; index++)); do
                    char=${part:index:1}; printf -v code '%d' "'$char"
                    ((units+=1+(code>65535)))
                done
                if ((units>255)); then echo "Windows-incompatible skill path: $path" >&2; return 1; fi
            fi
            prefix+="${prefix:+/}$part"; key=${prefix^^}
            if [[ -n ${seen[$key]+present} && "${seen[$key]}" != "$prefix" ]]; then
                echo "Windows path collision: ${seen[$key]} / $prefix" >&2; return 1
            fi
            seen[$key]=$prefix
        done
    done < <(printf '%s\0' "skills/$name"; git -C "$repo" ls-files -z --cached --others --exclude-standard -- "skills/$name")
}

check_skill() {
    local name=$1 folder="$repo/skills/$1"
    [[ "$name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ && ${#name} -le 64 ]] || return 1
    [[ ! -L "$repo/skills" && -d "$folder" && ! -L "$folder" && -f "$folder/SKILL.md" && ! -L "$folder/SKILL.md" ]] || {
        echo "Missing or unsafe skill folder: $name; run server maintenance." >&2; return 1;
    }
    [[ -z $(find "$folder" -type l -print -quit) ]] || { echo "Skill symlinks preserved: $name" >&2; return 1; }
    [[ -z $(find "$folder" -iname .git -o -iname .gitattributes) ]] || {
        echo "Unsupported Git metadata in skill: $name" >&2; return 1;
    }
    check_windows_paths "$name"
}

# One-time compatibility with old links into hash-addressed caches.
# These hashes are never used to download or select a source version.
hash_feed() {
    local root=$1 prefix=$2 item relative digest i
    while IFS= read -r -d '' item; do
        relative="$prefix${item##*/}"
        if [[ -L "$item" ]]; then
            echo 'Symlinks in skill content are unsupported.' >&2; return 1
        elif [[ -d "$item" ]]; then
            hash_feed "$item" "$relative/" || return 1
        elif [[ -f "$item" ]]; then
            printf '%s\0' "$relative"
            digest=$(sha256sum < "$item") || return 1
            digest=${digest%% *}
            for ((i=0; i<64; i+=2)); do printf '%b' "\\x${digest:i:2}"; done
            printf '\0'
        else
            echo 'Only regular files and directories are supported.' >&2; return 1
        fi
    done < <(find "$root" -mindepth 1 -maxdepth 1 -print0 | LC_ALL=C sort -z)
}

tree_hash() {
    local digest
    digest=$(hash_feed "$1" '' | sha256sum) || return 1
    printf '%s\n' "${digest%% *}"
}

migrate_legacy_links() {
    local name link old actual baseline expected folder
    while IFS= read -r name; do
        folder="$repo/skills/$name"
        [[ -d "$folder" ]] || continue
        check_skill "$name" || return 1
        for link in "$HOME/.agents/skills/$name" "$HOME/.claude/skills/$name"; do
            [[ -L "$link" ]] || continue
            old=$(readlink -f -- "$link") || continue
            [[ "${old%/*}" == "$cache/versions/$name" && "${old##*/}" =~ ^[0-9a-f]{64}$ ]] || continue
            baseline=${old##*/}
            actual=$(tree_hash "$old") || return 1
            [[ "$actual" != "$baseline" ]] || continue
            [[ -f "$old/SKILL.md" ]] || { echo "Legacy deletion preserved: $name" >&2; return 1; }
            expected=$(jq -er --arg name "$name" '.skills[$name] | .customization.content_sha256 // .content_sha256' "$repo/sources.json") || return 1
            if [[ $(tree_hash "$folder") == "$actual" ]]; then link_version "$name" || return 1; continue; fi
            if [[ "$expected" != "$baseline" || $(tree_hash "$folder") != "$baseline" ]]; then
                echo "CONFLICT: legacy edits differ from catalog; preserved: $old" >&2; return 1
            fi
            # Keep the legacy cache as a backup; import edits into the Git tree.
            local staged
            staged=$(mktemp -d "$(git -C "$repo" rev-parse --absolute-git-dir)/legacy-XXXXXX") || return 1
            if ! cp -a -- "$old/." "$staged/"; then rm -rf -- "$staged"; return 1; fi
            if [[ $(tree_hash "$staged") != "$actual" || $(tree_hash "$old") != "$actual" ]]; then
                rm -rf -- "$staged"; return 1
            fi
            rm -rf -- "$folder"
            mv -- "$staged" "$folder" || return 1
            link_version "$name" || return 1
        done
    done < <(skill_names)
}

publish_local_changes() {
    local row path name
    local -a paths=()
    skill_names >/dev/null || return 1
    # A manually staged change belongs to the user, not this automatic commit.
    [[ -z $(git -C "$repo" diff --cached --name-only) ]] || { echo 'Staged changes preserved.' >&2; return 1; }
    while IFS= read -r -d '' row; do
        path=${row:3}
        [[ "$path" == skills/*/* ]] || { echo "Unrelated local changes preserved: $path" >&2; return 1; }
        name=${path#skills/}; name=${name%%/*}
        jq -e --arg name "$name" '.skills | has($name)' "$repo/sources.json" >/dev/null || return 1
        check_skill "$name" || return 1
        paths+=(":(literal)$path")
    done < <(git -C "$repo" -c status.renames=false status --porcelain --untracked-files=all -z)
    ((${#paths[@]})) || return 0
    git -C "$repo" add -A -- "${paths[@]}" || return 1
    git -C "$repo" -c user.name=agent-skills -c user.email=agent-skills@localhost commit \
        -m $'Share local skill edits\n\nAgent-Skills-Client: 1' || return 1
}

assert_automatic_commits() {
    local revision message pending
    pending=$(git -C "$repo" rev-list '@{upstream}..HEAD') || return 1
    while IFS= read -r revision; do
        [[ -n "$revision" ]] || continue
        message=$(git -C "$repo" show -s --format=%B "$revision") || return 1
        [[ "$message" == *$'\nAgent-Skills-Client: 1' ]] || { echo 'Unpublished manual commits preserved.' >&2; return 1; }
    done <<< "$pending"
}

reconcile_repository() {
    local temporary merged result=0
    if git -C "$repo" merge-base --is-ancestor '@{upstream}' HEAD; then return 0; fi
    if git -C "$repo" merge-base --is-ancestor HEAD '@{upstream}'; then
        git -C "$repo" merge --ff-only --no-overwrite-ignore '@{upstream}'
        return $?
    fi
    # Merge in a disposable worktree so conflicts never enter installed skills.
    temporary=$(mktemp -d "$(git -C "$repo" rev-parse --absolute-git-dir)/client-merge-XXXXXX") || return 1
    if ! git -C "$repo" worktree add --detach "$temporary" HEAD; then rmdir "$temporary"; return 1; fi
    if git -C "$temporary" -c user.name=agent-skills -c user.email=agent-skills@localhost \
        -c core.hooksPath=/dev/null merge --no-ff --no-edit \
        -m $'Reconcile shared skill edits\n\nAgent-Skills-Client: 1' "$(git -C "$repo" rev-parse '@{upstream}')"; then
        merged=$(git -C "$temporary" rev-parse HEAD) || result=1
        if ((result == 0)); then git -C "$repo" merge --ff-only --no-overwrite-ignore "$merged" || result=1; fi
    else
        echo 'CONFLICT: local commit preserved; installed files unchanged. Resolve Git history manually.' >&2
        result=1
    fi
    git -C "$repo" worktree remove --force "$temporary" || return 1
    return "$result"
}

sync_repository() {
    if ! git -C "$repo" remote get-url origin >/dev/null 2>&1; then
        echo 'No origin configured; linking local catalog only.' >&2; return 0
    fi
    git -C "$repo" symbolic-ref -q HEAD >/dev/null || return 1
    git -C "$repo" rev-parse --verify '@{upstream}' >/dev/null || return 1
    assert_automatic_commits || return 1
    migrate_legacy_links || return 1
    publish_local_changes || return 1
    local attempt pending
    for attempt in 1 2; do
        git -C "$repo" -c credential.interactive=false fetch origin || return 1
        assert_automatic_commits || return 1
        reconcile_repository || return 1
        migrate_legacy_links || return 1
        publish_local_changes || return 1
        pending=$(git -C "$repo" rev-list '@{upstream}..HEAD') || return 1
        [[ -n "$pending" ]] || return 0
        if git -C "$repo" -c credential.interactive=false push; then return 0; fi
    done
    echo 'Git publication failed; local commit preserved for retry.' >&2
    return 1
}

link_version() {
    local name=$1 target="$repo/skills/$1" catalog link old temporary errors=0
    check_skill "$name" || return 1
    for catalog in "$HOME/.agents/skills" "$HOME/.claude/skills"; do
        link="$catalog/$name"
        if [[ -L "$link" && "$link" -ef "$target" ]]; then continue; fi
        if [[ -e "$link" || -L "$link" ]]; then
            old=$(readlink -f -- "$link") || old=''
            if [[ ! -L "$link" || "${old%/*}" != "$cache/versions/$name" || ! "${old##*/}" =~ ^[0-9a-f]{64}$ ]] ||
                { [[ $(tree_hash "$old") != "${old##*/}" ]] && ! diff -qr -- "$old" "$target" >/dev/null; }; then
                echo "CONFLICT (kept): $link" >&2; errors=1; continue
            fi
        fi
        mkdir -p -- "$catalog" || return 1
        temporary="$catalog/.$name-new"
        [[ ! -e "$temporary" && ! -L "$temporary" ]] || { echo "Temporary link preserved: $temporary" >&2; errors=1; continue; }
        ln -s -- "$target" "$temporary" || return 1
        if ! mv -Tf -- "$temporary" "$link"; then rm -- "$temporary"; errors=1; continue; fi
        echo "LINKED: $link"
    done
    return "$errors"
}

# Conflicts that stop reconcile_repository, shown and resolved by the "Projects" UI.
diverged_revisions() {
    local head upstream
    git -C "$repo" remote get-url origin >/dev/null 2>&1 || return 0
    git -C "$repo" symbolic-ref -q HEAD >/dev/null || return 0
    upstream=$(git -C "$repo" rev-parse --verify --quiet '@{upstream}') || return 0
    head=$(git -C "$repo" rev-parse HEAD) || return 1
    if git -C "$repo" merge-base --is-ancestor "$upstream" "$head" ||
        git -C "$repo" merge-base --is-ancestor "$head" "$upstream"; then return 0; fi
    printf '%s %s\n' "$head" "$upstream"
}

# Repeats the background merge in a disposable worktree, fills conflict_paths and runs "$@" <worktree>.
trial_merge() {
    local head=$1 upstream=$2 temporary path merged=true result=0
    shift 2
    temporary=$(mktemp -d "$(git -C "$repo" rev-parse --absolute-git-dir)/client-merge-XXXXXX") || return 1
    if ! git -C "$repo" worktree add --quiet --detach "$temporary" "$head"; then rmdir "$temporary"; return 1; fi
    conflict_paths=()
    git -C "$temporary" -c user.name=agent-skills -c user.email=agent-skills@localhost -c core.hooksPath=/dev/null merge --quiet --no-commit --no-ff "$upstream" >/dev/null 2>&1 || merged=false
    while IFS= read -r -d '' path; do conflict_paths+=("$path"); done < <(git -C "$temporary" diff --name-only -z --diff-filter=U)
    if [[ "$merged" == false && ${#conflict_paths[@]} -eq 0 ]]; then
        echo 'Trial merge failed without conflicts.' >&2; result=1
    else
        "$@" "$temporary" || result=$?
    fi
    git -C "$repo" worktree remove --force "$temporary" || return 1
    return "$result"
}

conflict_side() {
    local file=$1 id=$2
    if [[ $(tr -d '\000' < "$file" | wc -c) -eq $(wc -c < "$file") ]] && iconv -f UTF-8 -t UTF-8 "$file" >/dev/null 2>&1; then
        jq -cn --arg id "$id" --rawfile text "$file" '{id: $id, binary: false, text: $text}'
    else
        jq -cn --arg id "$id" '{id: $id, binary: true, text: null}'
    fi
}

# The local side is the working tree: the next synchronization commits it first.
cmd_conflicts() {
    local revisions head upstream path skill id mine library blob result=0
    local -a files=()
    revisions=$(diverged_revisions) || return 1
    if [[ -z "$revisions" ]]; then jq -cn '{state: "ok", upstream: null, files: []}'; return 0; fi
    read -r head upstream <<<"$revisions"
    trial_merge "$head" "$upstream" true || return 1
    blob=$(mktemp) || return 1
    for path in "${conflict_paths[@]}"; do
        if [[ ! "$path" =~ ^skills/([a-z0-9]+(-[a-z0-9]+)*)/ ]]; then
            echo "Conflict outside skills/ needs manual Git resolution: $path" >&2; result=1; break
        fi
        skill=${BASH_REMATCH[1]}
        mine=null library=null
        if [[ -f "$repo/$path" && ! -L "$repo/$path" ]]; then
            if ! id=$(git -C "$repo" hash-object --no-filters -- "$path") || ! mine=$(conflict_side "$repo/$path" "$id"); then result=1; break; fi
        fi
        if id=$(git -C "$repo" rev-parse --verify --quiet "$upstream:$path"); then
            if ! git -C "$repo" cat-file blob "$id" > "$blob" || ! library=$(conflict_side "$blob" "$id"); then result=1; break; fi
        fi
        files+=("$(jq -cn --arg p "$path" --arg s "$skill" --argjson m "$mine" --argjson l "$library" \
            '{path: $p, skill: $s, mine: $m, library: $l}')")
    done
    rm -f -- "$blob"
    ((result == 0)) || return "$result"
    printf '%s\n' "${files[@]}" | jq -cs --arg u "$upstream" \
        'map(select(. != null)) | {state: (if length > 0 then "conflict" else "ok" end), upstream: $u, files: .}'
}

# Runs inside trial_merge; returns 5 when the shown conflict no longer matches.
apply_choices() {
    local worktree=$1 path entry take mine_id library_id target source
    [[ "$(printf '%s\n' "${conflict_paths[@]}" | LC_ALL=C sort)" == "$(jq -r '.files[].path' "$resolve_request" | LC_ALL=C sort)" ]] || return 5
    for path in "${conflict_paths[@]}"; do
        entry=$(jq -c --arg p "$path" '[.files[] | select(.path == $p)][0]' "$resolve_request") || return 1
        mine_id=$(git -C "$repo" rev-parse --verify --quiet "$resolve_head:$path") || mine_id=''
        library_id=$(git -C "$repo" rev-parse --verify --quiet "$resolve_upstream:$path") || library_id=''
        [[ "$(jq -r '.mine // ""' <<<"$entry")" == "$mine_id" && "$(jq -r '.library // ""' <<<"$entry")" == "$library_id" ]] || return 5
        target="$worktree/$path"
        rm -f -- "$target"
        take=$(jq -r '.take' <<<"$entry")
        case "$take" in
            mine|library)
                source=$mine_id
                [[ "$take" == mine ]] || source=$library_id
                if [[ -n "$source" ]]; then
                    mkdir -p -- "${target%/*}" || return 1
                    git -C "$repo" cat-file blob "$source" > "$target" || return 1
                fi ;;
            edited)
                jq -e '.text | type == "string"' <<<"$entry" >/dev/null || { echo "No edited text: $path" >&2; return 1; }
                mkdir -p -- "${target%/*}" || return 1
                jq -j '.text' <<<"$entry" > "$target" || return 1 ;;
            *) echo "Unknown choice for $path." >&2; return 1 ;;
        esac
        git -C "$worktree" add -A -- ":(literal)$path" || return 1
    done
    [[ -z $(git -C "$worktree" diff --name-only --diff-filter=U) ]] || { echo 'Unresolved files remain; nothing merged.' >&2; return 1; }
    git -C "$worktree" -c user.name=agent-skills -c user.email=agent-skills@localhost -c core.hooksPath=/dev/null \
        commit --quiet --no-edit -m $'Resolve shared skill edits\n\nAgent-Skills-Client: 1' || return 1
    resolved_commit=$(git -C "$worktree" rev-parse HEAD)
}

# Request: {upstream, files: [{path, take: mine|library|edited, mine, library, text}]}, where mine and
# library are the object ids that were shown. Exit 5: the conflict changed and nothing was merged.
cmd_resolve() {
    local revisions head upstream pushed=true result=0
    export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=Never
    export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -oBatchMode=yes -oConnectTimeout=15}"
    resolve_request=$1
    jq -e '(.upstream | type == "string") and (.files | type == "array")' "$resolve_request" >/dev/null || {
        echo 'Invalid resolution request.' >&2; return 1;
    }
    assert_automatic_commits || return 1
    publish_local_changes || return 1
    revisions=$(diverged_revisions) || return 1
    read -r head upstream <<<"$revisions"
    if [[ -z "$revisions" || "$upstream" != "$(jq -r '.upstream' "$resolve_request")" ]]; then
        echo 'The library changed; review the conflict again.' >&2; return 5
    fi
    resolve_head=$head resolve_upstream=$upstream resolved_commit=''
    trial_merge "$head" "$upstream" apply_choices || result=$?
    if ((result == 5)); then echo 'The conflict changed; review it again.' >&2; fi
    ((result == 0)) || return "$result"
    git -C "$repo" merge --quiet --ff-only --no-overwrite-ignore "$resolved_commit" || return 1
    if ! git -C "$repo" -c credential.interactive=false push --quiet; then
        pushed=false; echo 'Publication failed; the next synchronization retries it.' >&2
    fi
    apply_bindings || echo 'Some skill links need attention; see status.' >&2
    jq -cn --argjson p "$pushed" '{resolved: true, pushed: $p}'
}
