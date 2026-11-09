#!/usr/bin/env bash
# Connect this Linux machine to a skill library; run by `skillarium client`.
#
#   skillarium client --library https://gitea.example.org/owner/skills.git [--path <folder>] [--name <name>] [--no-app]
#
# Installs the Skillarium AppImage of this engine's release (--no-app: engine
# only, with an hourly self-update timer instead), stores the Gitea token
# (prompted, or --token-file) in the Git credential helper after checking
# write access, clones the library (the first one into ~/Skillarium/library,
# further ones into ~/Skillarium/<name>; an existing clone is kept), adds it to
# the end of the connected libraries in the Skillarium configuration, links the
# skills and installs the hourly sync of all libraries. Repeating it with a
# connected library's URL only repairs that connection.
set -euo pipefail
engine=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$engine/scripts/locate.sh"

usage='Usage: skillarium client --library <url> [--path <folder>] [--name <name>] [--token-file <file>]
                         [--machine <name>] [--no-app] [--no-timers] [--no-launch] [--from-app]'
library_url='' path='' library_name='' token_file='' machine='' no_app=false no_timers=false no_launch=false
from_app=false
while (($#)); do
    case "$1" in
        --library) library_url=${2:?--library needs a URL}; shift 2 ;;
        --library=*) library_url=${1#--library=}; shift ;;
        --path) path=${2:?--path needs a folder}; shift 2 ;;
        # Short local name of the library; by default the repository name.
        --name) library_name=${2:?--name needs a name}; shift 2 ;;
        --token-file) token_file=${2:?--token-file needs a file}; shift 2 ;;
        --machine) machine=${2:?--machine needs a name}; shift 2 ;;
        --no-app) no_app=true; shift ;;
        --no-timers) no_timers=true; shift ;;
        --no-launch) no_launch=true; shift ;;
        # Run by the app's first-run screen: the app is there and keeps the engine updated.
        --from-app) from_app=true no_app=true no_launch=true; shift ;;
        -h|--help) echo "$usage"; exit 0 ;;
        *) echo "$usage" >&2; exit 2 ;;
    esac
done
[[ -n "$library_url" ]] || { echo "$usage" >&2; exit 2; }
die() { echo "ERROR: $*" >&2; exit 1; }

