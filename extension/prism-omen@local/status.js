// Prism top-bar status: shows something only when it matters.
//   - power draw in watts while on battery,
//   - a GPU icon while the discrete GPU is awake (it should sleep when unused),
//   - a fan icon with the speed while the fans spin,
//   - a mouse icon with the percentage when a mouse or keyboard battery is low.
// Clicking it opens the details and the power plans.
//
// Everything is read without blocking GNOME Shell: sysfs files asynchronously,
// the battery from UPower over D-Bus (it signals changes, no polling), device
// batteries from the cache that `prism daemon` writes (Solaar is too slow here).
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import Clutter from 'gi://Clutter';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const FAN_DIR = '/sys/devices/platform/omen-rgb-keyboard/fan';
const TICK_S = 5;
const LOW_DEVICE = 20;          // % at which a mouse/keyboard shows up in the bar
const UPOWER_DISCHARGING = 2;
const PLANS = [['battery', 'Battery saver', 'power-saver'],
    ['balanced', 'Balanced', 'balanced'], ['performance', 'Performance', 'performance']];

const decoder = new TextDecoder();

function readFile(path) {
    return new Promise(resolve => {
        Gio.File.new_for_path(path).load_contents_async(null, (f, res) => {
            try {
                const [, bytes] = f.load_contents_finish(res);
                resolve(decoder.decode(bytes).trim());
            } catch (e) {
                resolve(null);
            }
        });
    });
}

function readSync(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok ? decoder.decode(bytes).trim() : null;
    } catch (e) {
        return null;
    }
}

function listDir(path) {
    try {
        const out = [];
        const en = Gio.File.new_for_path(path).enumerate_children('standard::name', 0, null);
        let info;
        while ((info = en.next_file(null)))
            out.push(info.get_name());
        return out.sort();
    } catch (e) {
        return [];
    }
}

// The discrete GPU: PCI class "display controller" (0x0380) or "3D" (0x0302),
// i.e. a GPU without the laptop screen (same rule as prism battery). Only
// attributes that don't wake the card are read.
function findDiscreteGpu() {
    for (const dev of listDir('/sys/bus/pci/devices')) {
        const cls = readSync(`/sys/bus/pci/devices/${dev}/class`) ?? '';
        if ((cls.startsWith('0x0380') || cls.startsWith('0x0302')) &&
            GLib.file_test(`/sys/bus/pci/devices/${dev}/power`, GLib.FileTest.IS_DIR))
            return dev;
    }
    return null;
}

function findHwmon(name) {
    for (const h of listDir('/sys/class/hwmon')) {
        if (readSync(`/sys/class/hwmon/${h}/name`) === name)
            return `/sys/class/hwmon/${h}`;
    }
    return null;
}

