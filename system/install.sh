#!/usr/bin/env bash
# Prism system setup for Fedora: installs the omen-rgb-keyboard driver with
# DKMS, retires hp_wmi, and hands the driver's lighting and fan controls to
# the user who ran sudo, so Prism works right away: no sudo, no re-login.
#
# Usage: sudo system/install.sh [path-to-omen-rgb-keyboard-source]
# Without a path, the driver version Prism was tested with is downloaded.
set -euo pipefail

# OmenLinux's driver plus Prism's fixes (Linux 7.x state saving, the live "frame"
# lighting file for music lighting, single-zone brightness), until they are upstream.
DRIVER_REPO=https://github.com/Insidexa/omen-rgb-keyboard.git
DRIVER_COMMIT=75739ec5583045c57d5f3732b542820a464b0b69
SRC="${1:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR=/var/lib/omen-rgb-keyboard
DEV=/sys/devices/platform/omen-rgb-keyboard

die() { echo "error: $*" >&2; exit 1; }
step() { echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run with sudo"
USER_NAME="${SUDO_USER:-}"
[[ -n $USER_NAME && $USER_NAME != root ]] || die "run with sudo from your own account, not as root"
if [[ -z $SRC ]]; then
    command -v git >/dev/null || dnf install -y git
    SRC=$(mktemp -d)
    trap 'rm -rf "$SRC"' EXIT
    step "Downloading omen-rgb-keyboard (${DRIVER_COMMIT:0:7})"
    git -C "$SRC" init -q
    git -C "$SRC" fetch -q --depth 1 "$DRIVER_REPO" "$DRIVER_COMMIT"
    git -C "$SRC" checkout -q FETCH_HEAD
fi
[[ -f $SRC/dkms.conf ]] || die "driver source not found at $SRC"

NAME=$(sed -n 's/^PACKAGE_NAME="\?\([^"]*\)"\?$/\1/p' "$SRC/dkms.conf")
VER=$(sed -n 's/^PACKAGE_VERSION="\?\([^"]*\)"\?$/\1/p' "$SRC/dkms.conf")
[[ -n $NAME && -n $VER ]] || die "could not read name/version from dkms.conf"

step "Checking build tools"
pkgs=()
rpm -q dkms >/dev/null || pkgs+=(dkms)
rpm -q "kernel-devel-$(uname -r)" >/dev/null || pkgs+=("kernel-devel-$(uname -r)")
if ((${#pkgs[@]})); then dnf install -y "${pkgs[@]}"; fi

step "Installing $NAME $VER with DKMS"
# Drop every registered version so a rebuilt copy of the same version is used.
for v in $(dkms status "$NAME" 2>/dev/null | sed -n "s|^$NAME/\([^,:]*\).*|\1|p" | sort -u); do
    dkms remove "$NAME/$v" --all || true
done
rm -rf "/usr/src/$NAME-$VER"
mkdir -p "/usr/src/$NAME-$VER"
# A private copy, so DKMS can still rebuild on kernel updates if the clone moves.
tar -C "$SRC" --exclude=.git --exclude='*.ko' --exclude='*.o' -cf - . | tar -C "/usr/src/$NAME-$VER" -xf -
dkms install "$NAME/$VER"

step "Blacklisting hp_wmi (the Omen driver replaces it)"
cat > /etc/modprobe.d/prism-hp-wmi-blacklist.conf <<'EOF'
# Installed by Prism: omen_rgb_keyboard owns the HP WMI interface on this laptop.
blacklist hp_wmi
EOF
echo omen_rgb_keyboard > /etc/modules-load.d/omen_rgb_keyboard.conf
if lsinitrd 2>/dev/null | grep -q 'hp-wmi\.ko'; then
    step "hp_wmi is in the initramfs, rebuilding it"
    dracut -f
fi

step "Creating state directory $STATE_DIR"
install -d -m 0755 "$STATE_DIR"

step "Giving $USER_NAME control of the Omen settings"
sed "s/@USER@/$USER_NAME/g" "$HERE/70-prism-omen.rules" > /etc/udev/rules.d/70-prism-omen.rules
chmod 0644 /etc/udev/rules.d/70-prism-omen.rules
install -m 0644 "$HERE/71-prism-omen-key.hwdb" /etc/udev/hwdb.d/71-prism-omen-key.hwdb
install -m 0644 "$HERE/80-prism-runtime-pm.rules" /etc/udev/rules.d/80-prism-runtime-pm.rules
install -m 0644 "$HERE/prism-dracut.conf" /etc/dracut.conf.d/prism-omen.conf
dracut -f --regenerate-all || echo "dracut failed: run sudo dracut -f --regenerate-all" >&2
systemd-hwdb update
# udevd keeps the database it loaded: reload it (and the rules) before re-applying.
udevadm control --reload
udevadm trigger --subsystem-match=input --action=change

step "Switching drivers"
modprobe -r omen_rgb_keyboard 2>/dev/null || true
modprobe -r hp_wmi 2>/dev/null || true
modprobe sparse_keymap
modprobe omen_rgb_keyboard
udevadm settle -t 10 || true
# The bind event normally applies ownership; do it directly as well in case
# the rule ran before the attributes existed.
(cd "$DEV" && find rgb_zones fan battery -maxdepth 1 -type f -perm -u=w -exec chown "$USER_NAME" {} +)

echo
echo "Done. Driver $(modinfo -F version omen_rgb_keyboard 2>/dev/null) is loaded and $USER_NAME can change its settings."
echo "Open Prism now. (The top-bar tile appears after your next login: gnome-extensions enable prism-omen@local)"
