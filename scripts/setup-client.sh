#!/usr/bin/env bash
# Connect this Linux machine to a skill library; run by `skillarium client`.
#
#   skillarium client --library https://gitea.example.org/owner/skills.git [--path <folder>] [--no-app]
#
# Installs the Skillarium AppImage of this engine's release (--no-app: engine
# only, with an hourly self-update timer instead), stores the Gitea token
# (prompted, or --token-file) in the Git credential helper after checking
# write access, clones the library (default ~/Skillarium/library; an existing
# clone is kept), writes the Skillarium configuration, links the skills and
# installs the hourly sync timer. Safe to repeat.
set -euo pipefail
engine=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)

usage='Usage: skillarium client --library <url> [--path <folder>] [--token-file <file>] [--machine <name>]
                         [--no-app] [--no-timers] [--no-launch] [--from-app]'
library_url='' path="$HOME/Skillarium/library" token_file='' machine='' no_app=false no_timers=false no_launch=false
from_app=false
while (($#)); do
    case "$1" in
        --library) library_url=${2:?--library needs a URL}; shift 2 ;;
        --library=*) library_url=${1#--library=}; shift ;;
        --path) path=${2:?--path needs a folder}; shift 2 ;;
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

if [[ -n "$token_file" ]]; then
    token=$(tr -d '[:space:]' <"$token_file")
else
    echo 'Gitea access token for the library (write:repository); it goes to the Git credential helper.'
    read -r -s -p 'Token: ' token </dev/tty
    echo
fi
[[ -n "$token" ]] || die 'no token given'

gitea() { # gitea <resource>: JSON of an authenticated GET; fails on any error status
    curl -fsS --retry 2 -H "Authorization: token $token" -H 'Accept: application/json' "$api$1"
}
user=$(gitea /user) || die 'Gitea rejected the token'
login=$(jq -r '.login' <<<"$user")
repository=$(gitea "/repos/$owner/$name") || die "cannot read $owner/$name with this token"
[[ $(jq -r '.permissions.push // false' <<<"$repository") == true ]] || die "the token of $login cannot write to $owner/$name"
if [[ -z "$(git config --get credential.helper || true)" ]]; then
    git config --global credential.helper store
    echo 'Git credential helper: store (~/.git-credentials, readable by this user only)'
fi
printf 'protocol=%s\nhost=%s\nusername=%s\npassword=%s\n\n' "$scheme" "$host" "$login" "$token" | git credential approve
unset token

if [[ -e "$path/.git" ]]; then
    origin=$(git -C "$path" config --get remote.origin.url || true)
    [[ "$origin" =~ $pattern && "${BASH_REMATCH[5]}" == "$owner" && "${BASH_REMATCH[6]%.git}" == "$name" ]] ||
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

config="${XDG_CONFIG_HOME:-$HOME/.config}/skillarium/config.json"
mkdir -p -- "$(dirname -- "$config")"
if [[ -f "$config" ]]; then current=$(cat -- "$config"); else current='{}'; fi
jq --arg library "$path" '.library = $library' <<<"$current" >"$config.new"
mv -f -- "$config.new" "$config"

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
echo "CONNECTED: $url in $path"
