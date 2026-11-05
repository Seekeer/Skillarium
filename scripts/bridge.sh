# Bridge of the legacy layout (the engine inside the library clone) to an
# installed Skillarium, sourced by install.sh. Once Skillarium is installed and
# configured with this clone, the hourly sync runs current/install.sh, and the
# engine files can leave the library. Failures only postpone the bridge.
# SKILLARIUM_NO_BRIDGE=1 switches it off.

# Prints the installed engine folder (…/current); fails when it is not available yet.
skillarium_bridge() {
    local library=$1 home releases script config current
    home=${SKILLARIUM_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/skillarium}
    releases=${SKILLARIUM_RELEASES:-https://github.com/Seekeer/Skillarium/releases}
    if [[ ! -f "$home/current/VERSION" ]]; then
        script=$(mktemp) || return 1
        if ! curl -fsSL --retry 2 -o "$script" "$releases/latest/download/skillarium.sh" ||
            ! SKILLARIUM_HOME="$home" SKILLARIUM_LIBRARY="$library" bash "$script" install >&2; then
            rm -f -- "$script"
            echo 'Skillarium bridge postponed: the engine could not be installed; the next sync retries.' >&2
            return 1
        fi
        rm -f -- "$script"
    fi
    config="${XDG_CONFIG_HOME:-$HOME/.config}/skillarium/config.json"
    current=''
    [[ -f "$config" ]] && current=$(jq -r '.library // empty' "$config" 2>/dev/null || true)
    if [[ -z "$current" ]]; then
        mkdir -p -- "$(dirname -- "$config")"
        if [[ -f "$config" ]]; then
            jq --arg library "$library" '.library = $library' "$config" >"$config.new"
        else
            jq -n --arg library "$library" '{library: $library}' >"$config.new"
        fi
        mv -f -- "$config.new" "$config"
    fi
    printf '%s\n' "$home/current"
}
