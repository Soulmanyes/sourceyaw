#!/bin/sh
# Installs Source Yaw for the current user.
#   - installs Python/GTK 4/libadwaita/Pillow if missing (asks for your password)
#   - copies the app to ~/.local/share/sourceyaw and adds a launcher
#   - the colour changer runs from a Steam launch option (yaw-run), see the app
# Run ./install.sh --uninstall to remove it again.
# Without cloning the repo:
#   curl -fsSL https://raw.githubusercontent.com/Soulmanyes/sourceyaw/main/install.sh | sh
# The whole script is one { } block, so a download cut off midway runs nothing.
{
set -eu

NAME=sourceyaw
APP_ID=io.github.soulmanyes.SourceYaw
# names from before 1.2.0, when the app was called CS:S Yaw
OLD_NAME=css-yaw
OLD_APP_ID=io.github.soulmanyes.CssYaw
SRC=$(cd "$(dirname "$0")" && pwd)
DEST=${XDG_DATA_HOME:-$HOME/.local/share}/$NAME
APPS=${XDG_DATA_HOME:-$HOME/.local/share}/applications
BIN=$HOME/.local/bin
ICONS=${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps
UNITS=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user   # only to remove the old service

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# run through curl | sh: the repo isn't next to the script, so fetch it and run its installer
if [ ! -f "$SRC/yawcore.py" ]; then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    say "Downloading Source Yaw"
    # the latest release; main if there is none yet. releases/latest redirects to .../tag/<tag>
    tag=$(curl -fsSLo /dev/null -w '%{url_effective}' \
        https://github.com/Soulmanyes/sourceyaw/releases/latest | sed -n 's|.*/tag/||p')
    ref=${tag:+tags/$tag}
    curl -fsSL "https://github.com/Soulmanyes/sourceyaw/archive/refs/${ref:-heads/main}.tar.gz" |
        tar -xz -C "$tmp" --strip-components=1
    sh "$tmp/install.sh" "$@" < /dev/tty
    exit
fi

if [ "${1:-}" = "--uninstall" ]; then
    say "Removing Source Yaw"
    if [ -t 0 ] && [ -f "$DEST/yawcore.py" ]; then
        printf 'Also remove yaws.cfg and its line in autoexec.cfg from your games? [y/N] '
        read -r answer
        case $answer in
            [yY]*) python3 "$DEST/yawcore.py" --remove-from-game && echo "Removed from your games." ;;
        esac
    fi
    # $DEST also holds KROSSHAIR if the app installed it; drop its layer manifest too,
    # or the Vulkan loader is left pointing at a missing library
    rm -rf "$DEST" "$APPS/$NAME.desktop" "$APPS/$APP_ID.desktop" "$ICONS/$APP_ID.svg" \
        "$BIN/$NAME" "$BIN/$NAME-run" "$BIN/$OLD_NAME-run" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/vulkan/implicit_layer.d/$NAME-krosshair.json" \
        "${XDG_DATA_HOME:-$HOME/.local/share}/vulkan/implicit_layer.d/$NAME-overlay.json"
    say "Done. Your settings stay in ~/.config/$NAME."
    if [ -e /etc/systemd/system/sourceyaw-inputd.socket ]; then
        echo "The strafe trainer's input service is still installed. Remove it with:"
        echo "    curl -fsSL https://raw.githubusercontent.com/Soulmanyes/sourceyaw/main/inputd/install-inputd.sh | sudo sh -s remove"
    fi
    echo "Remove $NAME-run (or $OLD_NAME-run) from the launch options, or the game will not start."
    exit 0
fi

have_deps() {
    python3 - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw
from gi.repository import Gtk
import PIL
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 1)
assert (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 6)
PY
}

if ! have_deps; then
    say "Installing Python, GTK 4, libadwaita and Pillow"
    if command -v pacman >/dev/null; then
        sudo pacman -S --needed --noconfirm python python-gobject gtk4 libadwaita python-pillow
    elif command -v apt-get >/dev/null; then
        sudo apt-get update
        sudo apt-get install -y python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-pil
    elif command -v dnf >/dev/null; then
        sudo dnf install -y python3 python3-gobject gtk4 libadwaita python3-pillow
    else
        echo "Unknown package manager. Install Python 3, PyGObject, GTK 4," >&2
        echo "libadwaita 1.1 or newer and Pillow yourself, then run this again." >&2
        exit 1
    fi
    have_deps || { echo "GTK 4.6 and libadwaita 1.1 or newer are needed." >&2; exit 1; }
fi

# older versions ran the colour changer as an always-on systemd service;
# stop it before replacing its files (its stop hook runs the old script)
if [ -e "$UNITS/$OLD_NAME.service" ]; then
    say "Removing the old background service (the launch option replaces it)"
    systemctl --user disable --now $OLD_NAME.service 2>/dev/null || true
    rm -f "$UNITS/$OLD_NAME.service"
    systemctl --user daemon-reload
fi

# move an install from before the rename (CS:S Yaw -> Source Yaw)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
OLD_DEST=$DATA/$OLD_NAME
LAYERS=$DATA/vulkan/implicit_layer.d
if [ -d "$OLD_DEST" ]; then
    say "Moving CS:S Yaw to its new name, Source Yaw"
    mkdir -p "$DEST"
    # a KROSSHAIR the app installed: move it and repoint its layer file, so the
    # Vulkan loader isn't left pointing at a missing library
    if [ -d "$OLD_DEST/krosshair" ] && [ ! -e "$DEST/krosshair" ]; then
        mv "$OLD_DEST/krosshair" "$DEST/krosshair"
        if [ -f "$LAYERS/$OLD_NAME-krosshair.json" ]; then
            sed "s|$OLD_DEST/krosshair|$DEST/krosshair|" "$LAYERS/$OLD_NAME-krosshair.json" \
                > "$LAYERS/$NAME-krosshair.json"
            rm -f "$LAYERS/$OLD_NAME-krosshair.json"
        fi
    fi
    rm -rf "$OLD_DEST"
fi
OLD_CFG=${XDG_CONFIG_HOME:-$HOME/.config}/$OLD_NAME
NEW_CFG=${XDG_CONFIG_HOME:-$HOME/.config}/$NAME
if [ -d "$OLD_CFG" ] && [ ! -e "$NEW_CFG" ]; then
    mv "$OLD_CFG" "$NEW_CFG"
fi
rm -rf "$APPS/$OLD_APP_ID.desktop" "$APPS/$OLD_NAME.desktop" "$ICONS/$OLD_APP_ID.svg" \
    "$BIN/$OLD_NAME" "${XDG_CACHE_HOME:-$HOME/.cache}/$OLD_NAME"

say "Installing the app to $DEST"
mkdir -p "$DEST" "$APPS" "$BIN" "$ICONS"
install -m 644 "$SRC/yawcore.py" "$SRC/trainer.py" "$SRC/updater.py" "$DEST/"
# the input service's files; its installer (run with sudo) copies them to root-owned places
mkdir -p "$DEST/inputd"
install -m 644 "$SRC"/inputd/sourceyaw-inputd.* "$SRC/inputd/install-inputd.sh" "$DEST/inputd/"
install -m 755 "$SRC/app.py" "$SRC/yaw-watch.sh" "$SRC/yaw-run" "$DEST/"
cp -r "$SRC/icons" "$DEST/"
install -m 644 "$SRC/icons/hicolor/scalable/apps/$APP_ID.svg" "$ICONS/"
ln -sf "$DEST/app.py" "$BIN/$NAME"
# the launch option command; Steam has ~/.local/bin in its PATH
ln -sf "$DEST/yaw-run" "$BIN/$NAME-run"
# its name before 1.2.0, so launch options that still say css-yaw-run keep working
ln -sf "$DEST/yaw-run" "$BIN/$OLD_NAME-run"

# named after the app ID so docks and taskbars match the window to this launcher
rm -f "$APPS/$NAME.desktop"
cat > "$APPS/$APP_ID.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Source Yaw
Comment=Cycle cl_yawspeed in CS:S or Momentum Mod and see it on your crosshair
Exec=python3 $DEST/app.py
Icon=$APP_ID
Categories=Game;Utility;
Keywords=counter-strike;css;momentum;yawspeed;crosshair;surf;
DESKTOP

# the strafe trainer's on-screen number: a small Vulkan layer, built here
say "Building the strafe trainer overlay"
if command -v cc >/dev/null && make -C "$SRC/overlay" >/dev/null; then
    mkdir -p "$DEST/overlay" "$LAYERS"
    install -m 755 "$SRC/overlay/lib/$NAME-overlay.so" "$DEST/overlay/"
    sed "s|@LIBRARY@|$DEST/overlay/$NAME-overlay.so|" "$SRC/overlay/$NAME-overlay.json.in" \
        > "$LAYERS/$NAME-overlay.json"
else
    echo "Skipped: needs a C compiler and make. The trainer still plays its sound," >&2
    echo "but can't show the offset in game." >&2
fi

say "Writing yaws.cfg and crosshair colours from your settings"
python3 "$DEST/yawcore.py" --apply || echo "Skipped. Open the app and press Apply."

say "Done. Open \"Source Yaw\" from your app menu, or run: $NAME"
echo "Add this to the CS:S launch options, right before %command%:"
echo "    $NAME-run"
}
