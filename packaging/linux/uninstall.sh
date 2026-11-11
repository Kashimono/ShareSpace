#!/usr/bin/env bash
set -euo pipefail

DATA_HOME="${XDG_DATA_HOME:-${HOME:?HOME must be set}/.local/share}"
BIN_HOME="${XDG_BIN_HOME:-${HOME:?HOME must be set}/.local/bin}"
APP_DIR="${DATA_HOME%/}/sharespace/app"
BIN_PATH="${BIN_HOME%/}/sharespace"
DESKTOP_PATH="${DATA_HOME%/}/applications/sharespace.desktop"
ICON_PATH="${DATA_HOME%/}/icons/hicolor/256x256/apps/org.sharespace.ShareSpace.png"
MARKER='.sharespace-install'

case "$DATA_HOME" in /*) ;; *) echo 'XDG_DATA_HOME must be an absolute path.' >&2; exit 2 ;; esac
case "$BIN_HOME" in /*) ;; *) echo 'XDG_BIN_HOME must be an absolute path.' >&2; exit 2 ;; esac
if [[ ! -f "$APP_DIR/$MARKER" ]]; then
    echo 'The ShareSpace installation marker was not found; no program files were removed.' >&2
    exit 2
fi
if [[ -f "$BIN_PATH" ]] && grep -q '^# sharespace-managed-launcher$' "$BIN_PATH"; then
    rm -- "$BIN_PATH"
fi
if [[ -f "$DESKTOP_PATH" ]] && grep -q '^Name=ShareSpace$' "$DESKTOP_PATH"; then
    rm -- "$DESKTOP_PATH"
fi
rm -f -- "$ICON_PATH"
rm -rf -- "$APP_DIR"
echo 'ShareSpace program files and desktop integration were removed. Application settings and history were kept.'
