#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_SOURCE="$SCRIPT_DIR/app"
DATA_HOME="${XDG_DATA_HOME:-${HOME:?HOME must be set}/.local/share}"
BIN_HOME="${XDG_BIN_HOME:-${HOME:?HOME must be set}/.local/bin}"
APP_DIR="${DATA_HOME%/}/sharespace/app"
BIN_PATH="${BIN_HOME%/}/sharespace"
DESKTOP_DIR="${DATA_HOME%/}/applications"
DESKTOP_PATH="$DESKTOP_DIR/sharespace.desktop"
ICON_DIR="${DATA_HOME%/}/icons/hicolor/256x256/apps"
ICON_PATH="$ICON_DIR/org.sharespace.ShareSpace.png"
MARKER='.sharespace-install'

case "$DATA_HOME" in /*) ;; *) echo 'XDG_DATA_HOME must be an absolute path.' >&2; exit 2 ;; esac
case "$BIN_HOME" in /*) ;; *) echo 'XDG_BIN_HOME must be an absolute path.' >&2; exit 2 ;; esac
if [[ ! -x "$APP_SOURCE/ShareSpace" || ! -f "$SCRIPT_DIR/assets/sharespace.png" ]]; then
    echo 'The package is incomplete. Extract the full ShareSpace distribution before installing.' >&2
    exit 2
fi

mkdir -p "$(dirname -- "$APP_DIR")" "$BIN_HOME" "$DESKTOP_DIR" "$ICON_DIR"
STAGE="${APP_DIR}.stage.$$"
BACKUP="${APP_DIR}.backup.$$"
if [[ -e "$APP_DIR" && ! -f "$APP_DIR/$MARKER" ]]; then
    echo 'The target directory exists and is not managed by ShareSpace.' >&2
    exit 2
fi
if [[ -e "$STAGE" || -e "$BACKUP" ]]; then
    echo 'A previous install operation left a staging directory; review it before retrying.' >&2
    exit 2
fi

cleanup() {
    # STAGE was checked absent above and is unique to this install process.
    # Clean partial copies too, even when copying failed before writing MARKER.
    if [[ -e "$STAGE" || -L "$STAGE" ]]; then
        rm -rf -- "$STAGE"
    fi
}
trap cleanup EXIT
mkdir -p "$STAGE"
cp -a "$APP_SOURCE/." "$STAGE/"
printf '%s\n' 'ShareSpace 1.0.0' > "$STAGE/$MARKER"
if [[ -d "$APP_DIR" ]]; then
    mv -- "$APP_DIR" "$BACKUP"
fi
if ! mv -- "$STAGE" "$APP_DIR"; then
    if [[ -d "$BACKUP" && ! -e "$APP_DIR" ]]; then
        mv -- "$BACKUP" "$APP_DIR"
    fi
    echo 'ShareSpace could not activate the staged installation.' >&2
    exit 1
fi
if [[ -d "$BACKUP" && -f "$BACKUP/$MARKER" ]]; then
    rm -rf -- "$BACKUP"
fi

cat > "$BIN_PATH" <<'LAUNCHER'
#!/usr/bin/env sh
# sharespace-managed-launcher
data_home="${XDG_DATA_HOME:-${HOME:?HOME must be set}/.local/share}"
exec "$data_home/sharespace/app/ShareSpace" "$@"
LAUNCHER
chmod 755 "$BIN_PATH"
cp -- "$SCRIPT_DIR/assets/sharespace.png" "$ICON_PATH"
desktop_exec="$(printf '%s' "$BIN_PATH" | sed 's/\\/\\\\/g; s/"/\\"/g')"
desktop_path="$(printf '%s' "$APP_DIR" | sed 's/\\/\\\\/g; s/"/\\"/g')"
cat > "$DESKTOP_PATH" <<DESKTOP
[Desktop Entry]
Type=Application
Name=ShareSpace
Comment=Offline-first local file sharing
Exec="$desktop_exec"
Path="$desktop_path"
Icon=org.sharespace.ShareSpace
Terminal=false
Categories=Utility;FileTransfer;
DESKTOP

echo "ShareSpace 1.0.0 installed to $APP_DIR"
echo "Launch it from the desktop menu or run: $BIN_PATH"
echo 'Application settings and history remain in the OS application-data location.'