missing=()
for tool in git jq curl flock; do command -v "$tool" >/dev/null || missing+=("$tool"); done
((${#missing[@]} == 0)) || die "missing: ${missing[*]} (flock is in util-linux); install them first"

pattern='^(https?)://([^/@]+)((/[^/]+)*)/([^/]+)/([^/]+)/?$'
[[ "$library_url" =~ $pattern ]] || die 'expected the library as https://host/owner/name.git (no credentials in the URL)'
scheme=${BASH_REMATCH[1]} host=${BASH_REMATCH[2]} prefix=${BASH_REMATCH[3]} owner=${BASH_REMATCH[5]} name=${BASH_REMATCH[6]%.git}
base="$scheme://$host$prefix"
url="$base/$owner/$name.git"
api="$base/api/v1"

# Host, hosting prefix, owner and name, lower case: the same repository on another host differs.
library_key() {
    [[ "$1" =~ $pattern ]] || return 0
    local key="${BASH_REMATCH[1]}://${BASH_REMATCH[2]}${BASH_REMATCH[3]}/${BASH_REMATCH[5]}/${BASH_REMATCH[6]%.git}"
    printf '%s' "${key,,}"
}
key=$(library_key "$url")

# Name and folder of the connected library with this key; empty when none.
find_known() {
    local item_name item_path item_url item_key
    while IFS= read -r -d '' item_name && IFS= read -r -d '' item_path && IFS= read -r -d '' item_url; do
        item_key=$(library_key "$item_url")
        if [[ -z "$item_key" && -e "$item_path/.git" ]]; then
            item_key=$(library_key "$(git -C "$item_path" config --get remote.origin.url || true)")
        fi
        if [[ -n "$item_key" && "$item_key" == "$key" ]]; then printf '%s\0%s\0' "$item_name" "$item_path"; return 0; fi
    done < <(configured_rows)
}

# A library connected before keeps its folder and name: repeating only repairs it.
libraries=$(configured_libraries)
known='' known_path=''
{ IFS= read -r -d '' known && IFS= read -r -d '' known_path; } < <(find_known) || true
if [[ -n "$known" ]]; then
    [[ -n "$path" ]] || path=$known_path
    [[ -n "$library_name" ]] || library_name=$known
fi
taken() { [[ -z "$known" ]] && jq -e --arg n "$1" 'any(.[]; .name == $n)' <<<"$libraries" >/dev/null; }
[[ -n "$library_name" ]] || library_name=$name
if taken "$library_name"; then library_name="$owner-$name"; fi
[[ "$library_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die "invalid library name: $library_name; pass --name"
if taken "$library_name"; then die "a library named $library_name is connected already; pass --name"; fi
if [[ -z "$path" ]]; then
    if [[ -z "$known" && $(jq length <<<"$libraries") -gt 0 ]]; then
        path="$HOME/Skillarium/$library_name"
    else
        path="$HOME/Skillarium/library"
    fi
fi

if [[ -n "$token_file" ]]; then
    token=$(tr -d '[:space:]' <"$token_file")
else
    echo 'Gitea access token for the library (write:repository); it goes to the Git credential helper.'
    read -r -s -p 'Token: ' token </dev/tty
    echo
fi
[[ -n "$token" ]] || die 'no token given'

gitea() { # gitea <resource>: JSON of an authenticated GET; fails on any error status
    # The header comes through stdin, so the token never appears in the process list.
    printf 'header = "Authorization: token %s"\n' "$token" |
        curl -fsS --retry 2 --config - -H 'Accept: application/json' "$api$1"
}
user=$(gitea /user) || {
    # curl -f exits 22 on an HTTP error status; anything else means no answer at all.
    status=$?
    [[ $status -eq 22 ]] && die 'Gitea rejected the token'
    die "Unable to connect to Gitea at $api (curl exit $status)"
}
login=$(jq -r '.login' <<<"$user")
repository=$(gitea "/repos/$owner/$name") || die "cannot read $owner/$name with this token"
[[ $(jq -r '.permissions.push // false' <<<"$repository") == true ]] || die "the token of $login cannot write to $owner/$name"
# Git keeps one token per host: another account on the same host would be replaced.
stored=$(printf 'protocol=%s\nhost=%s\n\n' "$scheme" "$host" |
    GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=true SSH_ASKPASS=true git -c credential.interactive=false credential fill 2>/dev/null |
    sed -n 's/^username=//p' | head -n 1) || stored=''
if [[ -n "$stored" && "$stored" != "$login" ]]; then
    echo "WARNING: Git keeps one token per host: the stored token of another account on $host is replaced; libraries of that account stop syncing." >&2
fi
if [[ -z "$(git config --get credential.helper || true)" ]]; then
    git config --global credential.helper store
    echo 'Git credential helper: store (~/.git-credentials, readable by this user only)'
fi
printf 'protocol=%s\nhost=%s\nusername=%s\npassword=%s\n\n' "$scheme" "$host" "$login" "$token" | git credential approve
unset token

if [[ -e "$path/.git" ]]; then
    origin=$(git -C "$path" config --get remote.origin.url || true)
    [[ -n "$origin" && "$(library_key "$origin")" == "$key" ]] ||
        die "$path is a clone of $origin, not of $url; pass --path"
    echo "Existing clone kept: $path"
elif [[ -e "$path" ]] && [[ -n "$(ls -A -- "$path")" ]]; then
    die "$path exists and is not a clone; pass --path"
else
    mkdir -p -- "$(dirname -- "$path")"
    git -c credential.interactive=false clone -q "$url" "$path"
    echo "Library cloned into $path"
fi
path=$(cd -- "$path" && pwd -P)
[[ -f "$path/agent-skills.json" && -f "$path/sources.json" ]] || die "$url is not a skill library"
[[ -z "$machine" ]] || git -C "$path" config agent-skills.machine "$machine"

# Applied to the configuration as it is now, not as it was before the clone, under the lock
# of the links (see bindings.sh), so a parallel connection or disconnection is never lost.
state_folder="${XDG_STATE_HOME:-$HOME/.local/state}/agent-skills"
mkdir -p -- "$state_folder" || die "cannot create $state_folder"
exec {config_lock_fd}>"$state_folder/bindings.lock" || die 'cannot open the lock of the skill links'
flock -w 300 "$config_lock_fd" || die 'skill links are being updated by another run; retry later'
libraries=$(configured_libraries)
known='' current_path=''
{ IFS= read -r -d '' known && IFS= read -r -d '' current_path; } < <(find_known) || true
if [[ -n "$known" ]]; then
    library_name=$known
    libraries=$(jq -c --arg k "$known" --arg n "$library_name" --arg p "$path" --arg u "$url" \
        'map(if .name == $k then {name: $n, path: $p, url: $u} else . end)' <<<"$libraries")
else
    # A configured entry of the same folder gives way to this one.
    libraries=$(while IFS= read -r -d '' item_name && IFS= read -r -d '' item_path && IFS= read -r -d '' item_url; do
            if [[ "$(cd -- "$item_path" 2>/dev/null && pwd -P || printf '%s' "$item_path")" != "$path" ]]; then
                jq -cn --arg n "$item_name" --arg p "$item_path" --arg u "$item_url" '{name: $n, path: $p, url: $u}'
            fi
        done < <(configured_rows) | jq -cs --arg n "$library_name" --arg p "$path" --arg u "$url" '. + [{name: $n, path: $p, url: $u}]')
fi
if [[ -z "$known" ]] && jq -e --arg n "$library_name" '.[:-1] | any(.[]; .name == $n)' <<<"$libraries" >/dev/null; then
    die "a library named $library_name is connected already; pass --name"
fi
write_skillarium_config "$libraries" || die "cannot write $(skillarium_config)"
exec {config_lock_fd}>&-

app=''
if ! $no_app; then
    entry=$(jq -c '.app.linux // empty' "$engine/release.json" 2>/dev/null || true)
    if [[ -z "$entry" ]]; then
        echo 'This Skillarium release has no Linux app; skills and sync work without it.' >&2
    else
        folder=$(mktemp -d)
        trap 'rm -rf -- "$folder"' EXIT
        curl -fsSL --retry 2 -o "$folder/app" "$(jq -r .url <<<"$entry")" || die 'app download failed'
        [[ "$(wc -c <"$folder/app" | tr -d '[:space:]')" == "$(jq -r .size <<<"$entry")" &&
           "$(sha512sum <"$folder/app" | cut -d' ' -f1)" == "$(jq -r .sha512 <<<"$entry")" ]] ||
            die 'checksum mismatch for the app; it was not installed'
        app="$HOME/.local/bin/Skillarium.AppImage"
        mkdir -p -- "$HOME/.local/bin" "$HOME/.local/share/applications"
        install -m 755 "$folder/app" "$app.new"
        mv -f -- "$app.new" "$app"
        cat >"$HOME/.local/share/applications/skillarium.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Skillarium
Comment=Shared agent skills and project connections
Exec="$app" %U
Terminal=false
Categories=Development;
EOF
        echo "Skillarium app installed: $app"
    fi
fi

if $no_timers; then
    bash "$engine/install.sh" --library "$path" --sync-only
else
    bash "$engine/install.sh" --library "$path" --install
    # Without the app nothing else updates the engine.
    [[ -n "$app" ]] || $from_app || bash "$engine/scripts/skillarium.sh" enable-updates
fi
if [[ -n "$app" ]] && ! $no_launch && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    nohup "$app" >/dev/null 2>&1 &
fi
echo "CONNECTED: $url in $path as $library_name ($(jq length <<<"$libraries") libraries connected)"
