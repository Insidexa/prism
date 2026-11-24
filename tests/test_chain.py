import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from prism import chain, sound
from prism.chain import (BASS_MIN, CHAIN, NODE, ChainEngine, ChainProfile, ChainWatcher, controls,
                         graph, load_chains, render, wav_channels)
from prism.sound import SoundEngine, SoundSettings, SoundStore

SPEAKER = ("alsa_output.pci-0000_08_00.6.HiFi__Speaker__sink", "Speaker", "[Out] Speaker")
BANDS = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


def wav(path: Path, channels: int) -> Path:
    fmt = struct.pack("<HHIIHH", 3, channels, 48000, 48000 * 4 * channels, 4 * channels, 32)
    data = b"\0" * 16
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
                     + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data)
    return path


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_loads_profiles_resolves_relative_impulse_and_skips_bad_files(self):
        wav(self.dir / "t.irs", 2)
        (self.dir / "good.json").write_text(json.dumps({
            "label": "Good", "impulse": "t.irs", "impulse_gain_db": 6, "crossfeed": 0.9,
            "bass": {"h2_db": -20, "h3_db": -30, "h4_db": -28}}))
        (self.dir / "plain.json").write_text(json.dumps({}))
        (self.dir / "broken.json").write_text("{nope")
        (self.dir / "badbass.json").write_text(json.dumps({"bass": {"h2_db": -20}}))
        got = load_chains(self.dir)
        self.assertEqual(sorted(got), ["good", "plain"])
        g = got["good"]
        self.assertEqual((g.label, g.impulse, g.crossfeed), ("Good", self.dir / "t.irs", 0.5))  # clamped
        self.assertEqual(g.bass["top_hz"], 200.0)
        self.assertIsNone(got["plain"].impulse)
        self.assertEqual(got["plain"].label, "plain")

    def test_wav_channels(self):
        self.assertEqual(wav_channels(wav(self.dir / "m.irs", 1)), 1)
        self.assertEqual(wav_channels(wav(self.dir / "s.irs", 2)), 2)
        self.assertEqual(wav_channels(self.dir / "missing.irs"), 1)


class GraphTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ir = wav(Path(self._tmp.name) / "t.irs", 2)
        self.p = ChainProfile("t", "T", impulse=self.ir, impulse_gain_db=11.3, crossfeed=0.19,
                              bass={"h2_db": -21.6, "h3_db": -31.8, "h4_db": -27.9,
                                    "top_hz": 280, "bottom_hz": 12}, ceiling_db=-1.2)

    def tearDown(self):
        self._tmp.cleanup()

    def links(self, g):
        return {(l["output"], l["input"]) for l in g["links"]}

    def test_full_chain_order_and_crossfeed(self):
        g = graph(self.p, None, BANDS, 0.0, Path("/x/prism_bass.so"))
        self.assertEqual(g["inputs"], ["bass:In L", "bass:In R"])
        self.assertEqual(g["outputs"], ["lim:Out L", "lim:Out R"])
        lk = self.links(g)
        self.assertIn(("bass:Out L", "eqL0:In"), lk)
        self.assertIn(("eqL9:Out", "mixL:In 1"), lk)
        self.assertIn(("eqR9:Out", "mixL:In 2"), lk)   # crossfeed from the other channel
        self.assertIn(("mixR:Out", "convR:In"), lk)
        self.assertIn(("convL:Out", "lim:In L"), lk)
        conv = {n["name"]: n for n in g["nodes"]}
        self.assertEqual(conv["convR"]["config"]["channel"], 1)
        self.assertAlmostEqual(conv["convL"]["config"]["gain"], 3.6728, places=3)
        # Every input port is fed exactly once.
        ins = [l["input"] for l in g["links"]]
        self.assertEqual(len(ins), len(set(ins)))

    def test_without_bass_or_impulse(self):
        p = ChainProfile("p", "P")
        g = graph(p, None, BANDS, 0.0, Path("/x.so"))
        self.assertEqual(g["inputs"], ["eqL0:In", "eqR0:In"])
        self.assertIn(("mixL:Out", "lim:In L"), self.links(g))
        self.assertNotIn("bass", {n["name"] for n in g["nodes"]})

    def test_controls_eq_headroom_and_bass_off(self):
        gains = [6, 4, 0, 0, 0, 0, 0, 0, 0, -3]
        c = controls(self.p, gains, BANDS, BASS_MIN)
        self.assertEqual(c["bass:Amount (dB)"], -120.0)
        self.assertEqual(c["eqR9:Gain"], -3.0)
        h = 10 ** (-3 / 20)  # half the largest boost
        self.assertAlmostEqual(c["mixL:Gain 1"], 0.81 * h, places=5)
        self.assertAlmostEqual(c["mixL:Gain 2"], 0.19 * h, places=5)
        flat = controls(self.p, None, BANDS, 2.0)
        self.assertEqual((flat["bass:Amount (dB)"], flat["eqL0:Gain"], flat["mixR:Gain 1"]), (2.0, 0.0, 0.81))

    def test_render_is_valid_json_after_the_header_and_pins_the_speakers(self):
        text = render(self.p, SPEAKER[0], None, BANDS, 0.0, Path("/x.so"))
        args = json.loads(text.split("\n", 1)[1])["context.modules"][0]["args"]
        self.assertEqual(args["capture.props"]["node.name"], NODE)
        pb = args["playback.props"]
        self.assertEqual(pb["target.object"], SPEAKER[0])
        self.assertTrue(pb["node.dont-move"] and pb["node.dont-fallback"])
        self.assertEqual(args["node.description"], "Speakers (Prism T)")


