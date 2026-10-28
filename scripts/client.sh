# Native Linux installer. Sourced by install.sh; no Python runtime needed.
cache="${XDG_CACHE_HOME:-$HOME/.cache}/agent-skills"

safe_path() {
    local rest=$1 component
    [[ -n "$rest" && "$rest" != /* && "$rest" != *\\* && "$rest" != *:* ]] || return 1
    while :; do
        component=${rest%%/*}
        [[ -n "$component" && "$component" != . && "$component" != .. &&
            "$component" != *' ' && "$component" != *. ]] || return 1
        [[ "$rest" == */* ]] || break
        rest=${rest#*/}
    done
}

# Python pathlib sorts by path components. Traverse each directory in byte order
# to reproduce the controller's hash, including directories before their siblings.
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

validate_skill() {
    local folder=$1 name=$2
    [[ -f "$folder/SKILL.md" && ! -L "$folder/SKILL.md" ]] || return 1
    # The controller validates frontmatter; clients verify its approved bytes.
    [[ $(tree_hash "$folder") == "$name" ]]
}

link_version() {
    local name=$1 target=$2 catalog link old temporary errors=0
    for catalog in "$HOME/.agents/skills" "$HOME/.claude/skills"; do
        link="$catalog/$name"
        if [[ -L "$link" && "$link" -ef "$target" ]]; then
            echo "OK: $link"
            continue
        fi
        if [[ -e "$link" || -L "$link" ]]; then
            old=$(readlink -f -- "$link") || old=''
            if [[ ! -L "$link" || -z "$old" || "${old%/*}" != "$cache/versions/$name" ]] ||
                ! validate_skill "$old" "${old##*/}"; then
                echo "CONFLICT (kept): $link" >&2; errors=1; continue
            fi
        fi
        mkdir -p -- "$catalog" || return 1
        temporary="$catalog/.$name-new"
        if [[ -e "$temporary" || -L "$temporary" ]]; then
            echo "Existing temporary link preserved: $temporary" >&2; errors=1; continue
        fi
        ln -s -- "$target" "$temporary" || return 1
        if ! mv -Tf -- "$temporary" "$link"; then
            rm -- "$temporary"; errors=1; continue
        fi
        echo "LINKED: $link"
    done
    return "$errors"
}

install_entry() (
    local record=$1 name kind url relative revision expected target temporary mirror key tree row mode path archive
    name=$(jq -er '.key' <<< "$record") || return 1
    kind=$(jq -er '.value.source.kind' <<< "$record") || return 1
    url=$(jq -er '.value.source.url' <<< "$record") || return 1
    relative=$(jq -r '.value.source.path // "."' <<< "$record") || return 1
    revision=$(jq -er '.value.revision' <<< "$record") || return 1
    expected=$(jq -er '.value.content_sha256' <<< "$record") || return 1
    [[ "$name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ && ${#name} -le 64 && "$expected" =~ ^[0-9a-f]{64}$ ]] || {
        echo 'Invalid registry name or approved hash.' >&2; return 1;
    }
    [[ -n "$url" && "$url" != -* && "$url" != *$'\n'* && "$url" != *$'\r'* ]] || return 1
    target="$cache/versions/$name/$expected"
    if [[ -e "$target" || -L "$target" ]]; then
        validate_skill "$target" "$expected" || {
            echo "Cached skill modified or invalid (kept): $name" >&2; return 1;
        }
    else
        mkdir -p -- "${target%/*}" || return 1
        temporary=$(mktemp -d "${target%/*}/.download-XXXXXX") || return 1
        trap 'rm -rf -- "$temporary"' EXIT
        mkdir -- "$temporary/content" || return 1
        case "$kind" in
            git)
                [[ "$revision" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]] || return 1
                [[ "$relative" == . ]] || safe_path "$relative" || return 1
                key=$(printf '%s' "$url" | sha256sum) || return 1
                mirror="$cache/repositories/${key%% *}"
                mkdir -p -- "$mirror" || return 1
                [[ -f "$mirror/HEAD" ]] || git -C "$mirror" init --bare --quiet || return 1
                if ! git -C "$mirror" cat-file -e "$revision^{commit}" 2>/dev/null; then
                    git -C "$mirror" fetch --quiet --no-tags --depth=1 "$url" "$revision" || return 1
                fi
                tree=$revision
                [[ "$relative" == . ]] || tree="$revision:$relative"
                git -C "$mirror" ls-tree -rz "$tree" > "$temporary/tree" || return 1
                while IFS= read -r -d '' row; do
                    mode=${row%% *}; path=${row#*$'\t'}
                    [[ "$mode" == 100644 || "$mode" == 100755 ]] && safe_path "$path" || {
                        echo "Unsupported file or path in $name" >&2; return 1;
                    }
                done < "$temporary/tree"
                archive="$temporary/content.tar"
                git -C "$mirror" archive --format=tar --output="$archive" "$tree" || return 1
                tar --extract --file="$archive" --directory="$temporary/content" --no-same-owner --no-same-permissions || return 1
                ;;
            url)
                [[ "$url" == https://* && "$revision" =~ ^[0-9a-f]{64}$ ]] || return 1
                curl --proto '=https' --proto-redir '=https' --location --fail --silent --show-error \
                    --max-time 120 --max-filesize 33554432 --output "$temporary/content/SKILL.md" "$url" || return 1
                key=$(sha256sum < "$temporary/content/SKILL.md") || return 1
                [[ "${key%% *}" == "$revision" ]] || { echo 'File differs from approved revision.' >&2; return 1; }
                ;;
            *) echo "Unknown source type: $kind" >&2; return 1 ;;
        esac
        validate_skill "$temporary/content" "$expected" || { echo "Content hash mismatch: $name" >&2; return 1; }
        mv -- "$temporary/content" "$target" || return 1
    fi
    link_version "$name" "$target"
)

install_registry() {
    local entries record dependency errors=0
    for dependency in jq curl sha256sum git tar find sort; do
        command -v "$dependency" >/dev/null || { echo "Missing dependency: $dependency" >&2; return 1; }
    done
    jq -e '.version == 2 and (.skills | type == "object")' "$repo/sources.json" >/dev/null || return 1
    entries=$(jq -c '.skills | to_entries[]' "$repo/sources.json") || return 1
    [[ -n "$entries" ]] || return 0
    while IFS= read -r record; do
        if ! install_entry "$record"; then errors=1; fi
    done <<< "$entries"
    return "$errors"
}
