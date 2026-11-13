import copy
import io
import json
import os
import unittest
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import tests.support  # noqa: F401,E402
from mxoptions import actions, api, config, device, environment  # noqa: E402
from mxoptions.ui import keyboard_page, sounds  # noqa: E402
from mxoptions.ui.store import Store  # noqa: E402

APP = QApplication.instance() or QApplication([])  # widgets too: the keyboard's page is tested here


class Patterns(unittest.TestCase):
    def test_names_and_numbers(self):
        self.assertEqual(api.find_pattern("knock"), 0x0C)
        self.assertEqual(api.find_pattern("Sharp Collision"), 0x02)
        self.assertEqual(api.find_pattern("whisper_collision"), 0x1B)
        self.assertEqual(api.find_pattern("0x1b"), 0x1B)
        self.assertEqual(api.find_pattern("27"), 0x1B)
        self.assertIsNone(api.find_pattern("bogus"))
        self.assertIsNone(api.find_pattern("99"))

    def test_every_pattern_has_a_distinct_bus_name(self):
        names = [api.pattern_name(waveform) for waveform in device.WAVEFORMS]
        self.assertEqual(len(set(names)), len(names))
        for name in names:
            self.assertEqual(name, name.lower())
            self.assertNotIn(" ", name)


class Actions(unittest.TestCase):
    def test_catalogue_id_or_json(self):
        self.assertEqual(api.parse_action("copy"), actions.PRESET_BY_ID["copy"].action)
        self.assertEqual(api.parse_action('{"type": "keys", "keys": ["KEY_F5"]}'), {"type": "keys", "keys": ["KEY_F5"]})
        self.assertEqual(api.parse_action(' {"type": "hook", "name": "x"} ')["name"], "x")

    def test_what_cannot_be_triggered(self):
        for text in ("gestures", "ring", "default", "dpi_shift", "no-such-action", "{broken", '["list"]', '{"type": "warp"}'):
            with self.subTest(text=text):
                self.assertIsNone(api.parse_action(text))

    def test_what_the_desktop_does_in_its_own_way_goes_by_name(self):
        self.assertEqual(api.parse_action("overview"), {"type": "desktop", "do": "overview"})
        self.assertEqual(api.parse_action('{"type": "desktop", "do": "win_close"}'), {"type": "desktop", "do": "win_close"})
        self.assertTrue(environment.available(api.parse_action("overview"), environment.KDE))
        self.assertFalse(environment.available(api.parse_action("overview"), environment.SWAY))
        self.assertFalse(environment.available(api.parse_action('{"type": "desktop", "do": "no_such_thing"}'), environment.KDE))

    def test_catalogue_ids_are_unique(self):
        ids = [preset.id for preset in actions.PRESETS]
        self.assertEqual(len(set(ids)), len(ids))


class Clicks(unittest.TestCase):
    def read(self, data):
        with wave.open(io.BytesIO(data)) as f:
            return f.getframerate(), f.getnchannels(), f.getsampwidth(), f.getnframes()

    def test_every_style_is_a_short_mono_wav(self):
        for style in sounds.STYLES:
            for kind in sounds.KINDS:
                with self.subTest(style=style, kind=kind):
                    rate, channels, width, frames = self.read(sounds.synthesize(style, kind))
                    self.assertEqual((rate, channels, width), (sounds.RATE, 1, 2))
                    self.assertLess(frames / rate, 0.3)

    def test_the_tick_is_the_shortest_and_length_stretches_it(self):
        def length(style, stretch=1.0):
            return self.read(sounds.synthesize(style, "freespin", 1.0, stretch))[3]

        self.assertLess(length("tick"), length("clack"))
        self.assertLess(length("clack"), length("relay"))
        self.assertLess(length("tick", 0.5), length("tick"))
        self.assertLess(length("tick"), length("tick", 2.5))

    def test_volume_slider_is_gentle_at_the_quiet_end(self):
        self.assertEqual(sounds.loudness(0.0), 0.0)
        self.assertEqual(sounds.loudness(1.0), 1.0)
        self.assertAlmostEqual(sounds.loudness(0.3), 0.09)
        self.assertEqual(sounds.loudness(5), 1.0)