export const PrismStatus = GObject.registerClass(
class PrismStatus extends PanelMenu.Button {
    _init(ext, runPrism) {
        super._init(0.5, 'Prism status');
        this._ext = ext;
        this._runPrism = runPrism;
        this._gpu = findDiscreteGpu();
        this._cpuHwmon = findHwmon('k10temp') ?? findHwmon('coretemp');
        this._hasFans = GLib.file_test(`${FAN_DIR}/cpu_fan_rpm`, GLib.FileTest.EXISTS);
        this._state = {watts: null, onBattery: false, gpuAwake: false, fans: null, devices: []};
        this._ticks = 0;

        const box = new St.BoxLayout({style_class: 'panel-status-indicators-box'});
        const part = (icon) => {
            const b = new St.BoxLayout({visible: false});
            if (icon)
                b.add_child(new St.Icon({gicon: ext.icon(icon), style_class: 'system-status-icon'}));
            const label = new St.Label({y_align: Clutter.ActorAlign.CENTER});
            b.add_child(label);
            box.add_child(b);
            return {box: b, label};
        };
        this._gpuPart = part('prism-gpu-symbolic');
        this._fanPart = part('prism-fan-symbolic');
        this._devPart = part('prism-mouse-symbolic');
        this._wattPart = part(null);
        this.add_child(box);

        this._buildMenu();
        this._watchUPower();
        this._watchPowerProfiles();
        this._tick();
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, TICK_S, () => {
            this._tick();
            return GLib.SOURCE_CONTINUE;
        });
        this.menu.connect('open-state-changed', (_m, open) => {
            if (open)
                this._fillMenu();
        });
    }

    // ------------------------------------------------------------------ data
    _watchUPower() {
        Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, null,
            'org.freedesktop.UPower', '/org/freedesktop/UPower/devices/DisplayDevice',
            'org.freedesktop.UPower.Device', null, (_o, res) => {
                try {
                    this._upower = Gio.DBusProxy.new_for_bus_finish(res);
                } catch (e) {
                    return;
                }
                this._upowerId = this._upower.connect('g-properties-changed', () => this._readUPower());
                this._readUPower();
            });
    }

    _readUPower() {
        const rate = this._upower?.get_cached_property('EnergyRate')?.unpack();
        const state = this._upower?.get_cached_property('State')?.unpack();
        this._state.onBattery = state === UPOWER_DISCHARGING;
        this._state.watts = rate > 0 ? rate : null;
        this._show();
    }

    _watchPowerProfiles() {
        Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, null,
            'org.freedesktop.UPower.PowerProfiles', '/org/freedesktop/UPower/PowerProfiles',
            'org.freedesktop.UPower.PowerProfiles', null, (_o, res) => {
                try {
                    this._ppd = Gio.DBusProxy.new_for_bus_finish(res);
                } catch (e) {
                    return;
                }
                this._ppdId = this._ppd.connect('g-properties-changed', () => this._showPlan());
                this._showPlan();
            });
    }

    async _tick() {
        if (this._destroyed)
            return;
        this._ticks++;
        if (this._gpu) {
            const st = await readFile(`/sys/bus/pci/devices/${this._gpu}/power/runtime_status`);
            this._state.gpuAwake = st === 'active';
        }
        // Fan speeds come from the laptop's firmware and cost a few hundred ms
        // of kernel time: every 10 s plugged in, every 20 s on battery.
        const every = this._state.onBattery ? 4 : 2;
        if (this._hasFans && (this._ticks % every === 1 || this.menu.isOpen)) {
            const [cpu, gpu] = await Promise.all([readFile(`${FAN_DIR}/cpu_fan_rpm`),
                readFile(`${FAN_DIR}/gpu_fan_rpm`)]);
            this._state.fans = cpu === null ? null : [parseInt(cpu), parseInt(gpu ?? '0')];
        }
        if (this._ticks % 12 === 1) {  // the daemon refreshes it every 10 minutes
            try {
                const d = JSON.parse(await readFile(`${GLib.get_user_cache_dir()}/prism/devices.json`) ?? '{}');
                const fresh = Date.now() / 1000 - (d.time ?? 0) < 3600;
                this._state.devices = fresh ? d.devices ?? [] : [];
            } catch (e) {
                this._state.devices = [];
            }
        }
        if (this._destroyed)
            return;
        this._show();
        if (this.menu.isOpen)
            this._fillMenu();
    }

    // ------------------------------------------------------------------ bar
    _show() {
        const s = this._state;
        this._gpuPart.box.visible = s.gpuAwake;
        const fan = s.fans ? Math.max(...s.fans) : 0;
        this._fanPart.box.visible = fan > 0;
        this._fanPart.label.text = fan > 0 ? ` ${(fan / 1000).toFixed(1)}k` : '';
        const low = s.devices.filter(d => d.percent !== null && d.percent <= LOW_DEVICE &&
            d.state !== 'charging');
        this._devPart.box.visible = low.length > 0;
        this._devPart.label.text = low.length ? ` ${Math.round(Math.min(...low.map(d => d.percent)))}%` : '';
        this._wattPart.box.visible = s.onBattery && s.watts !== null;
        this._wattPart.label.text = s.watts !== null ? `${s.watts.toFixed(1)} W` : '';
        this.visible = this._gpuPart.box.visible || this._fanPart.box.visible ||
            this._devPart.box.visible || this._wattPart.box.visible;
    }

    // ------------------------------------------------------------------ menu
    _buildMenu() {
        const info = () => {
            const item = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
            this.menu.addMenuItem(item);
            return item;
        };
        this._powerItem = info();
        this._cpuItem = info();
        this._gpuItem = info();
        this._fanItem = info();
        this._devSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._devSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Power plan'));
        this._planItems = new Map();
        for (const [key, label, profile] of PLANS) {
            const item = this.menu.addAction(label, () => this._setPlan(key));
            this._planItems.set(profile, item);
        }
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Battery details…', () => {
            try {
                Gio.Subprocess.new([GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'prism'])],
                    Gio.SubprocessFlags.NONE);
            } catch (e) {
                Main.notifyError('Prism', e.message);
            }
        });
    }

    async _setPlan(key) {
        try {
            await this._runPrism(['plan', key]);
        } catch (e) {
            Main.notifyError('Prism', e.message);
        }
    }

    _showPlan() {
        const active = this._ppd?.get_cached_property('ActiveProfile')?.unpack();
        for (const [profile, item] of this._planItems)
            item.setOrnament(profile === active ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
    }

    async _fillMenu() {
        const s = this._state;
        this._powerItem.label.text = s.onBattery
            ? `On battery · ${s.watts !== null ? `${s.watts.toFixed(1)} W` : 'measuring…'}`
            : 'Plugged in';
        const cpu = this._cpuHwmon ? await readFile(`${this._cpuHwmon}/temp1_input`) : null;
        this._cpuItem.label.text = cpu ? `CPU ${Math.round(parseInt(cpu) / 1000)} °C` : 'CPU temperature unavailable';
        this._cpuItem.visible = !!cpu;
        if (!this._gpu) {
            this._gpuItem.visible = false;
        } else if (!s.gpuAwake) {
            this._gpuItem.label.text = 'Discrete GPU asleep';
        } else {
            // Awake already, so reading its sensor wakes nothing.
            const hw = listDir(`/sys/bus/pci/devices/${this._gpu}/hwmon`)[0];
            const t = hw ? await readFile(`/sys/bus/pci/devices/${this._gpu}/hwmon/${hw}/temp1_input`) : null;
            this._gpuItem.label.text = `Discrete GPU awake${t ? ` · ${Math.round(parseInt(t) / 1000)} °C` : ''}`;
        }
        this._fanItem.visible = this._hasFans;
        if (s.fans)
            this._fanItem.label.text = Math.max(...s.fans) > 0
                ? `Fans ${s.fans[0].toLocaleString()} / ${s.fans[1].toLocaleString()} rpm` : 'Fans off';
        this._devSection.removeAll();
        for (const d of s.devices) {
            const item = new PopupMenu.PopupMenuItem(
                `${d.name} ${d.percent !== null ? `${Math.round(d.percent)}%` : '–'}${d.state === 'charging' ? ' (charging)' : ''}`,
                {reactive: false, can_focus: false});
            this._devSection.addMenuItem(item);
        }
        this._showPlan();
    }

    destroy() {
        this._destroyed = true;
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        if (this._upowerId)
            this._upower.disconnect(this._upowerId);
        if (this._ppdId)
            this._ppd.disconnect(this._ppdId);
        this._upower = this._ppd = null;
        super.destroy();
    }
});
