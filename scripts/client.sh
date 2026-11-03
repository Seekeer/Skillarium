# Git-only Linux client. External sources are handled exclusively by the server.
# Publications go through proposal branches and pull requests; main is never pushed.
cache="${XDG_CACHE_HOME:-$HOME/.cache}/agent-skills"
main_branch=main
proposal_prefix=agent-skills/update

skill_names() {
    jq -er '.version == 2 and (.skills | type == "object")' "$repo/sources.json" >/dev/null || return 1
    jq -r '.skills | keys[]' "$repo/sources.json"
}

# Registered skills plus folders published by clients before server registration.
library_names() {
    local names folder name
    names=$(skill_names) || return 1
    {
        [[ -n "$names" ]] && printf '%s\n' "$names"
        for folder in "$repo"/skills/*/; do
            [[ -d "$folder" ]] || continue
            name=${folder%/}; name=${name##*/}
            [[ "$name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] && printf '%s\n' "$name"
        done
    } | LC_ALL=C sort -u
}

# Directories receiving links; project bindings may add further roots.
link_roots() {
    printf '%s\n' "$HOME/.agents/skills" "$HOME/.claude/skills"
}

machine_name() {
    local name
    name=$(git -C "$repo" config --get agent-skills.machine) || name=$(hostname -s 2>/dev/null || hostname)
    [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || {
        echo 'Invalid machine name; set: git config agent-skills.machine <name>' >&2; return 1;
    }
    printf '%s\n' "$name"
}

clone_id() {
    local id
    id=$(git -C "$repo" config --get agent-skills.clone-id) && { printf '%s\n' "$id"; return 0; }
    id=$(od -An -tx1 -N4 /dev/urandom | tr -d ' \n') || return 1
    git -C "$repo" config agent-skills.clone-id "$id" || return 1
    printf '%s\n' "$id"
}

# Service identity per command; the user's own Git settings stay untouched.
service_git() {
    local machine
    machine=$(machine_name) || return 1
    GIT_AUTHOR_NAME="agent-skills/$machine" GIT_AUTHOR_EMAIL="agent-skills+$machine@localhost" \
        GIT_COMMITTER_NAME="agent-skills/$machine" GIT_COMMITTER_EMAIL="agent-skills+$machine@localhost" \
        git "$@"
}

check_windows_paths() {
    local name=$1 path part stem prefix key char code units index
    local LC_ALL=C.UTF-8
    local invalid='[<>:"\\|?*]' controls=$'[\001-\037]'
    local devices=$'^(CON|PRN|AUX|NUL|CONIN\\$|CONOUT\\$|(COM|LPT)[1-9¹²³])$'
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

# Frontmatter structure only; the meaning of the instructions is not reviewed.
check_skill_text() {
    local name=$1 file=$2 line first=1 closed=0 found_name='' description=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line=${line%$'\r'}
        if ((first)); then line=${line#$'\xef\xbb\xbf'}; [[ "$line" == '---' ]] || break; first=0; continue; fi
        if [[ "$line" == '---' ]]; then closed=1; break; fi
        if [[ "$line" =~ ^name:[[:space:]]*(.+)$ ]]; then
            found_name=${BASH_REMATCH[1]}; found_name=${found_name%"${found_name##*[![:space:]]}"}
            found_name=${found_name#[\"\']}; found_name=${found_name%[\"\']}
        fi
        [[ "$line" =~ ^description:[[:space:]]*[^[:space:]] ]] && description=1
    done < "$file"
    if ((first || !closed)); then echo "Invalid skill $name: SKILL.md needs frontmatter" >&2; return 1; fi
    [[ "$found_name" == "$name" ]] || { echo "Invalid skill $name: SKILL.md name must match the folder" >&2; return 1; }
    ((description)) || { echo "Invalid skill $name: SKILL.md needs a description" >&2; return 1; }
}

check_skill() {
    local name=$1 folder="$repo/skills/$1"
    [[ "$name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ && ${#name} -le 64 ]] || { echo "Invalid skill name: $name" >&2; return 1; }
    [[ ! -L "$repo/skills" && -d "$folder" && ! -L "$folder" && -f "$folder/SKILL.md" && ! -L "$folder/SKILL.md" ]] || {
        echo "Missing or unsafe skill folder: $name; deleting a whole skill needs an explicit management change." >&2; return 1;
    }
    [[ -z $(find "$folder" -type l -print -quit) ]] || { echo "Skill symlinks preserved: $name" >&2; return 1; }
    [[ -z $(find "$folder" -iname .git -o -iname .gitattributes) ]] || {
        echo "Unsupported Git metadata in skill: $name" >&2; return 1;
    }
    check_skill_text "$name" "$folder/SKILL.md" || return 1
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

# Any valid edit of a skill folder, including a new own skill, is published.
publish_local_changes() {
    local row path name
    local -a paths=()
    local -A checked=()
    skill_names >/dev/null || return 1
    # A manually staged change belongs to the user, not this automatic commit.
    [[ -z $(git -C "$repo" diff --cached --name-only) ]] || { echo 'Staged changes preserved.' >&2; return 1; }
    while IFS= read -r -d '' row; do
        path=${row:3}
        [[ "$path" == skills/*/* ]] || { echo "Unrelated local changes preserved: $path" >&2; return 1; }
        name=${path#skills/}; name=${name%%/*}
        if [[ -z ${checked[$name]+present} ]]; then check_skill "$name" || return 1; checked[$name]=1; fi
        paths+=(":(literal)$path")
    done < <(git -C "$repo" -c status.renames=false status --porcelain --untracked-files=all -z)
    ((${#paths[@]})) || return 0
    git -C "$repo" add -A -- "${paths[@]}" || return 1
    service_git -C "$repo" commit -q -m $'Share local skill edits\n\nAgent-Skills-Client: 1' || return 1
}

assert_automatic_commits() {
    local revision message pending
    pending=$(git -C "$repo" rev-list '@{upstream}..HEAD') || return 1
    while IFS= read -r revision; do
        [[ -n "$revision" ]] || continue
        message=$(git -C "$repo" show -s --format=%B "$revision") || return 1
        [[ "$message" == *$'\nAgent-Skills-Client: 1'* ]] || { echo 'Unpublished manual commits preserved.' >&2; return 1; }
    done <<< "$pending"
}

reconcile_repository() {
    local temporary merged result=0
    if git -C "$repo" merge-base --is-ancestor '@{upstream}' HEAD; then return 0; fi
    if git -C "$repo" merge-base --is-ancestor HEAD '@{upstream}'; then
        git -C "$repo" merge -q --ff-only --no-overwrite-ignore '@{upstream}'
        return $?
    fi
    # Merge in a disposable worktree so conflicts never enter installed skills.
    temporary=$(mktemp -d "$(git -C "$repo" rev-parse --absolute-git-dir)/client-merge-XXXXXX") || return 1
    if ! git -C "$repo" worktree add -q --detach "$temporary" HEAD; then rmdir "$temporary"; return 1; fi
    if service_git -C "$temporary" -c core.hooksPath=/dev/null merge -q --no-ff --no-edit \
        -m $'Reconcile shared skill edits\n\nAgent-Skills-Client: 1' "$(git -C "$repo" rev-parse '@{upstream}')"; then
        merged=$(git -C "$temporary" rev-parse HEAD) || result=1
        if ((result == 0)); then git -C "$repo" merge -q --ff-only --no-overwrite-ignore "$merged" || result=1; fi
    else
        echo 'CONFLICT: local commit preserved; installed files unchanged. Resolve Git history manually.' >&2
        result=1
    fi
    git -C "$repo" worktree remove --force "$temporary" || return 1
    return "$result"
}

# --- Hosting API -------------------------------------------------------------

api_base() {
    local url
    url=$(git -C "$repo" config --get agent-skills.api) && { printf '%s\n' "${url%/}"; return 0; }
    url=$(git -C "$repo" remote get-url origin) || return 1
    if [[ "$url" =~ ^(https?)://([^@/]*@)?([^/]+)/(.+)$ ]]; then
        url="${BASH_REMATCH[1]}://${BASH_REMATCH[3]}/api/v1/repos/${BASH_REMATCH[4]}"
    elif [[ "$url" =~ ^(ssh://)?([^@/]+@)?([^:/]+)(:[0-9]+)?[:/](.+)$ ]]; then
        url="https://${BASH_REMATCH[3]}/api/v1/repos/${BASH_REMATCH[5]}"
    else
        echo 'Cannot derive hosting API; set git config agent-skills.api' >&2; return 1
    fi
    url=${url%/}; printf '%s\n' "${url%.git}"
}

api_body=''
api_status=0
api_credentials=''

# Sets api_status/api_body. Credentials come from the Git credential helper and
# are passed to curl through stdin, never on the command line.
api_request() {
    local method=$1 path=$2 data=${3-} base output host scheme body_file
    base=$(api_base) || return 1
    if [[ -z "$api_credentials" ]]; then
        scheme=${base%%://*}; host=${base#*://}; host=${host%%/*}
        api_credentials=$(printf 'protocol=%s\nhost=%s\n\n' "$scheme" "$host" |
            GIT_TERMINAL_PROMPT=0 git -C "$repo" -c credential.interactive=false credential fill 2>/dev/null) || api_credentials=''
        [[ "$api_credentials" == *password=* ]] || { api_credentials=''; echo 'Hosting credentials are unavailable; publication pending.' >&2; return 1; }
    fi
    local user password
    user=$(sed -n 's/^username=//p' <<< "$api_credentials"); password=$(sed -n 's/^password=//p' <<< "$api_credentials")
    body_file=$(mktemp) || return 1
    local -a args=(-sS -K - -o "$body_file" -w '%{http_code}' -X "$method" -H 'Accept: application/json' --max-time 30)
    if [[ -n "$data" ]]; then args+=(-H 'Content-Type: application/json' --data-binary "$data"); fi
    output=$(printf 'user = "%s:%s"\n' "${user//\"/\\\"}" "${password//\"/\\\"}" | curl "${args[@]}" "$base$path") || {
        rm -f -- "$body_file"; api_status=0; echo 'Hosting unavailable; publication pending.' >&2; return 1;
    }
    api_status=$output; api_body=$(cat -- "$body_file"); rm -f -- "$body_file"
    if [[ "$api_status" == 401 || "$api_status" == 403 ]]; then
        echo "Hosting rejected access ($api_status); publication pending." >&2; return 1
    fi
}

open_pull() {
    local branch=$1 page=1 found count
    while :; do
        api_request GET "/pulls?state=open&limit=50&page=$page" || return 1
        [[ "$api_status" == 200 ]] || { echo "Cannot list pull requests ($api_status)." >&2; return 1; }
        found=$(jq -r --arg head "$branch" --arg base "$main_branch" \
            '[.[] | select(.head.ref == $head and .base.ref == $base)][0] // empty | "\(.number) \(.head.sha) \(.html_url // "")"' <<< "$api_body") || return 1
        [[ -n "$found" ]] && { printf '%s\n' "$found"; return 0; }
        count=$(jq length <<< "$api_body") || return 1
        ((count < 50)) && return 0
        page=$((page + 1))
    done
}

schedule_merge() {
    local number=$1 head=$2 body
    body=$(jq -cn --arg head "$head" '{Do: "merge", merge_when_checks_succeed: true, head_commit_id: $head, delete_branch_after_merge: true}')
    api_request POST "/pulls/$number/merge" "$body" || return 1
    if [[ "$api_status" == 409 ]] && jq -e '.message | test("schedul"; "i")' <<< "$api_body" >/dev/null 2>&1; then
        # Renew an earlier schedule for the current head.
        api_request DELETE "/pulls/$number/merge" || return 1
        api_request POST "/pulls/$number/merge" "$body" || return 1
    fi
    [[ "$api_status" =~ ^20[014]$ ]] && return 0
    api_request GET "/pulls/$number" || return 1
    jq -e '.merged == true' <<< "$api_body" >/dev/null 2>&1 && return 0
    echo "Automatic merge was not accepted for pull request #$number ($api_status)." >&2
    return 1
}

publication_state() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" > "$(git -C "$repo" rev-parse --absolute-git-dir)/agent-skills-publication"
}

# Push local automatic commits to this clone's proposal branch and make sure
# exactly one pull request with automatic merge exists for it.
publish_proposal() {
    local head branch machine id found number title body
    head=$(git -C "$repo" rev-parse HEAD) || return 1
    machine=$(machine_name) || return 1
    id=$(clone_id) || return 1
    branch="agent-skills/sync/$machine-$id"
    if ! git -C "$repo" -c credential.interactive=false push -q origin "HEAD:refs/heads/$branch"; then
        publication_state "pending: push of $branch failed; local commits preserved"
        echo 'Proposal branch push failed; local commits preserved for retry.' >&2; return 1
    fi
    found=$(open_pull "$branch") || { publication_state "pending: hosting unavailable for $branch"; return 1; }
    if [[ -z "$found" ]]; then
        title="Skill edits from $machine"
        body=$(jq -cn --arg head "$branch" --arg base "$main_branch" --arg title "$title" \
            '{head: $head, base: $base, title: $title, body: "Automatic publication of client skill edits; the hosting runs the technical checks."}')
        if api_request POST /pulls "$body" && [[ "$api_status" =~ ^20[01]$ ]]; then
            found=$(jq -r '"\(.number) \(.head.sha) \(.html_url // "")"' <<< "$api_body")
        else
            # The request may have succeeded although the response was lost.
            found=$(open_pull "$branch") || found=''
        fi
        [[ -n "$found" ]] || { publication_state "pending: pull request for $branch not created"; echo 'Pull request not created; retry later.' >&2; return 1; }
    fi
    number=${found%% *}
    if ! schedule_merge "$number" "$head"; then
        publication_state "pending: pull request #$number awaits automatic merge scheduling"; return 1
    fi
    publication_state "pending: pull request #$number ${found##* } head $head"
    echo "PUBLISHED: pull request #$number scheduled for automatic merge"
}

sync_repository() {
    if ! git -C "$repo" remote get-url origin >/dev/null 2>&1; then
        echo 'No origin configured; linking local catalog only.' >&2; return 0
    fi
    git -C "$repo" symbolic-ref -q HEAD >/dev/null || return 1
    git -C "$repo" rev-parse --verify -q '@{upstream}' >/dev/null || return 1
    assert_automatic_commits || return 1
    migrate_legacy_links || return 1
    publish_local_changes || return 1
    local attempt pending
    for attempt in 1 2; do
        git -C "$repo" -c credential.interactive=false fetch -q --prune origin || return 1
        assert_automatic_commits || return 1
        reconcile_repository || return 1
        migrate_legacy_links || return 1
        publish_local_changes || return 1
        pending=$(git -C "$repo" rev-list '@{upstream}..HEAD') || return 1
        if [[ -z "$pending" ]]; then publication_state 'current'; return 0; fi
        if publish_proposal; then return 0; fi
    done
    echo 'Publication pending; local commits preserved for the next synchronization.' >&2
    return 1
}

link_version() {
    local name=$1 target="$repo/skills/$1" catalog link old temporary errors=0
    check_skill "$name" || return 1
    while IFS= read -r catalog; do
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
    done < <(link_roots)
    return "$errors"
}

install_registry() {
    local name names registered errors=0
    names=$(library_names) || return 1
    registered=$(skill_names) || return 1
    while IFS= read -r name; do
        [[ -n "$name" ]] || continue
        if ! grep -qxF -- "$name" <<< "$registered" && [[ ! -f "$repo/skills/$name/SKILL.md" ]]; then continue; fi
        link_version "$name" || errors=1
    done <<< "$names"
    return "$errors"
}

# --- Status and external update proposals ------------------------------------

trailer() {
    sed -n "s/^Agent-Skills-$2: //p" <<< "$1" | head -n 1
}

proposal_refs() {
    git -C "$repo" for-each-ref --sort=-committerdate --format='%(refname)' "refs/remotes/origin/$proposal_prefix/${1:+$1/}"
}

show_status() {
    local machine assigned pending state
    machine=$(machine_name) || return 1
    assigned=$(git -C "$repo" show "refs/remotes/origin/$main_branch:agent-skills.json" 2>/dev/null | jq -r '.server.machine // "none"') || assigned=unknown
    echo "Machine: $machine; assigned server: ${assigned:-unknown}"
    pending=$(git -C "$repo" rev-list --count '@{upstream}..HEAD' 2>/dev/null) || pending=unknown
    echo "Unpublished automatic commits: $pending"
    state="$(git -C "$repo" rev-parse --absolute-git-dir)/agent-skills-publication"
    [[ -f "$state" ]] && echo "Publication: $(cat -- "$state")"
    list_updates
}

list_updates() {
    local ref message count=0
    while IFS= read -r ref; do
        [[ -n "$ref" ]] || continue
        message=$(git -C "$repo" log --first-parent --grep='^Agent-Skills-Proposal: ' -1 --format=%B "$ref") || return 1
        echo "UPDATE $(trailer "$message" Proposal): $(trailer "$message" Status), policy $(trailer "$message" Policy), incoming $(trailer "$message" Incoming-Revision | cut -c1-12), commit $(git -C "$repo" rev-parse "$ref")"
        count=$((count + 1))
    done < <(proposal_refs)
    ((count)) || echo 'No pending external updates.'
}

# Prints "<ref> <head>" of the newest proposal of a skill.
find_proposal() {
    local name=$1 ref
    ref=$(proposal_refs "$name" | head -n 1)
    [[ -n "$ref" ]] || { echo "No pending update proposal for $name." >&2; return 1; }
    printf '%s %s\n' "$ref" "$(git -C "$repo" rev-parse "$ref")"
}

review_update() {
    local name=$1 found ref head message status worktree branch materials
    git -C "$repo" -c credential.interactive=false fetch -q --prune origin || return 1
    found=$(find_proposal "$name") || return 1
    ref=${found% *}; head=${found#* }; branch=${ref#refs/remotes/origin/}
    message=$(git -C "$repo" log --first-parent --grep='^Agent-Skills-Proposal: ' -1 --format=%B "$head")
    status=$(trailer "$message" Status)
    echo "Proposal: $name ($status), policy $(trailer "$message" Policy)"
    echo "Incoming revision: $(trailer "$message" Incoming-Revision)"
    echo "Proposal commit: $head (branch $branch)"
    echo "Base (upstream/$name in main), ours (skills/$name in main) and incoming (upstream/$name in the proposal) are Git trees:"
    echo "  base: refs/remotes/origin/$main_branch:upstream/$name  ours: refs/remotes/origin/$main_branch:skills/$name  incoming: $head:upstream/$name"
    echo '--- Result compared with the current shared version ---'
    git -C "$repo" --no-pager diff --stat "refs/remotes/origin/$main_branch" "$head" -- "skills/$name" "sources.json"
    git -C "$repo" --no-pager diff "refs/remotes/origin/$main_branch" "$head" -- "skills/$name"
    worktree="$(git -C "$repo" rev-parse --absolute-git-dir)/agent-skills-review/$name"
    if [[ -e "$worktree" ]]; then
        if [[ -n $(git -C "$worktree" status --porcelain 2>/dev/null) && $(git -C "$worktree" rev-parse HEAD 2>/dev/null) != "$head" ]]; then
            echo "Review copy with edits for an older proposal preserved: $worktree" >&2; return 1
        fi
        if [[ $(git -C "$worktree" rev-parse HEAD 2>/dev/null) != "$head" ]]; then
            git -C "$repo" worktree remove --force "$worktree" || return 1
        fi
    fi
    if [[ ! -e "$worktree" ]]; then
        mkdir -p -- "${worktree%/*}" && git -C "$repo" worktree add -q --detach "$worktree" "$head" || return 1
        materials="refs/remotes/origin/agent-skills/materials/$name/$(trailer "$message" Key)"
        if [[ "$status" == conflict ]] && git -C "$repo" rev-parse -q --verify "$materials" >/dev/null; then
            git -C "$worktree" checkout -q "$materials" -- "skills/$name" && git -C "$worktree" reset -q
        fi
    fi
    echo "Review copy outside the installation: $worktree/skills/$name"
    if [[ "$status" == confirm ]]; then
        echo "Accept exactly this result: bash install.sh --approve $name $head"
    else
        echo "Conflict markers (if any) are in the review copy. Resolve with one of:"
        echo "  bash install.sh --resolve $name $head keep-ours|take-upstream|merged"
    fi
}

# Merge the reviewed proposal only if its inputs and result are unchanged.
accept_proposal() {
    local name=$1 branch=$2 head=$3 result=$4 incoming=$5 found number current attempt
    found=$(open_pull "$branch") || return 1
    [[ -n "$found" ]] || { echo 'The pull request of this proposal is not open.' >&2; return 1; }
    number=${found%% *}; current=${found#* }; current=${current%% *}
    [[ "$current" == "$head" ]] || { echo 'Proposal changed since review; review it again.' >&2; return 1; }
    if ! git -C "$repo" merge-base --is-ancestor "refs/remotes/origin/$main_branch" "$head"; then
        api_request POST "/pulls/$number/update?style=merge" || return 1
        [[ "$api_status" =~ ^20[014]$ ]] || { echo "Cannot update the proposal ($api_status); wait for the server." >&2; return 1; }
        for attempt in $(seq 1 30); do
            found=$(open_pull "$branch") || return 1
            current=${found#* }; current=${current%% *}
            [[ "$current" != "$head" ]] && break
            sleep 1
        done
        git -C "$repo" -c credential.interactive=false fetch -q origin "$branch" || return 1
        if [[ $(git -C "$repo" rev-parse -q --verify "$current:skills/$name") != "$result" ||
              $(git -C "$repo" rev-parse -q --verify "$current:upstream/$name") != "$incoming" ]]; then
            echo 'Updated proposal differs from the reviewed result; review it again.' >&2; return 1
        fi
        head=$current
    fi
    schedule_merge "$number" "$head" || return 1
    echo "ACCEPTED: $name pull request #$number at $head"
}

proposal_for() {
    local name=$1 commit=$2 found shared policy
    git -C "$repo" -c credential.interactive=false fetch -q --prune origin || return 1
    found=$(find_proposal "$name") || return 1
    [[ "${found#* }" == "$commit" ]] || { echo 'Proposal changed since review; review it again.' >&2; return 1; }
    proposal_message=$(git -C "$repo" log --first-parent --grep='^Agent-Skills-Proposal: ' -1 --format=%B "$commit")
    proposal_branch=${found% *}; proposal_branch=${proposal_branch#refs/remotes/origin/}
    shared=$(git -C "$repo" rev-parse -q --verify "refs/remotes/origin/$main_branch:skills/$name") || shared=none
    [[ "$shared" == "$(trailer "$proposal_message" Shared-Tree)" ]] || {
        echo 'Shared skill changed since the proposal; the server will prepare a new one.' >&2; return 1;
    }
    policy=$(git -C "$repo" show "refs/remotes/origin/$main_branch:sources.json" |
        jq -r --arg name "$name" 'if .skills[$name].auto_update then "auto" else "confirm" end') || return 1
    [[ "$policy" == "$(trailer "$proposal_message" Policy)" ]] || {
        echo 'Update policy changed since the proposal; the server will prepare a new one.' >&2; return 1;
    }
}

approve_update() {
    local name=$1 commit=$2 proposal_message proposal_branch
    proposal_for "$name" "$commit" || return 1
    [[ $(trailer "$proposal_message" Status) == confirm ]] || {
        echo 'Conflicts need --resolve with keep-ours, take-upstream or merged.' >&2; return 1;
    }
    accept_proposal "$name" "$proposal_branch" "$commit" "$(trailer "$proposal_message" Result-Tree)" \
        "$(trailer "$proposal_message" Incoming-Tree)"
}

resolve_update() {
    local name=$1 commit=$2 strategy=$3 proposal_message proposal_branch target worktree head temporary
    proposal_for "$name" "$commit" || return 1
    worktree="$(git -C "$repo" rev-parse --absolute-git-dir)/agent-skills-review/$name"
    case "$strategy" in
        keep-ours) target=$(trailer "$proposal_message" Shared-Tree) ;;
        take-upstream) target=$(trailer "$proposal_message" Incoming-Tree) ;;
        merged)
            [[ -d "$worktree" && $(git -C "$worktree" rev-parse HEAD) == "$commit" ]] || {
                echo "Run --review $name and edit $worktree/skills/$name first." >&2; return 1;
            }
            if grep -rlE '^(<<<<<<<|>>>>>>>)( |$)' -- "$worktree/skills/$name" >/dev/null; then
                echo 'Conflict markers remain in the merged result; nothing published.' >&2; return 1
            fi
            ( repo=$worktree; check_skill "$name" ) || return 1
            git -C "$worktree" add -A -- "skills/$name" || return 1
            target=$(git -C "$worktree" write-tree --prefix="skills/$name/") || return 1 ;;
        *) echo "Unknown strategy: $strategy" >&2; return 1 ;;
    esac
    head=$commit
    if [[ $(git -C "$repo" rev-parse -q --verify "$commit:skills/$name") != "$target" ]]; then
        # Content-only commit on top of the server proposal; pins stay server-authored.
        temporary=$(mktemp -d "$(git -C "$repo" rev-parse --absolute-git-dir)/client-resolve-XXXXXX") || return 1
        git -C "$repo" worktree add -q --detach "$temporary" "$commit" || { rmdir "$temporary"; return 1; }
        if git -C "$temporary" rm -rq -- "skills/$name" && git -C "$temporary" read-tree --prefix="skills/$name/" -u "$target" &&
            service_git -C "$temporary" commit -q -m "Resolve $name update: $strategy"$'\n\nAgent-Skills-Client: 1'; then
            head=$(git -C "$temporary" rev-parse HEAD)
        fi
        git -C "$repo" worktree remove --force "$temporary" || return 1
        [[ "$head" != "$commit" ]] || return 1
        git -C "$repo" -c credential.interactive=false push -q origin "$head:refs/heads/$proposal_branch" || {
            echo 'Proposal push failed; resolution not published.' >&2; return 1;
        }
    fi
    if [[ "$strategy" == merged && "$head" != "$commit" ]]; then
        git -C "$worktree" update-ref HEAD "$head" "$commit" || return 1
    fi
    accept_proposal "$name" "$proposal_branch" "$head" "$target" "$(trailer "$proposal_message" Incoming-Tree)" || return 1
    [[ "$strategy" != merged ]] || git -C "$repo" worktree remove --force "$worktree"
}

# --- Stopped synchronization: shown and resolved by the "Projects" UI ---------

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

# Repeats the merge of reconcile_repository in a disposable worktree, fills conflict_paths and runs "$@" <worktree>.
trial_merge() {
    local head=$1 upstream=$2 temporary path merged=true result=0
    shift 2
    temporary=$(mktemp -d "$(git -C "$repo" rev-parse --absolute-git-dir)/client-merge-XXXXXX") || return 1
    if ! git -C "$repo" worktree add --quiet --detach "$temporary" "$head"; then rmdir "$temporary"; return 1; fi
    conflict_paths=()
    git -C "$temporary" -c user.name=agent-skills -c user.email=agent-skills@localhost -c core.hooksPath=/dev/null \
        merge --quiet --no-commit --no-ff "$upstream" >/dev/null 2>&1 || merged=false
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
        # A hand-edited result must still be a valid skill; nothing is merged otherwise.
        if [[ "$path" =~ ^skills/([^/]+)/SKILL\.md$ && -f "$target" ]]; then
            check_skill_text "${BASH_REMATCH[1]}" "$target" || return 1
        fi
        git -C "$worktree" add -A -- ":(literal)$path" || return 1
    done
    [[ -z $(git -C "$worktree" diff --name-only --diff-filter=U) ]] || { echo 'Unresolved files remain; nothing merged.' >&2; return 1; }
    service_git -C "$worktree" -c core.hooksPath=/dev/null \
        commit -q --no-edit -m $'Resolve shared skill edits\n\nAgent-Skills-Client: 1' || return 1
    resolved_commit=$(git -C "$worktree" rev-parse HEAD)
}

# Request: {upstream, files: [{path, take: mine|library|edited, mine, library, text}]}, where mine and
# library are the object ids that were shown. The merge is published like any client edit, through
# the proposal pull request. Exit 5: the conflict changed and nothing was merged.
cmd_resolve() {
    local revisions head upstream published=true result=0
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
    git -C "$repo" merge -q --ff-only --no-overwrite-ignore "$resolved_commit" || return 1
    if ! publish_proposal >/dev/null; then
        published=false; echo 'Publication pending; the next synchronization retries it.' >&2
    fi
    apply_bindings || echo 'Some skill links need attention; see status.' >&2
    jq -cn --argjson p "$published" '{resolved: true, published: $p}'
}