class Settings(unittest.TestCase):
    """Set() and the settings it changes, as other programs see them."""

    COPY = {"type": "keys", "keys": ["KEY_LEFTCTRL", "KEY_C"]}

    def setUp(self):
        class Engine:
            def __getattr__(self, name):
                return lambda *args: None
        self.store = Store(copy.deepcopy(config.DEFAULTS), Engine())
        self.store.flush = lambda: None  # made-up settings: never written anywhere
        self.store._save_timer.timeout.disconnect()
        self.api = api.MouseApi(self.store, None, lambda: None)

    def keys(self):
        return self.store.cfg["keyboard"]["keys"]

    def test_keys_are_added_and_taken_out_as_a_whole(self):
        self.assertTrue(self.api.Set("keyboard.keys", json.dumps({"0x00c7": self.COPY, "0x00c8": {"type": "host", "host": 1}})))
        self.assertEqual(json.loads(self.api.Get("keyboard.keys.0x00c7")), self.COPY)
        self.assertTrue(self.api.Set("keyboard.keys.0x00c7", json.dumps({"type": "keys", "keys": ["KEY_F5"]})))  # one that is there
        self.assertEqual(self.keys()["0x00c7"], {"type": "keys", "keys": ["KEY_F5"]})
        self.assertFalse(self.api.Set("keyboard.keys.0x00c9", json.dumps(self.COPY)))  # one that is not: no such path
        self.assertFalse(self.api.Set("keyboard.keys.0x00c7", json.dumps({"type": "default"})))  # taken out as a whole only
        self.assertTrue(self.api.Set("keyboard.keys", json.dumps({"0x00c8": {"type": "host", "host": 1}})))
        self.assertEqual(list(self.keys()), ["0x00c8"])
        self.assertIn('"keyboard"', self.api.GetConfig())

    def test_what_a_key_cannot_have_is_refused_not_dropped(self):
        self.api.Set("keyboard.keys", json.dumps({"0x00c7": self.COPY}))
        before = copy.deepcopy(self.store.cfg)
        for value in ({"0x00c7": {"type": "gestures"}}, {"0x00c7": {"type": "flow"}}, {"0x00C7": self.COPY},
                      {"0x00c7": {"type": "keys"}}, {"0x00c7": self.COPY, "0x00c8": {"type": "default"}}):
            with self.subTest(value=value):
                self.assertFalse(self.api.Set("keyboard.keys", json.dumps(value)))
                self.assertEqual(self.store.cfg, before)
        self.assertFalse(self.api.Set("keyboard.keys.0x00c7", json.dumps({"type": "ring", "x": 1})))
        self.assertFalse(self.api.Set("keyboard", json.dumps({"keys": {"0x00c7": {"type": "dpi_shift"}}})))
        self.assertEqual(self.store.cfg, before)

    def test_a_key_may_open_the_actions_ring_and_trigger_still_may_not(self):
        listed = self.api.ListActions()
        self.api.Set("keyboard.keys", json.dumps({"0x00c7": self.COPY}))
        self.assertTrue(self.api.Set("keyboard.keys.0x00c7", json.dumps({"type": "ring"})))
        self.assertEqual(self.keys(), {"0x00c7": {"type": "ring"}})
        self.assertTrue(self.api.Set("keyboard.keys", json.dumps({"0x00c8": {"type": "ring"}})))
        self.assertFalse(self.api.Set("keyboard.keys", json.dumps({"0x00c8": {"type": "ring", "bubbles": []}})))
        self.assertEqual(self.keys(), {"0x00c8": {"type": "ring"}})
        self.assertFalse(self.api.Trigger(json.dumps({"type": "ring"})))
        self.assertFalse(self.api.Trigger("ring"))
        self.assertNotIn("ring", listed)
        self.assertEqual(self.api.ListActions(), listed)

    def test_a_key_with_an_action_that_cannot_run_is_refused_everywhere(self):
        self.api.Set("keyboard.keys", json.dumps({"0x00c7": self.COPY}))
        before = copy.deepcopy(self.store.cfg)
        for action in ({"type": "desktop", "do": []}, {"type": "desktop", "do": {}},
                       {"type": "keys", "keys": ["KEY_NOT_REAL"]}, {"type": "macro", "steps": [{"do": "delay", "ms": "invalid"}]},
                       {"type": "macro", "steps": [{"do": "keys"}]}, {"type": "macro", "steps": [{"do": "click", "button": 1}]}):
            with self.subTest(action=action):
                self.assertFalse(self.api.Set("keyboard.keys", json.dumps({"0x00c7": action})))
                self.assertFalse(self.api.Set("keyboard.keys.0x00c7", json.dumps(action)))
                self.assertFalse(self.store.set_key("0x00c7", action))  # what the pages set goes through the same check
                self.assertFalse(self.store.set_key("0x00c8", action))
                self.assertEqual(self.store.cfg, before)
        self.assertFalse(self.store.set_key("0x00C7", self.COPY))
        self.assertTrue(self.store.set_key("0x00c8", self.COPY))

    def test_the_mouse_settings_work_as_before(self):
        self.assertTrue(self.api.Set("pointer.dpi", "1600"))
        self.assertEqual(self.store.cfg["pointer"]["dpi"], 1600)
        self.assertTrue(self.api.Set("wheel.threshold", "900"))  # clamped, as ever
        self.assertEqual(self.store.cfg["wheel"]["threshold"], 50)
        self.assertFalse(self.api.Set("wheel.mode", "sideways"))  # text it does not know: refused
        self.assertFalse(self.api.Set("pointer.dpi", '"fast"'))
        self.assertFalse(self.api.Set("version", "7"))
        self.assertFalse(self.api.Set("no.such.thing", "1"))
        self.assertTrue(self.api.Set("buttons.back", json.dumps({"type": "gestures"})))  # a mouse button still may
        self.assertEqual(self.store.cfg["buttons"]["back"]["type"], "gestures")

    def test_store_keys_and_restore_defaults(self):
        self.store.set_key("0x00c7", self.COPY)
        self.store.set_key("0x00c8", {"type": "none"})
        self.store.set_key("0x00c8", {"type": "default"})
        self.assertEqual(self.keys(), {"0x00c7": self.COPY})
        self.store.restore_defaults()
        self.assertEqual(self.keys(), {})
        self.assertIsNone(self.store.state["keyboard"])