class EngineTest(unittest.TestCase):
    """Switching and live updates, with PipeWire mocked out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        data, cfg = base / "data", base / "config"
        (data / "prism-sound" / "chains").mkdir(parents=True)
        (data / "prism-sound" / "chains" / "bo.json").write_text(json.dumps({"label": "B&O"}))
        (base / "lib").mkdir()
        (base / "lib" / "prism_bass.so").write_bytes(b"")
        (cfg / "autostart").mkdir(parents=True)
        self.autostart = cfg / "autostart" / "easyeffects-service.desktop"
        self.autostart.write_text("[Desktop Entry]\nName=Easy Effects\n")
        self.env = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data), "XDG_CONFIG_HOME": str(cfg),
                                                "PRISM_LADSPA_DIR": str(base / "lib")})
        self.env.start()
        self.calls = []
        self.volumes = {1: 0.8, 2: 0.3}  # 1: speakers, 2: chain
        self.nodes = {SPEAKER[0]: 1, NODE: 2}
        self.service_enabled = "disabled"

        def run(cmd, timeout=10):
            self.calls.append(cmd)
            if cmd[:2] == ["wpctl", "get-volume"]:
                return f"Volume: {self.volumes[int(cmd[2])]:.2f}\n"
            if cmd[:2] == ["wpctl", "set-volume"]:
                self.volumes[int(cmd[2])] = float(cmd[3])
            if cmd[:3] == ["systemctl", "--user", "is-enabled"]:
                return self.service_enabled + "\n"
            return ""
        self.patches = [
            mock.patch.object(chain, "_run", run),
            mock.patch.object(sound, "_run", run),
            mock.patch.object(chain, "speaker_sink", lambda: SPEAKER),
            mock.patch.object(sound, "speaker_sink", lambda: SPEAKER),
            mock.patch.object(chain, "node_ids", lambda: dict(self.nodes)),
            mock.patch.object(chain, "easyeffects_running", lambda: False),
            mock.patch.object(chain, "load_eq_presets", lambda: {"Club": [3.0] * 10}),
        ]
        for p in self.patches:
            p.start()
        self.engine = ChainEngine(cfg / "prism" / "chain-state.json")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self._tmp.cleanup()

    def cmds(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]

    def test_switching_hands_volume_over_safely_and_disables_easyeffects_autostart(self):
        self.engine.apply(SoundSettings(engine=CHAIN), switching=True)
        self.assertEqual(self.volumes, {1: 1.0, 2: 0.5})  # was 0.8 on the speakers: capped at 0.5
        self.assertIn(["systemctl", "--user", "enable", "filter-chain"], self.calls)
        self.assertTrue(self.engine.load_state().enabled_service)
        self.assertIn("Hidden=true", self.autostart.read_text())
        self.assertTrue(chain.conf_path().exists())
        self.assertIn(["wpctl", "set-default", "2"], self.calls)

    def test_easyeffects_stops_only_after_the_chain_is_the_output(self):
        seen = []

        def stop():
            seen.append((["wpctl", "set-default", "2"] in self.calls, self.volumes[1]))
        with mock.patch.object(chain, "stop_easyeffects", stop):
            self.engine.apply(SoundSettings(engine=CHAIN), switching=True)
        # Chain already the default (players never lose their output), speakers not yet raised.
        self.assertEqual(seen, [(True, 0.5)])  # at the listening volume, not 100 %
        self.assertEqual(self.volumes[1], 1.0)

    def test_release_blocklists_the_apps_playing_through_easyeffects(self):
        sinks = json.dumps([{"index": 7, "name": "easyeffects_sink"}, {"index": 1, "name": SPEAKER[0]}])
        playing = [{"sink": 7, "properties": {"application.name": "Firefox",
                                              "application.process.binary": "firefox"}},
                   {"sink": 1, "properties": {"application.name": "Other"}}]
        ee = Path(self._tmp.name) / "ee"
        state = {"released": False}

        def run(cmd, timeout=10):
            self.calls.append(cmd)
            if cmd[-2:] == ["list", "sinks"]:
                return sinks
            if cmd[-2:] == ["list", "sink-inputs"]:
                return "[]" if state["released"] else json.dumps(playing)
            if cmd[:3] == ["easyeffects", "-w", "-l"]:
                state["released"] = True
            return ""
        with mock.patch.object(chain, "_run", run), mock.patch.object(chain, "easyeffects_running", lambda: True), \
                mock.patch.object(chain, "ee_dir", lambda: ee):
            chain.release_easyeffects_streams()
        preset = json.loads((ee / "output" / f"{sound.HANDOVER_PRESET}.json").read_text())
        self.assertEqual(preset["output"]["blocklist"], ["Firefox", "firefox"])
        self.assertIn(["easyeffects", "-w", "-l", sound.HANDOVER_PRESET], self.calls)

    def test_clean_easyeffects_config_undoes_only_the_handover(self):
        f = chain.ee_config()
        f.parent.mkdir(parents=True)
        f.write_text("[Presets]\nlastLoadedOutputPreset=Prism Handover\n[StreamOutputs]\n"
                     "blocklist=Mine,Firefox,firefox\nusedPresets=Prism Speakers:3,Prism Handover:1\n")
        chain.clean_easyeffects_config(["Firefox", "firefox"])
        self.assertEqual(f.read_text(), "[Presets]\nlastLoadedOutputPreset=Prism Speakers\n"
                         "[StreamOutputs]\nblocklist=Mine\nusedPresets=Prism Speakers:3\n")

    def test_bass_and_eq_change_live_without_restart(self):
        self.engine.apply(SoundSettings(engine=CHAIN), switching=True)
        self.calls.clear()
        self.engine.apply(SoundSettings(engine=CHAIN, eq="Club", chain_bass=-3), switching=False)
        self.assertEqual(self.cmds("systemctl", "--user", "restart"), [])
        (cmd,) = self.cmds("pw-cli", "set-param")
        self.assertIn('"eqL0:Gain" 3', cmd[4])
        self.assertIn('"Gain": 3.0', chain.conf_path().read_text())  # kept for the next start

    def test_new_profile_restarts_with_speakers_at_listening_volume_first(self):
        self.engine.apply(SoundSettings(engine=CHAIN), switching=True)
        (chain.chains_dir() / "other.json").write_text(json.dumps({"label": "Other", "crossfeed": 0.1}))
        self.calls.clear()
        self.engine.apply(SoundSettings(engine=CHAIN, chain="other"), switching=False)
        restart = self.calls.index(["systemctl", "--user", "restart", "filter-chain"])
        guard = self.calls.index(["wpctl", "set-volume", "1", "0.500"])
        self.assertLess(guard, restart)  # no loud burst while the chain is away
        self.assertEqual(self.volumes, {1: 1.0, 2: 0.5})

    def test_leave_restores_speakers_service_and_autostart(self):
        self.engine.apply(SoundSettings(engine=CHAIN), switching=True)
        self.volumes[2] = 0.35
        self.assertEqual(self.engine.leave(), 0.35)
        self.assertEqual(self.volumes[1], 0.35)
        self.assertIn(["systemctl", "--user", "disable", "--now", "filter-chain"], self.calls)
        self.assertNotIn("Hidden=true", self.autostart.read_text())
        self.assertFalse(chain.conf_path().exists())

    def test_sound_engine_dispatches_and_switches_back(self):
        store = SoundStore(Path(self._tmp.name) / "sound.json")
        eng = SoundEngine(Path(self._tmp.name) / "ee", store)
        with mock.patch.object(chain, "ChainEngine", lambda: self.engine):
            self.assertEqual(eng.apply(SoundSettings(engine=CHAIN)), "B&O on the speakers")
            self.assertEqual(store.load().engine, CHAIN)
            with mock.patch.object(sound, "easyeffects_available", lambda: True), \
                    mock.patch.object(sound, "easyeffects_running", lambda: True), \
                    mock.patch.object(sound, "default_sink", lambda: SPEAKER[0]):
                eng.apply(SoundSettings(engine="easyeffects", base=sound.OFF))
        self.assertFalse(chain.conf_path().exists())
        self.assertEqual(store.load().engine, "easyeffects")

    def test_watcher_brings_speakers_down_then_restores_chain(self):
        self.engine.apply(SoundSettings(engine=CHAIN), switching=True)
        os.utime(self.engine.state_path, (0, 0))  # not busy
        w = ChainWatcher(self.engine, log=lambda m: None, settings=lambda: SoundSettings(engine=CHAIN))
        del self.nodes[NODE]
        self.volumes[1] = 1.0
        self.calls.clear()
        restarted = []

        def restart(v):
            restarted.append(v)
        with mock.patch.object(self.engine, "restart", restart):
            self.assertEqual(w.step(now=100.0), "restored")
            self.assertEqual(self.volumes[1], 0.5)  # speakers at the listening volume at once
            self.assertEqual(w.step(now=105.0), "waiting")  # not again within 10 s
        self.assertEqual(restarted, [0.5])

    def test_watcher_idle_for_easyeffects_or_headphones(self):
        w = ChainWatcher(self.engine, log=lambda m: None, settings=lambda: SoundSettings())
        self.assertIsNone(w.step())
        self.engine.apply(SoundSettings(engine=CHAIN), switching=True)
        os.utime(self.engine.state_path, (0, 0))
        w = ChainWatcher(self.engine, log=lambda m: None, settings=lambda: SoundSettings(engine=CHAIN))
        del self.nodes[NODE]
        with mock.patch.object(chain, "speaker_sink", lambda: None):
            self.assertIsNone(w.step())


if __name__ == "__main__":
    unittest.main()
