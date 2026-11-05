#!/usr/bin/env bash
# Skillarium engine manager and bootstrap for Linux.
#
#   curl -fsSL https://github.com/Seekeer/Skillarium/releases/latest/download/skillarium.sh | bash -s -- server --repo <url>
#   curl -fsSL .../skillarium.sh | bash -s -- client --library <url>
#   skillarium status | self-update [--channel stable|beta] | rollback | install [--version <v>]
#
# The engine of each release lives in $SKILLARIUM_HOME/versions/<version>/;
# current is a symlink switched in one rename, and schedules and the skillarium
# command always go through it. Two previous versions are kept for rollback.
set -euo pipefail

releases=${SKILLARIUM_RELEASES:-https://github.com/Seekeer/Skillarium/releases}
api=${SKILLARIUM_API:-https://api.github.com/repos/Seekeer/Skillarium}
home=${SKILLARIUM_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/skillarium}
bin_dir=${SKILLARIUM_BIN:-$HOME/.local/bin}
keep=2
version_pattern='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$'

die() { echo "skillarium: $*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: skillarium <command>
  status                         engine version, channel, library format and mode
  self-update [--channel C]      install the newest release of the channel (stable|beta)
  rollback                       return to the previous version and skip the current one
  install [--version V] [--channel C]
                                 install a release (first installation or a fixed version)
  enable-updates | disable-updates
                                 hourly self-update timer (machines without the app)
  server ...                     set up the library server (see: skillarium server --help)
  client ...                     connect this machine to a library (see: skillarium client --help)
EOF
}

# --- JSON through jq or python3, whichever the machine has -------------------

have() { command -v "$1" >/dev/null 2>&1; }

# python3 output without carriage returns (Python for Windows under Git Bash).
py() { python3 "$@" | tr -d '\r'; }

# Feed fields of the Linux archive: version, min format, max format, url, sha512, size.
feed_fields() {
    if have jq; then
        jq -r '[.version, (.format.min // 1), .format.max, .engine.linux.url,
                .engine.linux.sha512, .engine.linux.size] | map(tostring) | join("\t")'
    elif have python3; then
        py -c 'import json, sys
d = json.load(sys.stdin); e = d["engine"]["linux"]; f = d["format"]
print("\t".join(str(v) for v in (d["version"], f.get("min", 1), f["max"], e["url"], e["sha512"], e["size"])))'
    else
        die 'jq or python3 is required'
    fi
}

# Tag of the newest published release, pre-releases included (the beta channel).
newest_tag() {
    if have jq; then
        jq -r '[.[] | select(.draft | not)][0].tag_name // empty'
    else
        py -c 'import json, sys
items = [r for r in json.load(sys.stdin) if not r.get("draft")]
print(items[0]["tag_name"] if items else "")'
    fi
}

json_field() { # json_field <key> < document: a top-level string field, empty when absent
    if have jq; then
        jq -r --arg k "$1" '.[$k] // empty | tostring'
    else
        py -c 'import json, sys
v = json.load(sys.stdin).get(sys.argv[1])
print("" if v is None else v)' "$1"
    fi
}

# --- Versions -----------------------------------------------------------------

valid_version() { [[ "$1" =~ $version_pattern ]]; }

# Semantic version precedence: prints -1, 0 or 1.
version_cmp() {
    local a=$1 b=$2 ap='' bp='' i
    local -a x y
    [[ "$a" == *-* ]] && ap=${a#*-}
    [[ "$b" == *-* ]] && bp=${b#*-}
    IFS=. read -r -a x <<<"${a%%-*}"
    IFS=. read -r -a y <<<"${b%%-*}"
    for i in 0 1 2; do
        if ((10#${x[i]} != 10#${y[i]})); then
            if ((10#${x[i]} < 10#${y[i]})); then echo -1; else echo 1; fi
            return
        fi
    done
    if [[ "$ap" == "$bp" ]]; then echo 0; return; fi
    if [[ -z "$ap" ]]; then echo 1; return; fi
    if [[ -z "$bp" ]]; then echo -1; return; fi
    IFS=. read -r -a x <<<"$ap"
    IFS=. read -r -a y <<<"$bp"
    for ((i = 0; i < ${#x[@]} || i < ${#y[@]}; i++)); do
        if ((i >= ${#x[@]})); then echo -1; return; fi
        if ((i >= ${#y[@]})); then echo 1; return; fi
        [[ "${x[i]}" == "${y[i]}" ]] && continue
        if [[ "${x[i]}" =~ ^[0-9]+$ && "${y[i]}" =~ ^[0-9]+$ ]]; then
            if ((10#${x[i]} < 10#${y[i]})); then echo -1; else echo 1; fi
        elif [[ "${x[i]}" =~ ^[0-9]+$ ]]; then echo -1
        elif [[ "${y[i]}" =~ ^[0-9]+$ ]]; then echo 1
        elif [[ "${x[i]}" < "${y[i]}" ]]; then echo -1
        else echo 1; fi
        return
    done
    echo 0
}

current_version() {
    [[ -f "$home/current/VERSION" ]] && tr -d '[:space:]' <"$home/current/VERSION"
}

channel() {
    local value
    value=$(cat -- "$home/channel" 2>/dev/null || true)
    case "$value" in stable|beta) echo "$value" ;; *) echo stable ;; esac
}

# --- Library (read only here: its location and format) -------------------------

skillarium_config() { printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/skillarium/config.json"; }

library_path() {
    local config
    if [[ -n "${SKILLARIUM_LIBRARY:-}" ]]; then echo "$SKILLARIUM_LIBRARY"; return; fi
    config=$(skillarium_config)
    [[ -f "$config" ]] && json_field library <"$config"
    return 0
}

# Highest format of the library working tree and its shared branch; empty without a library.
library_format() {
    local library=$1 text value best=''
    [[ -n "$library" && -f "$library/agent-skills.json" ]] || return 0
    for text in "$(cat -- "$library/agent-skills.json")" \
                "$(git -C "$library" show '@{upstream}:agent-skills.json' 2>/dev/null || true)"; do
        [[ -n "$text" ]] || continue
        value=$(json_field format <<<"$text") || continue
        [[ -n "$value" ]] || value=1
        [[ "$value" =~ ^[1-9][0-9]*$ ]] || continue
        if [[ -z "$best" ]] || ((value > best)); then best=$value; fi
    done
    echo "$best"
}

# --- Installation -------------------------------------------------------------

lock() {
    mkdir -p -- "$home/versions"
    exec 8>"$home/.lock"
    have flock || die 'flock (util-linux) is required'
    flock -n 8 || die 'another Skillarium update is running'
}

# Reads the feed of a channel or of one version; sets f_version f_min f_max f_url f_sha f_size.
read_feed() {
    local wanted_channel=$1 wanted_version=${2:-} url document tag
    if [[ -n "$wanted_version" ]]; then
        valid_version "$wanted_version" || die "invalid version: $wanted_version"
        url="$releases/download/v$wanted_version/latest.json"
    elif [[ "$wanted_channel" == beta ]]; then
        document=$(curl -fsSL --retry 2 -H 'Accept: application/vnd.github+json' "$api/releases?per_page=20") ||
            die 'cannot list Skillarium releases'
        tag=$(newest_tag <<<"$document")
        [[ -n "$tag" ]] || die 'no Skillarium releases published'
        url="$releases/download/$tag/latest.json"
    else
        url="$releases/latest/download/latest.json"
    fi
    document=$(curl -fsSL --retry 2 "$url") || die "cannot read $url"
    IFS=$'\t' read -r f_version f_min f_max f_url f_sha f_size < <(feed_fields <<<"$document") ||
        die "invalid release feed: $url"
    valid_version "$f_version" || die "invalid version in the feed: $f_version"
    f_document=$document
    [[ "$f_min" =~ ^[1-9][0-9]*$ && "$f_max" =~ ^[1-9][0-9]*$ && "$f_size" =~ ^[0-9]+$ && "$f_sha" =~ ^[0-9a-f]{128}$ ]] ||
        die "invalid release feed: $url"
}

# The release must read the library: its formats cover the library's format.
compatible() {
    local format
    format=$(library_format "$(library_path)")
    [[ -z "$format" ]] && return 0
    if ((format > f_max)); then
        echo "Skillarium $f_version supports library format up to $f_max; the library has $format. Skipped." >&2
        return 1
    fi
    if ((format < f_min)); then
        echo "Skillarium $f_version needs library format $f_min or newer; the library has $format. Skipped." >&2
        return 1
    fi
}

# Downloads, verifies and unpacks f_version into versions/ unless it is already there.
fetch_version() {
    local target="$home/versions/$f_version" incoming archive digest size
    [[ -f "$target/VERSION" ]] && return 0
    incoming=$(mktemp -d "$home/versions/.incoming-XXXXXX")
    archive="$incoming/engine.tar.gz"
    if ! curl -fsSL --retry 2 -o "$archive" "$f_url"; then
        rm -rf -- "$incoming"; die "download failed: $f_url"
    fi
    size=$(wc -c <"$archive" | tr -d '[:space:]')
    digest=$(sha512sum <"$archive" | cut -d" " -f1)
    if [[ "$size" != "$f_size" || "$digest" != "$f_sha" ]]; then
        rm -rf -- "$incoming"; die "checksum mismatch for $f_url; nothing changed"
    fi
    mkdir -- "$incoming/engine"
    if ! tar -xzf - -C "$incoming/engine" <"$archive" ||
        [[ "$(tr -d '[:space:]' <"$incoming/engine/VERSION")" != "$f_version" ]]; then
        rm -rf -- "$incoming"; die "broken archive: $f_url"
    fi
    # The feed stays with the version: the server pins the library's PR check to it.
    echo "$f_document" >"$incoming/engine/release.json"
    rm -rf -- "$target"
    mv -- "$incoming/engine" "$target"
    rm -rf -- "$incoming"
}

# Points current at versions/<v> in one rename and records the activation order.
activate() {
    local version=$1 history="$home/history"
    ln -sfn "versions/$version" "$home/current.new"
    mv -Tf -- "$home/current.new" "$home/current"
    { grep -vxF -- "$version" "$history" 2>/dev/null || true; echo "$version"; } >"$history.new"
    mv -f -- "$history.new" "$history"
}

# Keeps the current version and the two most recently active other versions.
prune() {
    local current kept=() version folder
    current=$(current_version)
    if [[ -f "$home/history" ]]; then
        while IFS= read -r version; do
            [[ -n "$version" && "$version" != "$current" ]] || continue
            ((${#kept[@]} < keep)) && kept+=("$version")
        done < <(tac -- "$home/history")
    fi
    for folder in "$home/versions"/*/; do
        [[ -d "$folder" ]] || continue
        version=$(basename -- "$folder")
        [[ "$version" == "$current" ]] && continue
        [[ " ${kept[*]-} " == *" $version "* ]] && continue
        rm -rf -- "$folder"
        grep -vxF -- "$version" "$home/history" >"$home/history.new" || true
        mv -f -- "$home/history.new" "$home/history"
    done
}

install_command() {
    local quoted
    quoted=$(printf '%q' "$home")
    mkdir -p -- "$bin_dir"
    cat >"$bin_dir/skillarium.new" <<EOF
#!/usr/bin/env bash
# Managed by Skillarium
export SKILLARIUM_HOME=$quoted
exec bash "\$SKILLARIUM_HOME/current/scripts/skillarium.sh" "\$@"
EOF
    chmod 755 "$bin_dir/skillarium.new"
    mv -f -- "$bin_dir/skillarium.new" "$bin_dir/skillarium"
    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *) echo "Add $bin_dir to PATH to run skillarium from any shell." >&2 ;;
    esac
}

cmd_install() {
    local wanted_channel wanted_version=''
    wanted_channel=$(channel)
    while (($#)); do
        case "$1" in
            --version) wanted_version=${2:?--version needs a value}; shift 2 ;;
            --channel) wanted_channel=${2:?--channel needs a value}; shift 2 ;;
            *) die "unknown option: $1" ;;
        esac
    done
    [[ "$wanted_channel" == stable || "$wanted_channel" == beta ]] || die "unknown channel: $wanted_channel"
    lock
    read_feed "$wanted_channel" "$wanted_version"
    compatible || exit 1
    fetch_version
    activate "$f_version"
    echo "$wanted_channel" >"$home/channel"
    rm -f -- "$home/skip"
    install_command
    prune
    echo "INSTALLED: Skillarium $f_version in $home/current"
}

cmd_self_update() {
    local wanted_channel current order
    wanted_channel=$(channel)
    while (($#)); do
        case "$1" in
            --channel) wanted_channel=${2:?--channel needs a value}; shift 2 ;;
            *) die "unknown option: $1" ;;
        esac
    done
    [[ "$wanted_channel" == stable || "$wanted_channel" == beta ]] || die "unknown channel: $wanted_channel"
    lock
    echo "$wanted_channel" >"$home/channel"
    current=$(current_version || true)
    read_feed "$wanted_channel"
    if [[ -n "$current" ]]; then
        order=$(version_cmp "$f_version" "$current")
        if ((order <= 0)); then echo "Up to date: Skillarium $current ($wanted_channel)"; return 0; fi
    fi
    if [[ "$f_version" == "$(cat -- "$home/skip" 2>/dev/null || true)" ]]; then
        echo "Skillarium $f_version was rolled back; skipped until a newer release." >&2; return 0
    fi
    compatible || return 0
    fetch_version
    activate "$f_version"
    prune
    echo "UPDATED: Skillarium ${current:-none} -> $f_version"
}

cmd_rollback() {
    local current previous='' version skipped
    lock
    current=$(current_version) || die 'Skillarium is not installed'
    skipped=$(cat -- "$home/skip" 2>/dev/null || true)
    if [[ -f "$home/history" ]]; then
        while IFS= read -r version; do
            # A version rolled back before is not a target either.
            if [[ -n "$version" && "$version" != "$current" && "$version" != "$skipped" &&
                  -f "$home/versions/$version/VERSION" ]]; then
                previous=$version; break
            fi
        done < <(tac -- "$home/history")
    fi
    [[ -n "$previous" ]] || die 'no previous version to roll back to'
    echo "$current" >"$home/skip"
    activate "$previous"
    echo "ROLLED BACK: Skillarium $current -> $previous; $current is skipped until a newer release"
}

cmd_status() {
    local current library format supported mode
    current=$(current_version || true)
    echo "Skillarium: ${current:-not installed} ($(channel)) in $home"
    if [[ -d "$home/versions" ]]; then
        echo "Installed versions: $(cd -- "$home/versions" && ls -1 | tr '\n' ' ')"
    fi
    [[ -f "$home/skip" ]] && echo "Skipped after rollback: $(cat -- "$home/skip")"
    library=$(library_path)
    if [[ -z "$library" ]]; then echo 'Library: not configured'; return 0; fi
    format=$(library_format "$library")
    supported=$(sed -n 's/^supported_format=\([0-9]*\)$/\1/p' "$home/current/scripts/locate.sh" 2>/dev/null || true)
    mode=normal
    [[ -n "$format" && -n "$supported" ]] && ((format > supported)) && mode='read-only (update Skillarium)'
    echo "Library: $library; format ${format:-unknown}, engine supports ${supported:-unknown}; mode: $mode"
}

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
# A value for a systemd unit line: quoted, with \, ", % and $ escaped (as in scripts/locate.sh,
# which this file cannot source: it also runs before any engine is installed).
unit_quote() {
    local value=$1
    value=${value//\\/\\\\}; value=${value//\"/\\\"}
    value=${value//%/%%}; value=${value//\$/\$\$}
    printf '"%s"' "$value"
}
update_marker='# Managed by Skillarium self-update'

cmd_enable_updates() {
    local unit
    for unit in skillarium-update.service skillarium-update.timer; do
        if [[ -e "$unit_dir/$unit" ]] && [[ $(head -n 1 "$unit_dir/$unit") != "$update_marker" ]]; then
            die "existing unmanaged unit preserved: $unit_dir/$unit"
        fi
    done
    mkdir -p -- "$unit_dir"
    cat >"$unit_dir/skillarium-update.service" <<EOF
$update_marker
[Unit]
Description=Update the Skillarium engine
[Service]
Type=oneshot
Environment=$(unit_quote "SKILLARIUM_HOME=$home")
ExecStart=/usr/bin/bash $(unit_quote "$home/current/scripts/skillarium.sh") self-update
TimeoutStartSec=10min
EOF
    cat >"$unit_dir/skillarium-update.timer" <<EOF
$update_marker
[Unit]
Description=Update the Skillarium engine hourly
[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=15min
[Install]
WantedBy=timers.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now skillarium-update.timer
    echo 'Hourly Skillarium self-update enabled: systemctl --user status skillarium-update.timer'
}

cmd_disable_updates() {
    local unit
    systemctl --user disable --now skillarium-update.timer 2>/dev/null || true
    for unit in skillarium-update.service skillarium-update.timer; do
        if [[ -f "$unit_dir/$unit" ]] && [[ $(head -n 1 "$unit_dir/$unit") == "$update_marker" ]]; then
            rm -- "$unit_dir/$unit"
        fi
    done
    systemctl --user daemon-reload 2>/dev/null || true
    echo 'Skillarium self-update disabled.'
}

# Role setup always runs the installed engine, installing it first when missing.
run_role() {
    local script=$1
    shift
    if [[ ! -f "$home/current/VERSION" ]]; then
        (cmd_install) || exit 1
    fi
    exec bash "$home/current/$script" "$@"
}

command=${1:-help}
(($#)) && shift
case "$command" in
    status) cmd_status ;;
    self-update) cmd_self_update "$@" ;;
    rollback) cmd_rollback ;;
    install) cmd_install "$@" ;;
    enable-updates) cmd_enable_updates ;;
    disable-updates) cmd_disable_updates ;;
    server) run_role server/setup.sh "$@" ;;
    client) run_role scripts/setup-client.sh "$@" ;;
    help|--help|-h) usage ;;
    *) usage >&2; exit 2 ;;
esac