class KeyChooser(unittest.TestCase):
    """The keys on the Keys page, picked and given an action with the keyboard alone."""

    def test_a_second_key_is_picked_and_set_without_a_pointer(self):
        class Engine:
            def __getattr__(self, name):
                return lambda *args: None
        store = Store(copy.deepcopy(config.DEFAULTS), Engine())
        store.flush = lambda: None
        store._save_timer.timeout.disconnect()
        keys = [{"id": config.key_id(cid), "cid": cid, "tid": cid, "protected": cid == 0x00D1, "active": False,
                 "native_reason": "protected" if cid == 0x00D1 else None} for cid in (0x00D1, 0x0F01, 0x0F02)]
        page = keyboard_page.KeysPage(store)
        self.addCleanup(page.deleteLater)
        page.resize(1100, 600)
        page.show()
        page.activateWindow()
        self.assertTrue(QTest.qWaitForWindowActive(page))
        store.update_state(dict(store.state, keyboard={"connected": True, "known": True, "name": "Synthetic keyboard",
                                                       "battery": None, "current_host": 0, "hosts": [], "keys": keys,
                                                       "reason": None, "failed": None}))
        caps = page.strip.caps
        self.assertEqual([cap.accessibleName() for cap in caps.values()], ["Easy-Switch 1", "Key 0x0f01", "Key 0x0f02"])
        self.assertEqual(caps["0x00d1"].accessibleDescription(), "Kept by the keyboard")
        self.assertTrue(all(cap.focusPolicy() & Qt.FocusPolicy.TabFocus for cap in caps.values()))
        self.assertTrue(caps["0x0f01"].isChecked())  # the first key that can be changed, to begin with
        caps["0x0f01"].setFocus()
        QTest.keyClick(caps["0x0f01"], Qt.Key.Key_Right)
        self.assertTrue(caps["0x0f02"].hasFocus())
        self.assertTrue(caps["0x0f02"].isChecked())
        self.assertEqual(page._key["id"], "0x0f02")
        page.picker.changed.emit(actions.keys("LEFTCTRL", "V"))
        self.assertEqual(store.cfg["keyboard"]["keys"], {"0x0f02": actions.keys("LEFTCTRL", "V")})
        self.assertEqual(caps["0x0f02"].accessibleDescription(), actions.describe(actions.keys("LEFTCTRL", "V")))
        QTest.keyClick(caps["0x0f02"], Qt.Key.Key_Left)
        QTest.keyClick(caps["0x0f01"], Qt.Key.Key_Left)
        self.assertEqual(page._key["id"], "0x00d1")  # a key the keyboard keeps is reached too, and says so
        QTest.keyClick(caps["0x00d1"], Qt.Key.Key_Right)
        QTest.keyClick(caps["0x0f01"], Qt.Key.Key_Space)
        self.assertEqual(page._key["id"], "0x0f01")


if __name__ == "__main__":
    unittest.main()
