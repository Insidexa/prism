// Prism Quick Settings: keyboard lighting tile, Max fans and the speaker sound toggle.
// All changes go through the `prism` command, so validation lives in one place.
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import {QuickMenuToggle, QuickToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';
import {Slider} from 'resource:///org/gnome/shell/ui/slider.js';

const EFFECTS = [
    ['static', 'Static'], ['breathing', 'Breathing'], ['rainbow', 'Rainbow'],
    ['wave', 'Wave'], ['pulse', 'Pulse'], ['chase', 'Chase'], ['sparkle', 'Sparkle'],
    ['candle', 'Candle'], ['aurora', 'Aurora'], ['disco', 'Disco'],
    ['gradient', 'Gradient'], ['spectrum', 'Spectrum'],
];
const effectLabel = id => (EFFECTS.find(e => e[0] === id) ?? [id, id])[1];

function prismPath() {
    return GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', 'prism']);
}

function runPrism(args) {
    return new Promise((resolve, reject) => {
        let proc;
        try {
            proc = Gio.Subprocess.new([prismPath(), ...args],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (e) {
            reject(e);
            return;
        }
        proc.communicate_utf8_async(null, null, (p, res) => {
            try {
                const [, out, err] = p.communicate_utf8_finish(res);
                if (p.get_successful())
                    resolve(out);
                else
                    reject(new Error((err || '').replace(/^prism: /, '').trim() || 'prism failed'));
            } catch (e) {
                reject(e);
            }
        });
    });
}

const LightingToggle = GObject.registerClass(
class LightingToggle extends QuickMenuToggle {
    _init(ext) {
        const gicon = ext.icon('prism-keyboard-brightness-symbolic');
        super._init({title: 'Keyboard', subtitle: '', gicon, toggleMode: false});
        this._ext = ext;
        this._gicon = gicon;
        this._brightness = 0;
        this._lastOn = 100;
        this._sliderTimer = 0;

        this.menu.setHeader(this._gicon, 'Keyboard lighting', '');

        // Brightness slider
        const sliderItem = new PopupMenu.PopupBaseMenuItem({activate: false});
        sliderItem.add_child(new St.Icon({gicon: this._gicon, style_class: 'popup-menu-icon'}));
        this._slider = new Slider(0);
        this._slider.x_expand = true;
        this._sliderChangedId = this._slider.connect('notify::value', () => this._sliderMoved());
        sliderItem.add_child(this._slider);
        this.menu.addMenuItem(sliderItem);

        // Effects
        this._effectMenu = new PopupMenu.PopupSubMenuMenuItem('Effect', false);
        this._effectItems = new Map();
        for (const [id, label] of EFFECTS) {
            const item = this._effectMenu.menu.addAction(label, () => this._set(['effect', id]));
            this._effectItems.set(id, item);
        }
        this.menu.addMenuItem(this._effectMenu);

        // Music lighting: zones pulse with the sound
        this._music = new PopupMenu.PopupSwitchMenuItem('Music', false);
        this._music.connect('toggled', (_i, on) => this._set(['music', on ? 'on' : 'off']));
        this.menu.addMenuItem(this._music);

        // Presets, filled when the menu opens
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Presets'));
        this._presets = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._presets);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addAction('Prism Settings', () => {
            Main.panel.closeQuickSettings?.();
            try {
                Gio.Subprocess.new([prismPath()], Gio.SubprocessFlags.NONE);
            } catch (e) {
                Main.notifyError('Prism', e.message);
            }
        });

        this.connect('clicked', () => {
            const target = this._brightness > 0 ? 0 : this._lastOn;
            this._set(['brightness', String(target)]);
        });
        this.menu.connect('open-state-changed', (_m, open) => {
            if (open)
                this._loadPresets();
        });
    }

    sync(st) {
        this._brightness = st.brightness;
        if (st.brightness > 0)
            this._lastOn = st.brightness;
        this.checked = st.brightness > 0;
        const effect = effectLabel(st.animation);
        this.subtitle = st.brightness > 0 ? effect : 'Off';
        this.menu.setHeader(this._gicon, 'Keyboard lighting',
            st.brightness > 0 ? `${effect} · ${st.brightness}%` : 'Off');
        this._effectMenu.label.text = `Effect: ${effect}`;
        this._music.setToggleState(!!st.music);
        if (st.music) {
            this.subtitle = st.brightness > 0 ? 'Music' : 'Off';
            this._effectMenu.label.text = 'Effect: Music';
        }
        for (const [id, item] of this._effectItems)
            item.setOrnament(id === st.animation ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        this._slider.block_signal_handler(this._sliderChangedId);
        this._slider.value = st.brightness / 100;
        this._slider.unblock_signal_handler(this._sliderChangedId);
    }

    _sliderMoved() {
        if (this._sliderTimer)
            GLib.source_remove(this._sliderTimer);
        this._sliderTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 120, () => {
            this._sliderTimer = 0;
            this._set(['brightness', String(Math.round(this._slider.value * 100))]);
            return GLib.SOURCE_REMOVE;
        });
    }

    async _loadPresets() {
        let names = [];
        try {
            names = JSON.parse(await runPrism(['preset', 'list', '--json']));
        } catch (e) {
            return;
        }
        this._presets.removeAll();
        for (const name of names)
            this._presets.addAction(name, () => this._set(['preset', 'apply', name]));
    }

    _set(args) {
        this._ext.command(args);
    }

    destroy() {
        if (this._sliderTimer)
            GLib.source_remove(this._sliderTimer);
        this._sliderTimer = 0;
        super.destroy();
    }
});

const MaxFanToggle = GObject.registerClass(
class MaxFanToggle extends QuickToggle {
    _init(ext) {
        super._init({title: 'Max Fans', gicon: ext.icon('prism-fan-symbolic'), toggleMode: false});
        this.connect('clicked', () => ext.command(['maxfan', this.checked ? 'off' : 'on']));
    }

    sync(st) {
        this.checked = !!st.max_fan;
        this.subtitle = st.cpu_fan_rpm !== undefined
            ? `${Math.max(st.cpu_fan_rpm, st.gpu_fan_rpm).toLocaleString()} rpm` : '';
    }
});

// Speaker sound: on = the Prism chain, off = EasyEffects. Same loudness either
// way, so it doubles as an A/B switch for comparing the two.
const SoundToggle = GObject.registerClass(
class SoundToggle extends QuickToggle {
    _init(ext) {
        super._init({title: 'Prism Sound', gicon: ext.icon('prism-speaker-symbolic'), toggleMode: false});
        this._busy = false;
        this.connect('clicked', async () => {
            if (this._busy)
                return;
            this._busy = true;
            this.subtitle = 'Switching…';
            await ext.command(['sound', 'set', '--engine', this.checked ? 'easyeffects' : 'chain']);
            this._busy = false;
        });
    }

    sync(snd) {
        this.checked = snd.engine === 'chain';
        if (!this._busy)
            this.subtitle = this.checked ? (snd.chain_label ?? 'Prism chain') : 'EasyEffects';
    }
});

const PrismIndicator = GObject.registerClass(
class PrismIndicator extends SystemIndicator {
    _init(ext) {
        super._init();
        this._ext = ext;
        this._fanIcon = this._addIndicator();
        this._fanIcon.gicon = ext.icon('prism-fan-symbolic');
        this._fanIcon.visible = false;

        this._lighting = new LightingToggle(ext);
        this._maxFan = new MaxFanToggle(ext);
        this._sound = new SoundToggle(ext);
        this._sound.visible = false;
        this.quickSettingsItems.push(this._lighting, this._maxFan, this._sound);
    }

    sync(st) {
        const snd = st?.sound;
        this._sound.visible = !!snd?.can_switch;
        if (snd?.can_switch)
            this._sound.sync(snd);
        const ok = st && st.present;
        this._lighting.visible = !!ok;
        this._maxFan.visible = !!ok && st.max_fan !== undefined;
        if (!ok)
            return;
        this._lighting.sync(st);
        if (st.max_fan !== undefined)
            this._maxFan.sync(st);
        this._fanIcon.visible = !!st.max_fan;
    }

    destroy() {
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});

export default class PrismExtension extends Extension {
    enable() {
        this._indicator = new PrismIndicator(this);
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
        // Refresh whenever Quick Settings opens, so changes made in the app,
        // the terminal or GNOME's Power Mode show up without polling.
        this._menuId = Main.panel.statusArea.quickSettings.menu.connect('open-state-changed',
            (_m, open) => {
                if (open)
                    this.refresh();
            });
        this.refresh();
    }

    disable() {
        Main.panel.statusArea.quickSettings.menu.disconnect(this._menuId);
        this._menuId = 0;
        this._indicator?.destroy();
        this._indicator = null;
    }

    icon(name) {
        return Gio.FileIcon.new(this.dir.get_child('icons').get_child(`${name}.svg`));
    }

    async refresh() {
        try {
            const st = JSON.parse(await runPrism(['status', '--json']));
            this._indicator?.sync(st);
        } catch (e) {
            this._indicator?.sync(null);
            console.warn(`Prism: ${e.message}`);
        }
    }

    async command(args) {
        try {
            await runPrism(args);
        } catch (e) {
            Main.notifyError('Prism', e.message);
        }
        await this.refresh();
    }
}
