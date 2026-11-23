# Prism

**Keyboard lighting, fans and performance modes for HP Omen laptops on Linux.**
A native GNOME app, a Quick Settings tile and a command-line tool, built on the
community [omen-rgb-keyboard](https://github.com/OmenLinux/omen-rgb-keyboard) driver.

| Lighting | Performance | Automation |
|---|---|---|
| ![Lighting page](docs/lighting.png) | ![Performance page](docs/performance.png) | ![Automation page](docs/automation.png) |

## Features

- **Lighting**: live keyboard preview (click a key to pick its zone), per-zone colours
  for the 4 hardware zones (WASD, Left, Middle, Right), 12 effects with speed,
  brightness, and presets you can save.
- **Performance**: Quiet / Balanced / Performance modes that stay in sync with GNOME's
  Power Mode, live CPU/GPU temperature, CPU load and fan speed, a drag-and-drop fan
  curve editor with a warning for unsafe curves, and CPU power limits (PL1/PL2).
  The discrete GPU is never woken just to read its temperature.
- **Automation**: switch presets by time of day and power source, e.g. a dim
  “Battery Saver” when unplugged and a warm “Night” from 22:00 to 07:00. Rules are
  checked top to bottom, the first match wins, and colours you change by hand stay
  until a different rule starts to apply. Runs as a small user service, so it works
  with the app closed.
- **Quick Settings tile**: lighting on/off, brightness, effect and presets, plus a
  Max Fans toggle, right in GNOME's top-bar menu.
- **CLI** for scripts and shortcuts.
- **No sudo in daily use**: a one-time install hands the driver's controls to you.

## Tested hardware

| Laptop | Board | BIOS | Distro | Kernel | Status |
|---|---|---|---|---|---|
| HP Omen 16-c0xxx (Ryzen 7 5800H, RX 6600M) | 8902 | F.27 | Fedora 44, GNOME 50 | 7.3 | Working |

Other Omen models supported by the driver should work; the 4-zone layout in the
preview is the one of the 16-c0xxx. Reports from other models are welcome.

## Install

Requirements: Fedora (the system installer uses `dnf` and DKMS), GNOME 48–50,
Python 3 with PyGObject, GTK 4 and libadwaita (all preinstalled on Fedora Workstation).

```sh
git clone https://github.com/Insidexa/prism.git
cd prism
sudo system/install.sh   # once: driver via DKMS, retire hp_wmi, give you the controls
make install             # app, `prism` command and GNOME extension into ~/.local
```

Open **Prism** from the app grid. It works right away: no reboot, no re-login.

The Quick Settings tile is a GNOME Shell extension. GNOME on Wayland only discovers
new extensions at login, so after your next login run:

```sh
gnome-extensions enable prism-omen@local
```

### What `system/install.sh` does

1. Downloads the omen-rgb-keyboard driver at the version Prism was tested with
   (or uses a path you pass: `sudo system/install.sh /path/to/driver`).
2. Installs it with DKMS, so it is rebuilt automatically on kernel updates.
3. Blacklists `hp_wmi`, which would otherwise fight the driver for the same firmware
   interface (the driver provides power profiles itself).
4. Creates `/var/lib/omen-rgb-keyboard`, where the driver keeps colours and fan
   settings across reboots.
5. Installs a udev rule that makes you the owner of the driver's writable controls
   whenever it loads.

On first load the driver runs the fans at full speed for about 20 seconds to
measure their maximum RPM. That is expected and happens once.

> The upstream driver's own installer adds you to the `input` group instead.
> That group can read every keystroke from every keyboard, so Prism does not use it.

### Uninstall

```sh
make uninstall
sudo system/uninstall.sh   # removes the driver and rule, restores hp_wmi
```

## Command line

```sh
prism                          # open the app
prism status [--json]          # current lighting, mode and fans
prism color all 00c8ff         # whole keyboard
prism color 3 ffffff           # one zone: 3 WASD, 2 Left, 1 Middle, 0 Right
prism brightness 60
prism effect breathing --speed 4
prism preset list | apply Ice | save "My look" | delete "My look"
prism profile performance      # silent | normal | performance
prism maxfan on
prism auto status | on | off    # automatic presets (rules are edited in the app)
```

Effects: static, breathing, rainbow, wave, pulse, chase, sparkle, candle, aurora,
disco, gradient, spectrum.

Presets live in `~/.config/prism/presets.json`, automation rules in
`~/.config/prism/automation.json`. The rules are applied by the
`prism-automation` user service, which `make install` enables
(`systemctl --user status prism-automation`).

## Development

```sh
make test     # unit tests (no driver or GPU needed: they use a fake sysfs tree)
make run      # run from the source tree
PRISM_SYSFS=/path/to/fake/tree make run   # run against a fake driver
```

Layout:

```
prism/backend.py    all driver reads/writes and value validation
prism/sensors.py    temperatures and load (k10temp, amdgpu, /proc/stat)
prism/presets.py    built-in and saved presets
prism/automation.py time/power rules and the background service
prism/cli.py        the `prism` command
prism/app.py        GTK4/libadwaita app
prism/widgets.py    keyboard preview, fan curve editor, sparklines
extension/          GNOME Shell Quick Settings extension (calls the `prism` command)
system/             Fedora installer, udev rule, uninstaller
```

## Credits and license

Prism is licensed under the [GNU GPL v3](LICENSE).

- Driver: [omen-rgb-keyboard](https://github.com/OmenLinux/omen-rgb-keyboard)
  by alessandromrc and contributors (GPL).
- Icons in `data/icons/hicolor/scalable/status/` are from the
  [Adwaita icon theme](https://gitlab.gnome.org/GNOME/adwaita-icon-theme)
  (LGPL-3.0 / CC-BY-SA 3.0), renamed with a `prism-` prefix so they work with any
  icon theme.

Prism is a community project, not affiliated with or endorsed by HP.
"OMEN" is a trademark of HP Inc., used here only to name the supported hardware.
