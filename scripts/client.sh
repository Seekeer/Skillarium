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

install_registry() {
    local name names errors=0
    names=$(skill_names) || return 1
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        link_version "$name" || errors=1
    done <<< "$names"
    return "$errors"
}
