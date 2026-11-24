#!/usr/bin/env bash
# Undo system/install.sh: remove the DKMS driver and restore hp_wmi.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
NAME=omen-rgb-keyboard

modprobe -r omen_rgb_keyboard 2>/dev/null || true
for v in $(dkms status "$NAME" 2>/dev/null | sed -n "s|^$NAME/\([^,:]*\).*|\1|p" | sort -u); do
    dkms remove "$NAME/$v" --all || true
    rm -rf "/usr/src/$NAME-$v"
done
rm -f /etc/modprobe.d/prism-hp-wmi-blacklist.conf \
      /etc/modules-load.d/omen_rgb_keyboard.conf \
      /etc/udev/rules.d/70-prism-omen.rules \
      /etc/udev/hwdb.d/71-prism-omen-key.hwdb
systemd-hwdb update || true
udevadm control --reload-rules
modprobe hp_wmi || true
echo "Removed. Saved settings are kept in /var/lib/omen-rgb-keyboard; the 'omen' group is left in place."
