"""App profiles of the mouse: the settings, the engine following the app in front (buttons, their macros, the Actions
Ring and the picks made in it), the overlay, the Store, other programs and the pages. The mouse is synthetic
(tests/support.py); app names are made up."""

import ast
import copy
import gc
import json
import logging
import os
import queue
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import tests.support as support  # noqa: E402
from mxoptions import APP_ID, actions, api, config, engine, hidpp  # noqa: E402
from mxoptions.ui import pages, ring, ring_page  # noqa: E402
from mxoptions.ui.store import Store  # noqa: E402

APP = QApplication.instance() or QApplication([])
NATIVE = config.NATIVE
COPY = actions.keys("LEFTCTRL", "C")
PASTE = actions.keys("LEFTCTRL", "V")
BACK, FORWARD, MIDDLE = config.BUTTONS["back"], config.BUTTONS["forward"], config.BUTTONS["middle"]
GESTURE, PANEL = config.BUTTONS["gesture"], config.BUTTONS["panel"]
KATE, KONSOLE, KWRITE = "org.kde.kate", "org.kde.konsole", "org.kde.kwrite"


def bubble(name):
    return {"label": name, "icon": "", "color": "", "action": {"type": "hook", "name": name}}


RING_A = [bubble("a1"), bubble("a2")]
RING_B = [bubble("b1")]
PICK_A, PICK_GLOBAL = RING_A[0]["action"], {"type": "hook", "name": "global"}


def profile(app, ring=None, **buttons):
    return {"app": app, "name": app.rsplit(".", 1)[-1], "buttons": buttons, "ring": copy.deepcopy(ring)}


def macro(*steps, repeat=False, name=""):
    """Keys to tap, with ("wait", ms) for a delay between them."""
    made = [{"do": "delay", "ms": step[1]} if isinstance(step, tuple) else {"do": "keys", "keys": [step]} for step in steps]
    return {"type": "macro", "name": name, "repeat": repeat, "steps": made}


class Listener(engine.Listener):
    def __init__(self):
        self.rings, self.turns, self.gestures, self.states = [], [], [], []

    def on_ring(self, event, session=None, bubbles=None):
        self.rings.append((event, session, bubbles))

    def on_ring_turn(self, clicks):
        self.turns.append(clicks)

    def on_gesture(self, event):
        self.gestures.append(event)

    def on_state(self, state):
        self.states.append(state)


class Mouse(support.Mouse):
    """Writes down every reporting and pointer speed it is given."""

    name, dpi_default, force = "MX Master 4", 1000, None

    def __init__(self):
        super().__init__()
        self.log, self.dpis, self.fail = [], [], False

    def set_cid_reporting(self, cid, divert, raw_xy=False):
        if self.fail:
            self.fail = False
            raise hidpp.HidppError("asleep")
        super().set_cid_reporting(cid, divert, raw_xy)
        self.log.append((cid, divert, raw_xy))

    def set_dpi(self, dpi):
        self.dpis.append(dpi)


class Ran(support.Actions):
    """Stands in for the executor: what ran, was let go of and was stopped."""

    def __init__(self):
        super().__init__()
        self.released, self.cancelled = [], []

    def release(self, action):
        self.released.append(action)

    def cancel(self, action):
        self.cancelled.append(action)


def make(test, profiles=(), real=True, cfg=None, **sections):
    """An engine on the test's thread with a synthetic mouse; ``real``: a real Executor on a FakeInput."""
    if cfg is None:
        cfg = copy.deepcopy(config.DEFAULTS)
        for section, values in sections.items():
            cfg[section].update(copy.deepcopy(values))
        cfg["mouse"]["profiles"] = copy.deepcopy(list(profiles))
    made = engine.Engine(cfg, Listener())
    made.vinput = support.FakeInput()
    made.mouse = Mouse()
    made.online = True
    made._publish = lambda: None
    made._next_battery = float("inf")
    made._monitor = True       # the gestures page is open: gestures are shown
    made._sync_watcher = lambda: None  # no ordinary reports to follow on a synthetic mouse
    made._play = lambda *_args, **_kwargs: None
    if real:
        made.executor = actions.Executor(made.vinput, made._device_action)
        test.addCleanup(stop, test, made.executor)
    else:
        made.executor = Ran()
    return made


def stop(test, executor):
    """End the executor's macro thread and make sure it is gone."""
    executor.close()
    thread = executor.macros._thread
    if thread is not None:
        thread.join(3.0)
        test.assertFalse(thread.is_alive(), "a macro thread was left running")


def drain(made):
    while True:
        try:
            item = made._queue.get_nowait()
        except queue.Empty:
            return
        made._handle(item)


def set_config(made, cfg):
    """As the Store does it: a whole copy of the settings through the engine's queue."""
    made.set_config(cfg)
    drain(made)


def front(made, app):
    """``app`` in front, and long enough for it to count."""
    made.set_front(app)
    drain(made)
    made._take_front(float("inf"))


def held(made):
    return made.vinput._holds.held()


def tapped(made):
    return [name for name, down in made.vinput.sent if down]


def wait_for(test, condition, seconds=4.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    test.assertTrue(condition())


def no_configure_writes(made):
    """Of the whole configuration pushed on (re)connecting, only the buttons reach the synthetic mouse."""
    for name in ("_apply_pointer", "_apply_wheel", "_apply_thumbwheel", "_apply_haptics"):
        setattr(made, name, lambda *_args: None)
    made._read_status = lambda **_kwargs: None


class Settings(unittest.TestCase):
    """mouse.profiles in the settings: kept exactly as they may be, refused whole or in part, resolved per app."""

    def clean(self, profiles):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["mouse"]["profiles"] = profiles
        return config.clean(cfg)["mouse"]["profiles"]

    def test_a_valid_profile_is_kept_as_it_is(self):
        kate = profile(KATE, RING_A, back=COPY, forward=NATIVE, panel={"type": "dpi_shift"}, middle={"type": "flow"},
                       gesture={"type": "ring"}, smartshift={"type": "none"})
        self.assertEqual(self.clean([kate]), [kate])
        self.assertEqual(self.clean([profile(KONSOLE)]), [profile(KONSOLE)])
        self.assertEqual(config.clean(copy.deepcopy(config.DEFAULTS))["mouse"], {"profiles": [], "thumbwheel": [], "thumbwheel_keys": [], "gestures": []})

    def test_a_profile_that_is_not_one_is_dropped_whole(self):
        good = profile(KATE, back=COPY)
        bad = [dict(good, haptics={"battery_saving": False}), {k: v for k, v in good.items() if k != "ring"},
               dict(good, app="Org.Kde.Kate"), dict(good, app=""), dict(good, app="a\tb"), dict(good, name="x" * 65),
               dict(good, name=3), dict(good, buttons=[]), dict(good, ring="a"), dict(good, ring={}), "kate", None, 3]
        for value in bad:
            with self.subTest(value=str(value)[:60]):
                self.assertEqual(self.clean([value, profile(KONSOLE)]), [profile(KONSOLE)])
        for value in ("x", {"app": KATE}, None, 7):
            self.assertEqual(self.clean(value), [])

    def test_what_a_button_cannot_do_in_an_app_is_dropped_and_the_rest_kept(self):
        broken = {"type": "macro", "steps": [{"do": "keys", "keys": ["NOT_A_KEY"]}]}
        kate = profile(KATE, back=COPY, gesture={"type": "gestures"}, forward={"type": "ring", "x": 1},
                       middle={"type": "dpi_cycle", "x": 1}, smartshift=broken, panel={"type": "default", "x": 1})
        kate["buttons"]["left"] = COPY
        self.assertEqual(self.clean([kate]), [profile(KATE, back=COPY)])
        for action in (config.default_gestures(), {"type": "gestures"}, {"type": "wheel_mode", "mode": "auto"}, None, []):
            self.assertFalse(config.profile_button_action(action))
        for action in (NATIVE, COPY, {"type": "ring"}, *config.MOUSE_ONLY, {"type": "host", "host": 1}):
            self.assertTrue(config.profile_button_action(action))

    def test_an_own_ring_is_checked_as_the_ring_for_all_apps(self):
        nine = [bubble(f"b{n}") for n in range(9)]
        deep = [{"label": "1", "icon": "", "color": "", "children": [
            {"label": "2", "icon": "", "color": "", "children": [
                {"label": "3", "icon": "", "color": "", "children": [bubble("4")]}]}]}]
        got = self.clean([profile(KATE, nine), profile(KONSOLE, deep), profile(KWRITE, [])])
        self.assertEqual(got[0]["ring"], nine[:8])
        self.assertEqual(got[1]["ring"], config.clean_bubbles(deep))
        self.assertNotIn("children", got[1]["ring"][0]["children"][0]["children"][0])  # three levels at most
        self.assertIsNone(got[2]["ring"])  # nothing of its own: the ring for all apps

    def test_at_most_32_and_one_for_each_app(self):
        many = [profile(f"app{n}") for n in range(33)]
        self.assertEqual([p["app"] for p in self.clean(many)], [f"app{n}" for n in range(32)])
        self.assertEqual(self.clean([profile(KATE, back=COPY), profile(KATE, back=PASTE)]), [profile(KATE, back=COPY)])

    def test_the_rest_of_the_settings_stay_as_they_are(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["haptics"]["battery_saving"] = False
        cfg["keyboard"]["profiles"] = [{"app": KATE, "name": "Kate", "keys": {"0x0f01": NATIVE}}]
        expected = config.clean(copy.deepcopy(cfg))
        cfg["mouse"]["profiles"] = [profile(KATE, RING_A, back=COPY)]
        got = config.clean(cfg)
        self.assertEqual(got["version"], 6)
        self.assertEqual({k: v for k, v in got.items() if k != "mouse"}, {k: v for k, v in expected.items() if k != "mouse"})
        self.assertIs(got["haptics"]["battery_saving"], False)

    def test_what_counts_in_each_app(self):
        cfg = config.clean(dict(copy.deepcopy(config.DEFAULTS), mouse={"profiles": [
            profile(KATE, RING_A, back=COPY, forward=NATIVE), profile(KONSOLE, back=PASTE)]}))
        for app in (None, "org.kde.dolphin"):
            self.assertEqual(config.mouse_buttons(cfg, app), cfg["buttons"])
            self.assertEqual(config.ring_bubbles(cfg, app), cfg["ring"]["bubbles"])
            self.assertIsNone(config.mouse_profile_of(cfg, app))
        self.assertEqual(config.mouse_buttons(cfg, KATE), dict(cfg["buttons"], back=COPY, forward=NATIVE))
        self.assertEqual(config.ring_bubbles(cfg, KATE), RING_A)
        self.assertEqual(config.ring_bubbles(cfg, KONSOLE), cfg["ring"]["bubbles"])  # no ring of its own


class ButtonsPerApp(unittest.TestCase):
    """The engine follows the app in front: the buttons do what its profile says, and only what changes is written."""

    def test_a_button_does_what_the_app_in_front_says(self):
        made = make(self, [profile(KATE, back=COPY)])
        for app, expected in ((None, set()), (KATE, {"KEY_LEFTCTRL", "KEY_C"}), (KONSOLE, set()), (None, set())):
            with self.subTest(app=app):
                front(made, app)
                made._button_down(BACK)
                self.assertEqual(held(made), expected)
                made._button_up(BACK)
                self.assertEqual(held(made), set())

    def test_only_changed_buttons_that_report_otherwise_are_written(self):
        made = make(self, [profile(KATE, back=COPY, forward=PASTE)], buttons={"forward": COPY})
        front(made, KATE)
        self.assertEqual(made.mouse.log, [(BACK, True, False)])  # forward is diverted either way
        front(made, KATE)
        self.assertEqual(len(made.mouse.log), 1)
        front(made, KONSOLE)
        self.assertEqual(made.mouse.log[1:], [(BACK, False, False)])
        cfg = copy.deepcopy(made.cfg)
        cfg["pointer"]["dpi"] = 1200
        set_config(made, cfg)
        self.assertEqual(len(made.mouse.log), 2)  # nothing of the buttons changed
        cfg["buttons"]["middle"] = PASTE
        set_config(made, cfg)
        self.assertEqual(sorted(cid for cid, _divert, _raw in made.mouse.log[2:]), sorted(config.BUTTONS.values()))

    def test_only_the_last_app_counts_after_a_pause(self):
        made = make(self, [profile(KATE, back=COPY), profile(KONSOLE, back=PASTE)])
        front(made, KATE)
        writes = len(made.mouse.log)
        for app in (KONSOLE, KATE):
            made.set_front(app)
            drain(made)
            made._take_front(made._front[1] - 0.01)  # not yet
        made._take_front(float("inf"))
        self.assertEqual(len(made.mouse.log), writes)
        made._button_down(BACK)
        self.assertEqual(held(made), {"KEY_LEFTCTRL", "KEY_C"})

    def test_this_program_in_front_keeps_the_mouse_as_it_was(self):
        made = make(self, [profile(KATE, back=COPY)])
        front(made, KATE)
        writes = len(made.mouse.log)
        front(made, APP_ID)
        self.assertEqual(made.keyboard._front, APP_ID)  # the keyboard follows it
        self.assertEqual(len(made.mouse.log), writes)
        made._button_down(BACK)
        self.assertEqual(held(made), {"KEY_LEFTCTRL", "KEY_C"})

    def test_a_held_button_that_changes_lets_go_at_once_and_its_release_does_nothing(self):
        made = make(self, [profile(KATE, back=COPY)])
        front(made, KATE)
        made._button_down(BACK)
        front(made, KONSOLE)
        self.assertEqual(held(made), set())
        sent = list(made.vinput.sent)
        made._button_up(BACK)
        self.assertEqual(made.vinput.sent, sent)

    def test_a_held_button_that_stays_the_same_is_let_go_of_on_release(self):
        made = make(self, [profile(KATE, back=COPY)], buttons={"forward": PASTE})
        made._button_down(FORWARD)
        front(made, KATE)
        self.assertEqual(held(made), {"KEY_LEFTCTRL", "KEY_V"})
        made._button_up(FORWARD)
        self.assertEqual(held(made), set())

    def test_a_gesture_held_on_a_changed_button_is_called_off(self):
        made = make(self, [profile(KATE, gesture=COPY)], real=False)
        made._button_down(GESTURE)
        front(made, KATE)
        self.assertEqual(made.listener.gestures[-1]["result"], "cancelled")
        self.assertIn((GESTURE, True, False), made.mouse.log)  # no raw movement any more
        made._button_up(GESTURE)
        self.assertEqual(made.executor.ran, [])

    def test_a_gesture_held_on_an_unchanged_button_goes_on(self):
        made = make(self, [profile(KATE, back=COPY)], real=False)
        made._button_down(GESTURE)
        front(made, KATE)
        made._button_up(GESTURE)
        self.assertEqual(made.executor.ran, [made.cfg["buttons"]["gesture"]["tap"]])

    def test_precision_mode_held_on_a_changed_button_ends(self):
        made = make(self, [profile(KATE, back=COPY)], buttons={"back": {"type": "dpi_shift"}})
        made._button_down(BACK)
        front(made, KATE)
        self.assertEqual(made.mouse.dpis, [400, 1000])
        made._button_up(BACK)
        self.assertEqual(made.mouse.dpis, [400, 1000])

    def test_a_shortcut_that_brings_another_app_to_the_front(self):
        for konsole, expected in ((profile(KONSOLE), set()), (profile(KONSOLE, back=COPY), {"KEY_LEFTCTRL", "KEY_C"})):
            with self.subTest(changes=expected == set()):
                made = make(self, [profile(KATE, back=COPY), konsole])
                front(made, KATE)
                made._button_down(BACK)  # it starts Konsole
                made.set_front(KONSOLE)
                drain(made)
                made._take_front(made._front[1] - 0.01)
                self.assertEqual(held(made), {"KEY_LEFTCTRL", "KEY_C"})  # not before the pause
                made._take_front(float("inf"))
                self.assertEqual(held(made), expected)
                made._button_up(BACK)
                self.assertEqual(held(made), set())

    def test_offline_nothing_is_written_and_the_next_connection_writes_what_counts(self):
        made = make(self, [profile(KATE, back=COPY)])
        no_configure_writes(made)
        made.online = False
        front(made, KATE)
        self.assertEqual(made.mouse.log, [])
        made._configure()
        self.assertIn((BACK, True, False), made.mouse.log)

    def test_a_failed_write_is_tried_again_with_what_counts(self):
        made = make(self, [profile(KATE, back=COPY)])
        no_configure_writes(made)
        made.mouse.fail = True
        front(made, KATE)
        self.assertTrue(made._dirty)
        self.assertFalse(made.online)
        made._configure()
        self.assertIn((BACK, True, False), made.mouse.log)

    def test_new_settings_for_a_held_button_of_the_app_in_front_let_go_of_it(self):
        made = make(self, [profile(KATE, back=COPY)])
        front(made, KATE)
        made._button_down(BACK)
        cfg = copy.deepcopy(made.cfg)
        cfg["mouse"]["profiles"][0]["buttons"]["back"] = PASTE
        set_config(made, cfg)
        self.assertEqual(held(made), set())
        made._button_up(BACK)
        self.assertNotIn("KEY_V", tapped(made) + [name for name, _down in made.vinput.sent])
        cfg["buttons"]["back"] = actions.keys("LEFTCTRL", "X")  # hidden by the profile: nothing to write
        writes = len(made.mouse.log)
        set_config(made, cfg)
        self.assertEqual(len(made.mouse.log), writes)

    def test_battery_saving_alone_sends_nothing_to_the_mouse(self):
        made = make(self, [profile(KATE, back=COPY)])
        sent = []
        made._apply_haptics = lambda: sent.append("haptics")
        front(made, KATE)
        made._button_down(BACK)
        writes = len(made.mouse.log)
        for saving in (False, True):
            cfg = copy.deepcopy(made.cfg)
            cfg["haptics"]["battery_saving"] = saving
            set_config(made, cfg)
        self.assertEqual((sent, len(made.mouse.log)), ([], writes))
        self.assertEqual(held(made), {"KEY_LEFTCTRL", "KEY_C"})
        cfg = copy.deepcopy(made.cfg)
        cfg["haptics"]["level"] = 30
        set_config(made, cfg)
        self.assertEqual(sent, ["haptics"])


class MacrosPerApp(unittest.TestCase):
    """The macros a button started stop when the app in front changes what the button does, however many there are,
    held or let go, however often the settings were copied meanwhile; the others play on. Real Executor."""

    LONG = macro("KEY_A", ("wait", 1500), "KEY_B")

    def copy_settings(self, made):
        cfg = copy.deepcopy(made.cfg)
        cfg["pointer"]["dpi"] += 50  # something else changes: every button's action is a new, equal object
        set_config(made, cfg)

    def press(self, made, cid):
        made._button_down(cid)
        made._button_up(cid)

    def test_a_macro_let_go_of_stops_after_equal_copies(self):
        made = make(self, [profile(KATE, back=NATIVE)], buttons={"back": self.LONG, "forward": macro("KEY_Z")})
        self.press(made, BACK)
        wait_for(self, lambda: "KEY_A" in tapped(made))
        self.copy_settings(made)
        self.copy_settings(made)
        self.press(made, FORWARD)  # queued behind it
        start = time.monotonic()
        front(made, KATE)
        wait_for(self, lambda: "KEY_Z" in tapped(made))
        self.assertLess(time.monotonic() - start, 1.2)  # its wait was cut short
        time.sleep(0.2)
        self.assertEqual(tapped(made), ["KEY_A", "KEY_Z"])

    def test_a_repeating_macro_held_stops_and_its_command_runs_on(self):
        repeating = {"type": "macro", "repeat": True, "steps": [{"do": "command", "command": "sleep 60"},
                                                                {"do": "keys", "keys": ["KEY_R"]}, {"do": "delay", "ms": 50}]}
        made = make(self, [profile(KATE, back=NATIVE)], buttons={"back": repeating})
        with mock.patch.object(actions.subprocess, "Popen") as popen:
            made._button_down(BACK)
            wait_for(self, lambda: "KEY_R" in tapped(made))
            self.copy_settings(made)
            self.copy_settings(made)
            front(made, KATE)
            time.sleep(0.2)
            count = len(tapped(made))
            time.sleep(0.3)
            self.assertEqual(len(tapped(made)), count)
            made._button_up(BACK)
        self.assertFalse(popen.return_value.kill.called or popen.return_value.terminate.called)

    def test_a_press_refused_by_a_full_queue_does_not_save_the_macro_playing(self):
        made = make(self, [profile(KATE, back=NATIVE)], buttons={"back": self.LONG, "forward": macro("KEY_Z")})
        self.press(made, BACK)
        wait_for(self, lambda: "KEY_A" in tapped(made))
        for _press in range(5):  # four queue up behind it, the fifth is refused
            self.copy_settings(made)
            self.press(made, BACK)
        self.assertEqual(made.executor.macros._queue.qsize(), 4)
        front(made, KATE)
        runner = made.executor.macros
        wait_for(self, lambda: runner._queue.empty() and runner._playing is None, 1.2)  # all dropped, the wait cut short
        self.press(made, FORWARD)
        wait_for(self, lambda: "KEY_Z" in tapped(made))
        time.sleep(0.3)
        self.assertEqual(tapped(made), ["KEY_A", "KEY_Z"])

    def test_every_start_of_a_changed_button_ends_and_the_other_macros_play_on(self):
        made = make(self, [profile(KATE, back=NATIVE)], buttons={"back": macro("KEY_A", ("wait", 800), "KEY_B"),
                                                                 "forward": macro("KEY_F")})
        keys = actions.Executor(made.vinput, lambda *_args: None, owner="keyboard")
        self.addCleanup(stop, self, keys)
        self.press(made, BACK)
        wait_for(self, lambda: "KEY_A" in tapped(made))
        self.copy_settings(made)
        self.press(made, BACK)
        self.copy_settings(made)
        self.press(made, BACK)
        self.press(made, FORWARD)
        keys.trigger(macro("KEY_K", ("wait", 300), "KEY_L"))
        wait_for(self, lambda: "KEY_K" in tapped(made))
        front(made, KATE)
        wait_for(self, lambda: "KEY_F" in tapped(made) and "KEY_L" in tapped(made))
        time.sleep(0.2)
        self.assertEqual(sorted(tapped(made)), ["KEY_A", "KEY_F", "KEY_K", "KEY_L"])

    def test_nothing_is_kept_of_the_actions_a_button_had(self):
        mark = "marker"
        one, two = macro("KEY_A", name=mark), macro("KEY_B", name=mark)
        made = make(self, buttons={"back": one})
        no_configure_writes(made)

        def live():
            wait_for(self, lambda: made.executor.macros._queue.empty() and made.executor.macros._playing is None)
            gc.collect()
            return sum(1 for item in gc.get_objects() if isinstance(item, dict) and item.get("name") == mark)

        counts = []
        for change in range(30):
            cfg = copy.deepcopy(made.cfg)
            cfg["buttons"]["back"] = two if change % 2 == 0 else one
            set_config(made, cfg)
            self.copy_settings(made)
            self.press(made, BACK)
            counts.append(live())
        self.assertEqual(counts[-1], counts[0])
        made._configure()
        self.assertLessEqual(live(), counts[0])

    def test_two_buttons_with_the_same_macro_object_stop_apart(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["buttons"]["back"] = cfg["buttons"]["forward"] = macro("KEY_A", ("wait", 300), "KEY_B")  # one object
        made = make(self, cfg=cfg)
        self.press(made, FORWARD)
        wait_for(self, lambda: "KEY_A" in tapped(made))
        cfg = copy.deepcopy(made.cfg)
        self.assertIs(cfg["buttons"]["back"], cfg["buttons"]["forward"])  # a copy keeps them one
        cfg["buttons"]["back"] = NATIVE
        set_config(made, cfg)
        wait_for(self, lambda: "KEY_B" in tapped(made))

    def test_a_macro_plays_to_its_end_across_equal_copies(self):
        made = make(self, [profile(KATE, back=self.LONG)], buttons={"back": self.LONG})
        self.press(made, BACK)
        wait_for(self, lambda: "KEY_A" in tapped(made))
        self.copy_settings(made)
        front(made, KATE)  # the same macro there
        wait_for(self, lambda: "KEY_B" in tapped(made), 3.0)


RING_CASES = {  # what the mouse's ring buttons send, without profiles, as they always did
    "hold": ([("down", BACK), ("up", BACK)], ["down", "up"]),
    "tap then press again": ([("down", BACK), ("up", BACK), ("down", BACK), ("up", BACK)], ["down", "up", "down", "up"]),
    "reset while held, then up": ([("down", BACK), ("reset",), ("up", BACK)], ["down", "cancel"]),
    "reset when not held": ([("reset",)], []),
    "two ring buttons, first up first": ([("down", BACK), ("down", FORWARD), ("up", BACK), ("up", FORWARD)],
                                         ["down", "down", "up"]),
    "two ring buttons, last up first": ([("down", BACK), ("down", FORWARD), ("up", FORWARD), ("up", BACK)],
                                        ["down", "down", "up"]),
    "up of a button never pressed": ([("up", BACK)], []),
    "other button between": ([("down", BACK), ("down", MIDDLE), ("up", MIDDLE), ("up", BACK)], ["down", "up"]),
    "down twice": ([("down", BACK), ("down", BACK), ("up", BACK), ("up", BACK)], ["down", "down", "up"]),
}


class RingPerApp(unittest.TestCase):
    """Each ring a button or a key opens gets a number from the engine; a pick runs only while the bubbles of its
    ring still count. The overlay's own messages are played by hand here, in an order they can arrive in."""

    def rig(self, *profiles):
        made = make(self, profiles, real=False)
        return made, made.listener.rings, made.executor.ran

    def pick(self, made, action, session):
        """A pick the overlay made reaches the engine (RingOverlay.triggered -> Engine.ring_action)."""
        made.ring_action(action, session)
        drain(made)

    def test_a_ring_opens_with_the_bubbles_of_the_app_in_front(self):
        made, rings, _ran = self.rig(profile(KATE, RING_A))
        front(made, KATE)
        made._button_down(PANEL)
        self.assertEqual(rings, [("down", 1, RING_A)])
        self.assertIsNot(rings[0][2], made.cfg["mouse"]["profiles"][0]["ring"])
        made._button_up(PANEL)
        set_config(made, dict(copy.deepcopy(made.cfg), mouse={"profiles": []}))
        made._button_down(PANEL)
        self.assertEqual(rings[-1], ("down", 2, made.cfg["ring"]["bubbles"]))

    def test_another_ring_in_front_closes_a_held_one_and_its_pick_runs_nothing(self):
        made, rings, ran = self.rig(profile(KATE, RING_A))
        front(made, KATE)
        made._button_down(PANEL)
        front(made, KONSOLE)
        self.assertEqual(rings[1:], [("cancel", 1, None)])
        made._button_up(PANEL)
        self.assertEqual(len(rings), 2)
        self.pick(made, PICK_A, 1)
        self.assertEqual(ran, [])

    def test_a_ring_left_open_closes_whether_or_not_it_said_so_yet(self):
        for told in (True, False):
            with self.subTest(told=told):
                made, rings, _ran = self.rig(profile(KATE, RING_A))
                front(made, KATE)
                self.assertEqual(rings, [])
                made._button_down(PANEL)
                made._button_up(PANEL)
                if told:
                    made.set_ring_open(True)
                    drain(made)
                front(made, KONSOLE)
                self.assertEqual(rings[-1], ("cancel", 1, None))

    def test_an_old_pick_arriving_after_a_new_ring_runs_nothing(self):
        made, rings, ran = self.rig(profile(KATE, RING_A))
        front(made, KATE)
        made._button_down(PANEL)   # ring 1, Kate's; the overlay's pick of it is still on its way (its timer)
        made._button_up(PANEL)
        front(made, KONSOLE)
        made._button_down(PANEL)   # ring 2, the one for all apps
        self.assertEqual(rings[-1][:2], ("down", 2))
        self.pick(made, PICK_A, 1)
        self.assertEqual(ran, [])
        self.pick(made, PICK_GLOBAL, 2)
        self.assertEqual(ran, [PICK_GLOBAL])

    def test_the_same_ring_in_another_app_keeps_its_picks(self):
        made, _rings, ran = self.rig(profile(KATE, RING_A), profile(KWRITE, RING_A))
        front(made, KATE)
        made._button_down(PANEL)
        made._button_up(PANEL)
        front(made, KWRITE)
        self.pick(made, PICK_A, 1)
        made._button_down(PANEL)
        self.pick(made, PICK_A, 2)
        self.assertEqual(ran, [PICK_A, PICK_A])

    def test_an_old_pick_after_a_new_ring_of_the_same_bubbles_runs(self):
        made, _rings, ran = self.rig()
        made._button_down(PANEL)
        made._button_up(PANEL)
        made._button_down(PANEL)
        self.pick(made, PICK_GLOBAL, 1)
        self.pick(made, PICK_GLOBAL, 2)
        self.assertEqual(ran, [PICK_GLOBAL, PICK_GLOBAL])

    def test_a_late_word_that_an_old_ring_opened_lets_none_of_its_picks_in(self):
        made, _rings, ran = self.rig(profile(KATE, RING_A))
        front(made, KATE)
        made._button_down(PANEL)
        made._button_up(PANEL)
        front(made, KONSOLE)
        made.set_ring_open(True)  # the overlay shows ring 1 only now
        drain(made)
        self.pick(made, PICK_A, 1)
        self.assertEqual(ran, [])
        made._button_down(PANEL)
        self.pick(made, PICK_GLOBAL, 2)
        self.assertEqual(ran, [PICK_GLOBAL])

    def test_the_thumb_wheel_turns_the_ring_until_it_says_it_closed(self):
        made, _rings, _ran = self.rig(profile(KATE, RING_A))
        made.vinput = mock.Mock()
        front(made, KATE)
        made._button_down(PANEL)
        made.set_ring_open(True)
        drain(made)
        front(made, KONSOLE)
        made._on_thumbwheel(5)
        self.assertEqual(len(made.listener.turns), 1)
        made.set_ring_open(False)
        drain(made)
        made._on_thumbwheel(5)
        self.assertEqual(len(made.listener.turns), 1)
        self.assertTrue(made.vinput.hscroll.called)

    def test_a_dial_of_an_old_ring_turns_nothing(self):
        made, _rings, ran = self.rig(profile(KATE, RING_A))
        step = actions.SCROLLS[next(iter(actions.SCROLLS))][2]
        front(made, KATE)
        made._button_down(PANEL)
        front(made, KONSOLE)
        self.pick(made, step, 1)
        self.assertEqual(ran, [])
        made._button_down(PANEL)
        self.pick(made, step, 2)
        self.assertEqual(ran, [step])

    def test_a_number_the_engine_never_gave_runs_nothing(self):
        made, _rings, ran = self.rig()
        made._button_down(PANEL)
        for session in (2, 0, -1, True, "1", 1.0, [1]):
            self.pick(made, PICK_GLOBAL, session)
        self.assertEqual(ran, [])
        self.pick(made, PICK_GLOBAL, 1)
        self.pick(made, PICK_GLOBAL, None)  # a ring another program opened
        self.assertEqual(ran, [PICK_GLOBAL, PICK_GLOBAL])

    def test_a_key_s_ring_has_the_bubbles_of_the_app_in_front_too(self):
        made, rings, _ran = self.rig(profile(KATE, RING_A))
        front(made, KATE)
        made._ring_event(("keyboard", 0x0F01), "down")
        made._button_down(PANEL)  # the mouse takes it
        self.assertEqual(rings, [("down", 1, RING_A), ("down", 2, RING_A)])
        made._ring_event(("keyboard", 0x0F01), "up")
        made._button_up(PANEL)
        self.assertEqual(rings[2:], [("up", None, None)])

    def test_a_profile_of_the_keys_only_leaves_the_mouse_s_ring(self):
        made, rings, _ran = self.rig()
        made.cfg["keyboard"]["profiles"] = [{"app": KATE, "name": "Kate", "keys": {"0x0f01": NATIVE}}]
        made._button_down(PANEL)
        front(made, KATE)
        made._button_up(PANEL)
        self.assertEqual([event for event, _session, _bubbles in rings], ["down", "up"])

    def test_without_profiles_the_ring_buttons_send_what_they_always_did(self):
        for name, (steps, expected) in RING_CASES.items():
            with self.subTest(case=name):
                made, rings, _ran = self.rig()
                made.cfg["buttons"]["back"] = made.cfg["buttons"]["forward"] = {"type": "ring"}
                made.cfg["buttons"]["middle"] = {"type": "none"}
                for step in steps:
                    if step[0] == "reset":
                        made._reset_input_state()
                    else:
                        (made._button_down if step[0] == "down" else made._button_up)(step[1])
                self.assertEqual([event for event, _session, _bubbles in rings], expected)
                downs = [session for event, session, _bubbles in rings if event == "down"]
                self.assertEqual(downs, list(range(1, len(downs) + 1)))
                self.assertEqual([session for event, session, _bubbles in rings if event != "down"],
                                 [None] * (len(rings) - len(downs)))


class Overlay(unittest.TestCase):
    """The overlay keeps the number of the ring it shows and gives it with every pick."""

    def setUp(self):
        self.overlay = ring.RingOverlay(APP, kind=ring.XWAYLAND)
        self.addCleanup(self.overlay.deleteLater)
        self.addCleanup(self.overlay.close)
        self.overlay.configure({"size": 1.0, "style": "bubbles", "bubbles": [bubble("global")]})
        self.picks = []
        self.overlay.triggered.connect(lambda action, session: self.picks.append((action, session)))

    def settle(self, seconds=0.4):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            APP.processEvents()
            time.sleep(0.01)

    def test_it_shows_what_the_engine_sends(self):
        overlay = self.overlay
        overlay.handle("down", 3, RING_A)
        self.assertEqual((overlay._open, overlay._session, overlay._shown), (True, 3, RING_A))
        overlay.handle("down", 4, RING_B)  # pressed again: closed, its bubbles not shown
        self.assertFalse(overlay._open)
        overlay.handle("up")
        overlay.handle("down", 5, None)
        self.assertEqual((overlay._session, overlay._shown), (5, [bubble("global")]))

    def test_a_pick_carries_the_number_its_ring_had_when_it_was_picked(self):
        overlay = self.overlay
        overlay.handle("down", 3, RING_A)
        overlay._choose(0)               # its action follows after a moment
        overlay.handle("cancel", 3)
        overlay.handle("down", 4, RING_B)
        self.assertEqual(overlay._session, 4)
        self.settle()
        self.assertEqual(self.picks, [(PICK_A, 3)])
        overlay._choose(0)
        self.settle()
        self.assertEqual(self.picks[1:], [(RING_B[0]["action"], 4)])

    def test_a_dial_step_carries_its_ring_s_number(self):
        overlay = self.overlay
        name = next(iter(actions.SCROLLS))
        overlay.handle("down", 6, [{"label": "", "icon": "", "color": "", "scroll": name}])
        overlay._placed, overlay._selected = True, 0
        overlay.turn(1)
        self.assertEqual(self.picks, [(actions.SCROLLS[name][2], 6)])

    def dial(self):
        """Ring 6 of one dial, placed, the dial under the pointer. Returns the cues it plays, its step up and down
        as picked, and a function turning the wheel over the dial."""
        overlay = self.overlay
        name = next(iter(actions.SCROLLS))
        overlay.handle("down", 6, [{"label": "", "icon": "", "color": "", "scroll": name}])
        overlay._placed, overlay._selected = True, 0
        cues = []
        overlay.cue.connect(cues.append)
        centre = ring.layout(overlay._style, overlay._ring_rect(), 1)[0][0].center
        _label, _icon, up, down, _click = actions.SCROLLS[name]
        return cues, (up, 6), (down, 6), lambda delta: overlay.wheelAt(centre.x(), centre.y(), delta)

    def test_one_huge_dial_turn_steps_a_bounded_number_and_owes_nothing(self):
        overlay = self.overlay
        cues, up, down, wheel = self.dial()
        most = ring.DIAL_STEPS_MAX
        for clicks, step, click in ((32767 * 20 / 120, up, 1), (-32768 * 20 / 120, down, -1)):  # int16 thumb reports
            with self.subTest(clicks=clicks):
                self.picks.clear()
                cues.clear()
                overlay.turn(clicks)
                self.assertEqual((len(self.picks), len(cues)), (most, most))
                self.assertEqual(self.picks, [step] * most)
                self.assertLess(abs(overlay._wheel), ring.WHEEL_STEP)  # the rest dropped, not owed
                overlay.turn(click)  # the next ordinary click is one step
                self.assertEqual((self.picks, len(cues)), ([step] * (most + 1), most + 1))
        with self.subTest(wheel=-32768):
            self.picks.clear()
            cues.clear()
            wheel(-32768)
            self.assertEqual((len(self.picks), len(cues)), (most, most))
            self.assertEqual(self.picks, [down] * most)
            self.assertLess(abs(overlay._wheel), ring.WHEEL_STEP)

    def test_ordinary_dial_turns_send_what_they_always_did(self):
        overlay = self.overlay
        cues, up, down, wheel = self.dial()

        def turn(clicks):
            return lambda: overlay.turn(clicks)

        def over(delta):
            return lambda: wheel(delta)

        # what is done, the steps it sends, the dial's angle and the wheel travel left after it
        done = [("turn 1", turn(1), [up], 30.0, 0.0), ("turn -1", turn(-1), [down], 0.0, 0.0),
                ("turn 2", turn(2), [up, up], 60.0, 0.0)]
        done += [(f"sixth {n}", turn(1 / 6), [], 60.0, 20.0 * n) for n in range(1, 6)]
        done += [("sixth 6", turn(1 / 6), [up], 90.0, 0.0), ("wheel 120", over(120), [up], 120.0, 0.0)]
        done += [(f"wheel 15 #{n}", over(15), [], 120.0, 15.0 * n) for n in range(1, 8)]
        done += [("wheel 15 #8", over(15), [up], 150.0, 0.0)]
        done += [(f"wheel 15 again #{n}", over(15), [], 150.0, 15.0 * n) for n in range(1, 8)]
        sent = []
        for name, call, steps, angle, left in done:
            call()
            sent += steps
            self.assertEqual((self.picks, overlay._angle, overlay._wheel), (sent, angle, left), name)
        self.assertEqual((overlay._wheel, len(cues)), (105.0, len(sent)))

    def test_a_numbered_cancel_closes_engine_rings_only(self):
        overlay = self.overlay
        self.assertTrue(overlay.open(RING_B))  # another program's
        overlay.handle("cancel", 7)
        self.assertTrue(overlay._open)
        overlay.handle("cancel")  # as on a reset: closes whatever is shown
        self.assertFalse(overlay._open)
        overlay.handle("down", 8, RING_A)
        overlay.handle("cancel", 7)  # a later ring stays
        self.assertTrue(overlay._open)
        overlay.handle("cancel", 8)
        self.assertFalse(overlay._open)
        overlay.handle("down", 9, RING_A)
        overlay.handle("up")  # left open by a tap
        overlay.handle("cancel", 9)
        self.assertFalse(overlay._open)

    def rig(self, *profiles):
        """The engine and this overlay, wired as the program wires them, on one thread."""
        made = make(self, profiles, real=False)
        made.listener.on_ring = self.overlay.handle
        self.overlay.triggered.connect(made.ring_action)
        return made

    def test_rings_other_programs_open_stay_and_their_picks_run(self):
        made = self.rig(profile(KATE, RING_A, back=COPY))
        service = api.MouseApi(Store(copy.deepcopy(config.DEFAULTS), _Engine()), self.overlay, lambda: None)
        front(made, KATE)
        for opened, shown in ((service.OpenRing, [bubble("global")]), (lambda: service.ShowRing(json.dumps(RING_B)), RING_B)):
            with self.subTest(shown=shown[0]["label"]):
                self.assertTrue(opened())
                self.assertEqual(self.overlay._shown, shown)
                front(made, KONSOLE if made._app == KATE else KATE)  # the ring of the app in front changes
                self.assertTrue(self.overlay._open)
                self.overlay._choose(0)
                self.settle()
                drain(made)
                self.assertEqual(made.executor.ran[-1], shown[0]["action"])

    def test_a_button_changed_while_another_program_s_ring_is_open_leaves_that_ring(self):
        made = self.rig(profile(KATE, RING_A), profile(KWRITE, RING_A, panel=COPY))
        front(made, KATE)
        made._button_down(PANEL)          # ring 1, held
        self.overlay._choose(0)           # a bubble picked: the overlay closes, the button is still down
        self.assertTrue(api.MouseApi(Store(copy.deepcopy(config.DEFAULTS), _Engine()), self.overlay, lambda: None).ShowRing(json.dumps(RING_B)))
        front(made, KWRITE)               # the same ring there, but the button does something else
        self.assertTrue(self.overlay._open)
        self.assertIsNone(made._ring_by)
        made._button_up(PANEL)
        self.assertTrue(self.overlay._open)
        self.settle()
        drain(made)
        self.assertEqual(made.executor.ran, [PICK_A])  # the pick made before still runs: the same ring
        self.overlay.close()
        front(made, KATE)
        made._button_down(PANEL)          # an engine ring on screen, its button changed: it closes
        self.assertTrue(self.overlay._open)
        front(made, KWRITE)
        self.assertFalse(self.overlay._open)


class StoreAndOthers(unittest.TestCase):
    """The Store as the pages use it, other programs (Set, Get), the settings file, and what never leaves the engine."""

    def setUp(self):
        self.store = Store(copy.deepcopy(config.DEFAULTS), _Engine())
        self.store.flush = lambda: None
        self.store._save_timer.timeout.disconnect()
        self.api = api.MouseApi(self.store, None, lambda: None)

    def profiles(self):
        return self.store.cfg["mouse"]["profiles"]

    def test_the_store_as_the_pages_use_it(self):
        store = self.store
        for bad in ("Kate", "", "x" * 129, "a\tb", None, 3):
            self.assertFalse(store.add_mouse_profile(bad))
        self.assertTrue(store.add_mouse_profile(KATE))
        self.assertFalse(store.add_mouse_profile(KATE))
        self.assertEqual(self.profiles(), [{"app": KATE, "name": KATE, "buttons": {}, "ring": None}])
        before = copy.deepcopy(store.cfg)
        for key, action in (("back", {"type": "gestures"}), ("left", COPY), ("back", {"type": "ring", "x": 1}),
                            ("back", [])):
            self.assertFalse(store.set_profile_button(KATE, key, action))
        self.assertFalse(store.set_profile_button(KONSOLE, "back", COPY))  # no profile
        self.assertFalse(store.set_profile_ring(KATE, []))
        self.assertFalse(store.set_profile_ring(KATE, "x"))
        self.assertFalse(store.set_profile_ring(KONSOLE, RING_A))
        self.assertEqual(store.cfg, before)
        self.assertTrue(store.set_profile_button(KATE, "back", NATIVE))
        self.assertTrue(store.set_profile_button(KATE, "forward", COPY))
        self.assertTrue(store.set_profile_button(KATE, "forward", None))
        self.assertTrue(store.set_profile_ring(KATE, RING_A))
        self.assertEqual(self.profiles()[0], {"app": KATE, "name": KATE, "buttons": {"back": NATIVE}, "ring": RING_A})
        self.assertTrue(store.set_profile_ring(KATE, None))
        self.assertIsNone(self.profiles()[0]["ring"])
        for n in range(config.MAX_PROFILES - 1):
            self.assertTrue(store.add_mouse_profile(f"app{n}"))
        self.assertFalse(store.add_mouse_profile("one-too-many"))
        heard = []
        store.mouse_app_changed.connect(lambda: heard.append(store.mouse_app))
        store.set_mouse_app("app0")
        store.remove_mouse_profile("app0")
        self.assertIsNone(store.mouse_profile("app0"))
        self.assertEqual((store.mouse_app, heard), (None, ["app0", None]))
        self.assertEqual(config.clean(copy.deepcopy(store.cfg)), store.cfg)
        self.assertNotIn("mouse_app", json.dumps(store.cfg))

    def test_restoring_the_defaults_removes_them(self):
        self.store.add_mouse_profile(KATE)
        self.store.cfg["flow"]["id"] = "here"
        self.store.restore_defaults()
        self.assertEqual(self.profiles(), [])
        self.assertEqual(self.store.cfg["flow"]["id"], "here")

    def test_other_programs_set_what_may_be_and_are_refused_the_rest(self):
        value = [profile(KATE, RING_A, back=COPY)]
        self.assertTrue(self.api.Set("mouse.profiles", json.dumps(value)))
        self.assertEqual(self.profiles(), value)
        self.assertTrue(self.api.Set("mouse.profiles.0.buttons.back", json.dumps(NATIVE)))
        before = copy.deepcopy(self.store.cfg)
        for path, bad in (("mouse.profiles.0.buttons.back", {"type": "gestures"}), ("mouse.profiles.0.ring", []),
                          ("mouse.profiles", [dict(value[0], haptics={})]), ("mouse.profiles", value * 2),
                          ("mouse.profiles", [profile(f"a{n}") for n in range(33)]), ("mouse.profiles.0.app", "Kate"),
                          ("mouse.profiles", [profile(KATE, back={"type": "dpi_shift", "x": 1})])):
            with self.subTest(path=path, value=json.dumps(bad)[:60]):
                self.assertFalse(self.api.Set(path, json.dumps(bad)))
                self.assertEqual(self.store.cfg, before)
        self.assertEqual(json.loads(self.api.Get("buttons.back")), config.DEFAULTS["buttons"]["back"])

    def test_the_settings_file_holds_what_was_saved_never_the_app_in_front(self):
        made = make(self, [profile(KATE, back=COPY)])
        self.store.cfg["mouse"]["profiles"] = [profile(KATE, back=COPY)]
        front(made, KATE)
        exported = config.export_data(self.store.cfg)
        expected = copy.deepcopy(self.store.cfg)
        expected["flow"].update(copy.deepcopy(config.LOCAL_FLOW))
        self.assertEqual(json.loads(exported)["config"], json.loads(config.to_bytes(expected)))
        current = copy.deepcopy(self.store.cfg)
        current["flow"]["id"] = "this-one"
        back = config.import_data(exported, current)
        self.assertEqual(back["mouse"], self.store.cfg["mouse"])
        self.assertEqual(back["flow"]["id"], "this-one")

    def test_the_app_in_front_is_never_logged_published_or_told(self):
        secret = "org.example.secret"
        made = make(self, [profile(secret, RING_A, back=COPY)])
        made._publish = lambda: engine.Engine._publish(made)
        with self.assertLogs("mxoptions", logging.DEBUG) as logs:
            logging.getLogger("mxoptions").debug("start")
            front(made, secret)
            made._button_down(PANEL)
            front(made, KONSOLE)
            made._publish()
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertNotIn("secret", json.dumps(made.listener.states, default=str))
        self.assertNotIn("secret", json.dumps(made.cfg["buttons"]) + json.dumps(made.cfg["ring"]))
        self.store.cfg = copy.deepcopy(made.cfg)
        self.assertEqual(json.loads(self.api.GetConfig()), made.cfg)  # the saved profile only
        self.assertEqual(json.loads(self.api.Get("buttons.back")), config.DEFAULTS["buttons"]["back"])


class _Engine:
    """Stands in for the engine behind a Store."""

    def __getattr__(self, name):
        return lambda *args: None


class FakeFront(QObject):
    changed = Signal()

    def __init__(self, supported=True):
        super().__init__()
        self.supported, self.last_other, self.wanted = supported, None, []

    def want(self, who, on):
        self.wanted.append((who, on))


class Pages(unittest.TestCase):
    """The Buttons and Ring pages: whose settings they show, and one app's buttons and ring made and changed."""

    def make_store(self, supported=True):
        store = Store(copy.deepcopy(config.DEFAULTS), _Engine())
        store.flush = lambda: None
        store._save_timer.timeout.disconnect()
        store.front = FakeFront(supported)
        return store

    def page(self, kind, store):
        page = kind(store)
        self.addCleanup(page.deleteLater)
        page.resize(1100, 700)
        page.show()
        return page

    def entries(self, chooser):
        return [chooser.apps.itemText(i) for i in range(chooser.apps.count())]

    def test_buttons_of_one_app(self):
        store = self.make_store()
        store.set_button("back", COPY)
        page = self.page(pages.ButtonsPage, store)
        chooser = page.apps
        self.assertIn((chooser, True), store.front.wanted)
        self.assertEqual((self.entries(chooser), chooser.add_app.isEnabled(), chooser.remove_app.isEnabled()),
                         (["All apps"], False, False))
        page.view.select("back")
        self.assertTrue(page.picker.list.inherit_row.isHidden())  # all apps: as today
        self.assertFalse(page.picker.list._rows["gestures"].isHidden())
        store.front.last_other = KATE
        store.front.changed.emit()
        self.assertIn(KATE, chooser.hint.text())
        QTest.mouseClick(chooser.add_app, Qt.MouseButton.LeftButton)
        self.assertEqual((store.mouse_app, self.entries(chooser), chooser.apps.currentIndex()), (KATE, ["All apps", KATE], 1))
        self.assertFalse(page.picker.list.inherit_row.isHidden())
        self.assertTrue(page.picker.list.inherit_row.isChecked())  # nothing of its own yet
        self.assertFalse(page.picker.list._rows["gestures"].isHidden())  # an app's own gestures may be given here
        self.assertTrue(page.picker.list._rows["spotlight"].isHidden())
        page.picker.list.search.setText("spot")
        self.assertTrue(page.picker.list._rows["spotlight"].isHidden())  # not by a search either
        page.picker.list.search.clear()
        page.picker.list._rows["default"].click()
        self.assertEqual(store.mouse_profile(KATE)["buttons"], {"back": NATIVE})
        self.assertEqual(store.cfg["buttons"]["back"], COPY)  # every other app: unchanged
        self.assertEqual(page.view._chips["back"].value.text(), actions.describe(NATIVE))  # what it does there
        page.picker.list.inherit_row.click()
        self.assertEqual(store.mouse_profile(KATE)["buttons"], {})
        chooser.apps.setCurrentIndex(0)
        self.assertIsNone(store.mouse_app)
        self.assertTrue(page.picker.list.inherit_row.isHidden())
        self.assertFalse(page.picker.list._rows["gestures"].isHidden())
        chooser.apps.setCurrentIndex(1)
        with mock.patch.object(pages.QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes):
            QTest.mouseClick(chooser.remove_app, Qt.MouseButton.LeftButton)
        self.assertEqual((store.cfg["mouse"]["profiles"], store.mouse_app, self.entries(chooser)), ([], None, ["All apps"]))
        page.hide()
        self.assertEqual(store.front.wanted[-1], (chooser, False))

    def test_the_ring_of_one_app(self):
        store = self.make_store()
        store.set("ring", "bubbles", [bubble("g1"), bubble("g2")])
        store.add_mouse_profile(KATE)
        page = self.page(ring_page.RingPage, store)
        self.assertTrue(page.opens_with.isVisible() and page.own.isHidden() and page.shared.isHidden())
        store.set_mouse_app(KATE)
        self.assertFalse(page.editor.isEnabled())  # it uses the ring for all apps
        self.assertFalse(page.presets.isEnabled())
        self.assertEqual(page.own.text(), "Make a ring for this app")
        self.assertIn("uses the ring for all apps", page.apps.hint.text())
        self.assertTrue(page.opens_with.isHidden())
        self.assertTrue(page.shared.isVisible())
        page._add()  # nothing to add to
        self.assertIsNone(store.mouse_profile(KATE)["ring"])
        page.own.click()
        self.assertEqual(store.mouse_profile(KATE)["ring"], store.cfg["ring"]["bubbles"])
        self.assertTrue(page.editor.isEnabled() and page.presets.isEnabled())
        global_ring = copy.deepcopy(store.cfg["ring"]["bubbles"])
        page._add()
        self.assertEqual(len(store.mouse_profile(KATE)["ring"]), len(global_ring) + 1)
        page._field_changed("label", "only here")
        self.assertEqual(store.cfg["ring"]["bubbles"], global_ring)  # the ring for all apps: unchanged
        with mock.patch.object(ring_page.QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes):
            page._apply_preset("x", lambda: [bubble("preset")])
        self.assertEqual(store.mouse_profile(KATE)["ring"], [bubble("preset")])
        page.style.selected.emit("pie")
        page.size.changed.emit(1.2)
        self.assertEqual((store.cfg["ring"]["style"], store.cfg["ring"]["size"]), ("pie", 1.2))  # for all apps
        self.assertEqual(page.own.text(), "Use the ring for all apps")
        with mock.patch.object(ring_page.QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes):
            page.own.click()
        self.assertIsNone(store.mouse_profile(KATE)["ring"])
        store.set_profile_button(KATE, "panel", COPY)
        self.assertTrue(page.unassigned.isVisible())  # no button opens the ring there
        store.set_mouse_app(None)
        self.assertTrue(page.unassigned.isHidden())

    def test_both_pages_show_the_same_app_and_go_back_when_it_goes(self):
        store = self.make_store()
        store.add_mouse_profile(KATE)
        buttons, rings = self.page(pages.ButtonsPage, store), self.page(ring_page.RingPage, store)
        buttons.apps.apps.setCurrentIndex(1)
        self.assertEqual(rings.apps.apps.currentIndex(), 1)
        store.remove_mouse_profile(KATE)
        self.assertEqual((buttons.apps.apps.currentIndex(), rings.apps.apps.currentIndex(), store.mouse_app), (0, 0, None))
        store.add_mouse_profile(KATE)
        store.set_mouse_app(KATE)
        store.restore_defaults()
        self.assertEqual((rings.apps.apps.currentIndex(), store.mouse_app), (0, None))

    def test_where_the_desktop_does_not_say_the_pages_say_why(self):
        from mxoptions import i18n
        self.addCleanup(i18n.setup, "en")
        for language, words in (("en", "does not tell"), ("uk", "не повідомляє")):
            with self.subTest(language=language):
                i18n.setup(language)
                store = self.make_store(supported=False)
                store.front.last_other = KATE
                store.cfg["mouse"]["profiles"] = [profile(KATE)]
                page = self.page(pages.ButtonsPage, store)
                self.assertFalse(page.apps.apps.isEnabled() or page.apps.add_app.isEnabled())
                self.assertEqual(self.entries(page.apps), [i18n._("All apps")])
                self.assertIn(words, page.apps.hint.text())

    def test_names_for_assistive_technology_and_the_tab_order(self):
        from mxoptions import i18n
        self.addCleanup(i18n.setup, "en")
        for language, combo, remove in (("en", "Settings for", "Remove the settings of this app"),
                                        ("uk", "Налаштування для", "Вилучити налаштування цієї програми")):
            with self.subTest(language=language):
                i18n.setup(language)
                page = self.page(pages.ButtonsPage, self.make_store())
                chooser = page.apps
                self.assertEqual((chooser.apps.accessibleName(), chooser.remove_app.accessibleName()), (combo, remove))
                label = chooser.findChildren(pages.QLabel)[0]
                self.assertIs(label.buddy(), chooser.apps)
                order = [chooser.apps.nextInFocusChain(), chooser.add_app.nextInFocusChain()]
                self.assertEqual(order, [chooser.add_app, chooser.remove_app])


class Wiring(unittest.TestCase):
    """How the program connects the engine, the overlay and the watcher of the app in front (app.py). The program
    itself is not started here: that would install its tray icon and take signals; its source is read instead."""

    def setUp(self):
        from mxoptions import app
        self.app = app
        with open(app.__file__, encoding="utf-8") as f:
            self.tree = ast.parse(f.read())

    def test_picks_reach_the_engine_with_their_ring_s_number(self):
        connects = {(ast.unparse(node.func.value), ast.unparse(node.args[0])) for node in ast.walk(self.tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "connect"}
        self.assertIn(("overlay.triggered", "engine.ring_action"), connects)
        self.assertNotIn(("overlay.triggered", "engine.trigger_action"), connects)
        self.assertIn(("bridge.ring", "overlay.handle"), connects)
        bridge = self.app.Bridge()
        heard = []
        bridge.ring.connect(lambda *args: heard.append(args))
        bridge.on_ring("down", 3, RING_A)
        bridge.on_ring("cancel")
        self.assertEqual(heard, [("down", 3, RING_A), ("cancel", None, None)])

    def test_the_app_in_front_is_followed_for_mouse_profiles_too(self):
        wants = [node.args[1] for node in ast.walk(self.tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "want"
                 and isinstance(node.args[0], ast.Constant) and node.args[0].value == "profiles"]
        self.assertEqual(len(wants), 2)
        for expression in wants:
            code = compile(ast.Expression(expression), "app.py", "eval")
            for keyboard, mouse, expected in (([], [], False), ([1], [], True), ([], [1], True)):
                cfg = {"keyboard": {"profiles": keyboard}, "mouse": {"profiles": mouse}}
                store = mock.Mock(cfg=cfg)
                self.assertIs(eval(code, {"bool": bool}, {"cfg": cfg, "store": store}), expected)


from mxoptions import device, vinput  # noqa: E402

DOLPHIN = "org.kde.dolphin"
STEP_UP = -6  # one click of the thumb wheel rolled up (this mouse reports rolling up as negative, 20/120 resolution)
TAB, TAB_BACK = engine.THUMB_STEPS["tabs"][1:]
DESKTOP_RIGHT = engine.THUMB_STEPS["desktops"][1]


def own(app, function):
    return {"app": app, "function": function}


class ThumbMouse(Mouse):
    """Writes the thumb wheel's reporting into the same log as the buttons': ("thumb", diverted, inverted)."""

    def __init__(self):
        super().__init__()
        self.thumb_fail = False

    def set_thumbwheel(self, diverted, invert):
        if self.thumb_fail:
            self.thumb_fail = False
            raise hidpp.HidppError("asleep")
        self.log.append(("thumb", diverted, invert))


class Wheels(support.FakeInput):
    """The virtual input, with what is scrolled sideways and up or down."""

    def __init__(self):
        super().__init__()
        self.sideways, self.scrolled = [], []

    def hscroll(self, value):
        self.sideways.append(value)

    def scroll(self, value):
        self.scrolled.append(value)


def thumb_engine(test, profiles=(), entries=(), **sections):
    """An engine whose mouse and virtual input write down what the thumb wheel does; ``entries``: mouse.thumbwheel."""
    made = make(test, profiles, real=False, **sections)
    made.cfg["mouse"]["thumbwheel"] = copy.deepcopy(list(entries))
    made.mouse, made.vinput = ThumbMouse(), Wheels()
    made.executor.close = lambda: None
    made.played = []
    made._play = lambda waveform, wait=True: made.played.append(waveform)
    return made


def thumbs(made):
    return [entry for entry in made.mouse.log if entry[0] == "thumb"]


def turn(made, clock, rotation, times=1):
    for _ in range(times):
        clock.now += 0.05
        made._on_thumbwheel(rotation)


def edited(made, change):
    """The engine's settings with ``change`` made to a copy, given to it as the Store would."""
    cfg = copy.deepcopy(made.cfg)
    change(cfg)
    set_config(made, cfg)


class ThumbWheelSettings(unittest.TestCase):
    """mouse.thumbwheel in the settings: the thumb wheel functions of single apps, kept, left out or resolved."""

    def clean(self, entries, profiles=(profile(KATE), profile(KONSOLE))):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["mouse"]["profiles"] = copy.deepcopy(list(profiles))
        cfg["mouse"]["thumbwheel"] = entries
        return config.clean(cfg)["mouse"]["thumbwheel"]

    def test_every_function_of_an_app_with_a_profile_is_kept_in_its_order(self):
        apps = [f"app{n}" for n in range(len(config.THUMB_FUNCTIONS))]
        entries = [own(app, function) for app, function in zip(reversed(apps), config.THUMB_FUNCTIONS)]
        self.assertEqual(self.clean(copy.deepcopy(entries), [profile(app) for app in apps]), entries)

    def test_what_is_not_a_function_of_an_app_with_a_profile_is_left_out(self):
        for bad in (own(KWRITE, "zoom"), own(KATE, "spin"), own(KATE, None), own(KATE, ["zoom"]), own(KATE, {"a": 1}),
                    own(KATE, 3), own(["x"], "zoom"), own({"x": 1}, "zoom"), own(3, "zoom"), own(None, "zoom"),
                    "zoom", [KATE, "zoom"], None, {"app": KATE}, {"function": "zoom"}, own("Kate", "zoom")):
            with self.subTest(entry=bad):
                self.assertEqual(self.clean([bad, own(KONSOLE, "tabs")]), [own(KONSOLE, "tabs")])
        self.assertEqual(self.clean([own(KATE, "zoom"), own(KATE, "tabs"), own(KONSOLE, "volume")]),
                         [own(KATE, "zoom"), own(KONSOLE, "volume")])  # the first for an app

    def test_other_keys_of_an_entry_are_left_out_and_its_function_kept(self):
        self.assertEqual(self.clean([dict(own(KATE, "zoom"), speed=2.0, later={"x": 1})]), [own(KATE, "zoom")])

    def test_a_list_it_is_not_is_none_and_the_rest_stays_as_it_is(self):
        for bad in ("zoom", {KATE: "zoom"}, None, 3):
            with self.subTest(value=bad):
                self.assertEqual(self.clean(bad), [])
        self.assertEqual(config.DEFAULTS["version"], 6)
        older = copy.deepcopy(config.DEFAULTS)
        older["mouse"] = {"profiles": [profile(KATE, back=COPY)]}  # written by a version without this list
        older["thumbwheel"]["function"] = "tabs"
        cleaned = config.clean(older)
        self.assertEqual(cleaned["mouse"], {"profiles": [profile(KATE, back=COPY)], "thumbwheel": [], "thumbwheel_keys": [], "gestures": []})
        with_entries = copy.deepcopy(older)
        with_entries["mouse"]["thumbwheel"] = [own(KATE, "zoom")]
        again = config.clean(with_entries)
        self.assertEqual(again["mouse"]["thumbwheel"], [own(KATE, "zoom")])
        again["mouse"]["thumbwheel"] = []
        self.assertEqual(again, cleaned)

    def test_one_for_each_profile_at_most(self):
        apps = [f"app{n}" for n in range(config.MAX_PROFILES + 1)]
        kept = self.clean([own(app, "zoom") for app in apps], [profile(app) for app in apps])
        self.assertEqual(kept, [own(app, "zoom") for app in apps[:config.MAX_PROFILES]])

    def test_what_counts_in_each_app(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["thumbwheel"].update(function="tabs", invert=True, speed=1.5, ticks=False)
        cfg["mouse"]["profiles"] = [profile(KATE), profile(KONSOLE)]
        cfg["mouse"]["thumbwheel"] = [own(KATE, "zoom"), own(KWRITE, "volume")]  # KWrite: no profile, never cleaned
        expected = {"function": "tabs", "invert": True, "speed": 1.5, "ticks": False, "up": [], "down": []}
        for app in (None, "org.example.unknown", KONSOLE, KWRITE):
            with self.subTest(app=app):
                self.assertIsNone(config.app_thumb_function(cfg, app))
                self.assertEqual(config.thumbwheel(cfg, app), expected)
        self.assertEqual(config.app_thumb_function(cfg, KATE), "zoom")
        self.assertEqual(config.thumbwheel(cfg, KATE), dict(expected, function="zoom"))
        self.assertEqual(cfg["thumbwheel"]["function"], "tabs")  # resolving changes nothing


class ThumbWheelPerApp(unittest.TestCase):
    """The engine following the app in front with the thumb wheel: what a turn does, what the mouse is told, and
    that a step begun in one app is never finished in another."""

    def setUp(self):
        self.clock = support.Clock()
        self.clock.__enter__()
        self.addCleanup(self.clock.__exit__)

    def test_the_app_in_front_turns_its_own_function(self):
        made = thumb_engine(self, [profile(KATE), profile(KONSOLE)], [own(KATE, "volume")])
        front(made, KATE)
        turn(made, self.clock, STEP_UP)
        self.assertEqual((made.executor.ran, made.vinput.sideways), ([actions.keys("VOLUMEUP")], []))
        front(made, KONSOLE)
        turn(made, self.clock, STEP_UP)
        self.assertEqual((len(made.executor.ran), made.vinput.sideways), (1, [vinput.NOTCH]))

    def test_the_mouse_is_told_only_when_the_thumb_wheel_changes_between_scrolling_by_itself_and_not(self):
        made = thumb_engine(self, [profile(KATE), profile(KWRITE), profile(KONSOLE), profile(DOLPHIN, back=COPY)],
                            [own(KATE, "volume"), own(KWRITE, "zoom"), own(KONSOLE, "zoom")],
                            thumbwheel={"invert": True})
        front(made, KATE)  # sideways scrolling by itself at speed 1 -> volume: goes to the program
        self.assertEqual(made.mouse.log, [("thumb", True, False)])
        front(made, KWRITE)  # volume -> zoom
        front(made, KONSOLE)  # zoom -> zoom
        self.assertEqual(made.mouse.log, [("thumb", True, False)])
        front(made, DOLPHIN)  # its button and the thumb wheel both: the buttons first, in one go
        self.assertEqual(made.mouse.log, [("thumb", True, False), (BACK, True, False), ("thumb", False, True)])

    def test_a_step_begun_in_one_app_is_not_finished_in_another(self):
        made = thumb_engine(self, [profile(KATE), profile(KONSOLE)], [own(KATE, "tabs"), own(KONSOLE, "desktops")])
        front(made, KATE)
        turn(made, self.clock, STEP_UP, 2)  # two of the three clicks a tab takes
        front(made, KONSOLE)
        turn(made, self.clock, STEP_UP, 3)  # three of the five a desktop takes
        self.assertEqual(made.executor.ran, [])
        turn(made, self.clock, STEP_UP, 2)
        self.assertEqual(made.executor.ran, [DESKTOP_RIGHT])

    def test_the_same_function_in_the_next_app_goes_on_with_the_step(self):
        made = thumb_engine(self, [profile(KATE), profile(KONSOLE)], [own(KATE, "tabs")], thumbwheel={"function": "tabs"})
        front(made, KATE)
        turn(made, self.clock, STEP_UP, 2)
        front(made, KONSOLE)  # it has none of its own: tabs, as for all apps
        turn(made, self.clock, STEP_UP)
        self.assertEqual((made.executor.ran, thumbs(made)), ([TAB], []))

    def test_part_of_a_sideways_scroll_carries_over_only_with_the_same_function(self):
        made = thumb_engine(self, [profile(KATE), profile(KONSOLE), profile(KWRITE)], [own(KWRITE, "zoom")],
                            thumbwheel={"speed": 1.33})
        front(made, KATE)
        turn(made, self.clock, STEP_UP)
        front(made, KONSOLE)
        turn(made, self.clock, STEP_UP)
        self.assertEqual(made.vinput.sideways, [159, 160])  # 159.6 a click: the part left over goes on
        front(made, KWRITE)
        front(made, KONSOLE)
        turn(made, self.clock, STEP_UP)
        self.assertEqual(made.vinput.sideways, [159, 160, 159])

    def test_this_program_in_front_keeps_the_thumb_wheel_of_the_app_before(self):
        made = thumb_engine(self, [profile(KATE)], [own(KATE, "tabs")])
        front(made, KATE)
        turn(made, self.clock, STEP_UP, 2)
        writes = len(made.mouse.log)
        front(made, APP_ID)
        turn(made, self.clock, STEP_UP)
        self.assertEqual((made.executor.ran, len(made.mouse.log)), ([TAB], writes))

    def test_only_the_last_app_counts_after_a_pause(self):
        made = thumb_engine(self, [profile(KATE), profile(KONSOLE)], [own(KATE, "tabs"), own(KONSOLE, "desktops")])
        front(made, KATE)
        turn(made, self.clock, STEP_UP, 2)
        writes = len(made.mouse.log)
        for app in (KONSOLE, KATE):
            made.set_front(app)
            drain(made)
        made._take_front(float("inf"))
        turn(made, self.clock, STEP_UP)
        self.assertEqual((made.executor.ran, len(made.mouse.log)), ([TAB], writes))

    def test_while_the_ring_is_open_the_thumb_wheel_turns_its_dials_in_every_app(self):
        made = thumb_engine(self, [profile(KATE), profile(KONSOLE)], [own(KATE, "zoom")])
        front(made, KATE)
        made.set_ring_open(True)
        drain(made)
        turn(made, self.clock, STEP_UP)
        front(made, KONSOLE)
        turn(made, self.clock, STEP_UP)
        self.assertEqual((made.listener.turns, made.vinput.scrolled, made.vinput.sideways), ([1.0, 1.0], [], []))
        made.set_ring_open(False)
        drain(made)
        turn(made, self.clock, STEP_UP)
        self.assertEqual((len(made.listener.turns), made.vinput.sideways), (2, [vinput.NOTCH]))

    def test_offline_nothing_is_written_and_the_next_connection_writes_what_counts(self):
        made = thumb_engine(self, [profile(KATE)], [own(KATE, "volume")])
        made.online = False
        front(made, KATE)
        self.assertEqual(made.mouse.log, [])
        for name in ("_apply_pointer", "_apply_wheel", "_apply_haptics"):
            setattr(made, name, lambda: None)
        made._read_status = lambda **_kwargs: None
        made._configure()
        self.assertEqual(thumbs(made), [("thumb", True, False)])

    def test_a_failed_thumb_wheel_write_is_tried_again_with_what_counts(self):
        made = thumb_engine(self, [profile(KATE)], [own(KATE, "volume")])
        made.mouse.thumb_fail = True
        front(made, KATE)
        self.assertEqual((made._dirty, made.online, made.mouse.log), (True, False, []))
        for name in ("_apply_pointer", "_apply_wheel", "_apply_haptics"):
            setattr(made, name, lambda: None)
        made._read_status = lambda **_kwargs: None
        made._configure()
        self.assertEqual((thumbs(made), made._dirty), ([("thumb", True, False)], False))

    def test_new_settings_count_for_the_app_in_front(self):
        made = thumb_engine(self, [profile(KATE)], [own(KATE, "tabs")])
        front(made, KATE)
        self.assertEqual(thumbs(made), [("thumb", True, False)])
        turn(made, self.clock, STEP_UP, 2)
        edited(made, lambda cfg: cfg["thumbwheel"].update(function="volume"))  # hidden by Kate's own: no change
        self.assertEqual(len(thumbs(made)), 1)
        turn(made, self.clock, STEP_UP)
        self.assertEqual(made.executor.ran, [TAB])
        turn(made, self.clock, STEP_UP, 2)
        edited(made, lambda cfg: cfg["mouse"]["thumbwheel"][0].update(function="desktops"))
        self.assertEqual(len(thumbs(made)), 2)
        turn(made, self.clock, STEP_UP, 3)
        self.assertEqual(made.executor.ran, [TAB])  # the two clicks of the tab before do not count
        edited(made, lambda cfg: cfg["thumbwheel"].update(speed=1.5))  # for every app, Kate too
        self.assertEqual(len(thumbs(made)), 3)
        edited(made, lambda cfg: cfg["mouse"].update(profiles=[]))  # its entry, not cleaned here, counts no more
        self.assertEqual(len(thumbs(made)), 4)
        turn(made, self.clock, STEP_UP)
        self.assertEqual(made.executor.ran, [TAB, actions.keys("VOLUMEUP")])

    def test_offline_a_step_begun_under_the_function_before_is_not_finished_either(self):
        for entries, change in (([own(KATE, "tabs")], lambda cfg: cfg["mouse"]["thumbwheel"][0].update(function="desktops")),
                                ([], lambda cfg: cfg["thumbwheel"].update(function="desktops"))):
            with self.subTest(own=bool(entries)):
                made = thumb_engine(self, [profile(KATE)], entries, thumbwheel={"function": "tabs"})
                front(made, KATE)
                turn(made, self.clock, STEP_UP, 2)
                made.online = False
                edited(made, change)
                turn(made, self.clock, STEP_UP, 3)  # reports still queued when it went away
                self.assertEqual(made.executor.ran, [])
                turn(made, self.clock, STEP_UP, 2)
                self.assertEqual(made.executor.ran, [DESKTOP_RIGHT])

    def test_steps_of_an_app_s_own_function_tick_as_set_for_all_apps(self):
        for ticks, expected in ((True, [device.WAVE_TICK]), (False, [])):
            with self.subTest(ticks=ticks):
                made = thumb_engine(self, [profile(KATE)], [own(KATE, "volume")], thumbwheel={"ticks": ticks})
                front(made, KATE)
                turn(made, self.clock, STEP_UP)
                self.assertEqual(made.played, expected)

    def test_battery_saving_alone_sends_nothing_to_the_mouse(self):
        made = thumb_engine(self, [profile(KATE)], [own(KATE, "tabs")])
        front(made, KATE)
        turn(made, self.clock, STEP_UP, 2)
        writes = list(made.mouse.log)
        for saving in (False, True):
            edited(made, lambda cfg: cfg["haptics"].update(battery_saving=saving))
        turn(made, self.clock, STEP_UP)
        self.assertEqual((made.mouse.log, made.executor.ran), (writes, [TAB]))

    def test_on_stopping_the_thumb_wheel_scrolls_by_itself_in_the_direction_for_all_apps(self):
        made = thumb_engine(self, [profile(KATE)], [own(KATE, "zoom")], thumbwheel={"invert": True})
        front(made, KATE)
        made.mouse.wheel = None  # the scroll wheel is handed back too: not looked at here
        made._shutdown()
        self.assertEqual(thumbs(made), [("thumb", True, False), ("thumb", False, True)])

    def test_without_functions_of_their_own_only_the_buttons_are_written(self):
        made = thumb_engine(self, [profile(KATE, back=COPY), profile(KONSOLE)], thumbwheel={"function": "tabs"})
        front(made, KATE)
        front(made, KONSOLE)
        self.assertEqual(made.mouse.log, [(BACK, True, False), (BACK, False, False)])


class ThumbWheelStoreAndOthers(unittest.TestCase):
    """The thumb wheel of single apps in the Store, for other programs (Set, Get), in the settings file and in logs."""

    def setUp(self):
        self.store = Store(copy.deepcopy(config.DEFAULTS), _Engine())
        self.store.flush = lambda: None
        self.store._save_timer.timeout.disconnect()
        self.api = api.MouseApi(self.store, None, lambda: None)
        self.pushed = []
        self.store.changed.connect(lambda: self.pushed.append(copy.deepcopy(self.store.cfg["mouse"]["thumbwheel"])))

    def entries(self):
        return self.store.cfg["mouse"]["thumbwheel"]

    def test_the_store_as_the_page_uses_it(self):
        store = self.store
        self.assertFalse(store.set_profile_thumb(KATE, "zoom"))  # no profile
        store.add_mouse_profile(KATE)
        store.add_mouse_profile(KONSOLE)
        self.assertEqual(self.entries(), [])  # a new profile has none of its own
        self.pushed.clear()
        self.assertTrue(store.set_profile_thumb(KATE, None))
        self.assertEqual(self.pushed, [])
        before = copy.deepcopy(store.cfg)
        with self.assertLogs("mxoptions", logging.WARNING) as logs:
            for bad in ("spin", "", 3, ["zoom"], {"type": "zoom"}, "Zoom"):
                self.assertFalse(store.set_profile_thumb(KATE, bad))
        self.assertNotIn(KATE, "\n".join(logs.output))
        self.assertNotIn("spin", "\n".join(logs.output))
        self.assertEqual((store.cfg, self.pushed), (before, []))
        self.assertTrue(store.set_profile_thumb(KATE, "zoom"))
        self.assertTrue(store.set_profile_thumb(KATE, "zoom"))
        self.assertTrue(store.set_profile_thumb(KONSOLE, "volume"))
        self.assertTrue(store.set_profile_thumb(KATE, "tabs"))
        self.assertEqual(self.pushed, [[own(KATE, "zoom")], [own(KATE, "zoom"), own(KONSOLE, "volume")],
                                       [own(KATE, "tabs"), own(KONSOLE, "volume")]])
        self.assertTrue(store.set_profile_thumb(KATE, None))
        self.assertEqual(self.entries(), [own(KONSOLE, "volume")])
        self.assertEqual(store.cfg["thumbwheel"], config.DEFAULTS["thumbwheel"])  # for all apps: unchanged
        self.assertEqual(config.clean(copy.deepcopy(store.cfg)), store.cfg)

    def test_removing_a_profile_or_restoring_the_defaults_removes_its_function(self):
        store = self.store
        for app in (KATE, KONSOLE):
            store.add_mouse_profile(app)
            store.set_profile_thumb(app, "zoom")
        self.pushed.clear()
        store.remove_mouse_profile(KATE)
        self.assertEqual((self.entries(), self.pushed), ([own(KONSOLE, "zoom")], [[own(KONSOLE, "zoom")]]))
        store.add_mouse_profile(KATE)
        self.assertEqual(self.entries(), [own(KONSOLE, "zoom")])  # back without the one it had
        store.restore_defaults()
        self.assertEqual(store.cfg["mouse"], {"profiles": [], "thumbwheel": [], "thumbwheel_keys": [], "gestures": []})

    def test_other_programs_set_what_may_be_and_are_refused_the_rest(self):
        self.assertTrue(self.api.Set("mouse.profiles", json.dumps([profile(KATE), profile(KONSOLE)])))
        value = [own(KATE, "zoom"), own(KONSOLE, "tabs")]
        self.assertTrue(self.api.Set("mouse.thumbwheel", json.dumps(value)))
        self.assertEqual(self.entries(), value)
        self.assertTrue(self.api.Set("mouse.thumbwheel.1.function", json.dumps("volume")))
        self.assertEqual(self.entries()[1], own(KONSOLE, "volume"))
        before = copy.deepcopy(self.store.cfg)
        for path, bad in (("mouse.thumbwheel", [own(KWRITE, "zoom")]), ("mouse.thumbwheel", [own(KATE, "spin")]),
                          ("mouse.thumbwheel", [dict(own(KATE, "zoom"), speed=2)]), ("mouse.thumbwheel", value * 2),
                          ("mouse.thumbwheel", {KATE: "zoom"}), ("mouse.thumbwheel.0.function", "spin"),
                          ("mouse.thumbwheel.0.function", None), ("mouse.thumbwheel.0.app", KWRITE),
                          ("mouse.thumbwheel.2", own(KWRITE, "zoom"))):
            with self.subTest(path=path, value=bad):
                self.assertFalse(self.api.Set(path, json.dumps(bad)))
                self.assertEqual(self.store.cfg, before)
        self.assertTrue(self.api.Set("mouse.profiles", json.dumps([profile(KONSOLE)])))  # Kate's function goes with it
        self.assertEqual(self.entries(), [own(KONSOLE, "volume")])
        self.assertEqual(json.loads(self.api.Get("mouse.thumbwheel")), [own(KONSOLE, "volume")])

    def test_the_settings_file_holds_what_was_saved_never_what_counts_in_front(self):
        self.store.cfg["mouse"]["profiles"] = [profile(KATE)]
        self.store.cfg["mouse"]["thumbwheel"] = [own(KATE, "zoom")]
        made = thumb_engine(self, [profile(KATE)], [own(KATE, "zoom")])
        front(made, KATE)
        self.store.cfg["flow"]["id"] = "this-one"
        exported = config.export_data(self.store.cfg)
        self.assertEqual(json.loads(exported)["config"]["thumbwheel"], config.DEFAULTS["thumbwheel"])
        current = copy.deepcopy(self.store.cfg)
        current["flow"]["id"] = "here"
        back = config.import_data(exported, current)
        self.assertEqual((back["mouse"], back["flow"]["id"]), (self.store.cfg["mouse"], "here"))
        older = json.loads(exported)
        del older["config"]["mouse"]["thumbwheel"]  # exported by a version without it
        self.assertEqual(config.import_data(json.dumps(older).encode(), current)["mouse"],
                         {"profiles": [profile(KATE)], "thumbwheel": [], "thumbwheel_keys": [], "gestures": []})

    def test_the_app_in_front_and_its_function_are_never_logged_published_or_told(self):
        secret = "org.example.secret"
        self.store.cfg["mouse"]["profiles"] = [profile(secret)]
        self.store.cfg["mouse"]["thumbwheel"] = [own(secret, "brightness")]
        made = thumb_engine(self, [profile(secret)], [own(secret, "brightness")])
        made._publish = lambda: engine.Engine._publish(made)
        with support.Clock() as clock, self.assertLogs("mxoptions", logging.DEBUG) as logs:
            logging.getLogger("mxoptions").debug("start")
            front(made, secret)
            turn(made, clock, STEP_UP)
            front(made, KONSOLE)
            made._publish()
            self.assertFalse(self.store.set_profile_thumb(secret, "spin"))
            self.assertFalse(self.api.Set("mouse.thumbwheel.0.function", json.dumps("spin")))
        text = "\n".join(logs.output)
        for word in ("secret", "brightness", "spin"):
            self.assertNotIn(word, text)
        self.assertNotIn("secret", json.dumps(made.listener.states, default=str))
        self.assertEqual(json.loads(self.api.Get("thumbwheel.function")), "hscroll")
        self.assertEqual(json.loads(self.api.GetConfig()), self.store.cfg)  # the saved entry only


class ThumbWheelPage(unittest.TestCase):
    """The Thumb wheel card of the Point & scroll page: the function of all apps or of one app, the rest for all."""

    CAPS = {"dpi": (200, 8000, 50), "torque": True, "smartshift": True, "wheel": True, "ratchet_events": True,
            "waveforms": [4], "thumbwheel": True}

    def make_store(self, supported=True):
        store = Store(copy.deepcopy(config.DEFAULTS), _Engine())
        store.flush = lambda: None
        store._save_timer.timeout.disconnect()
        store.front = FakeFront(supported)
        return store

    def page(self, store, kind=pages.PointScrollPage):
        page = kind(store)
        self.addCleanup(page.deleteLater)
        page.resize(1100, 700)
        page.show()
        return page

    def functions(self, page):
        combo = page.thumb_function
        return [combo.itemData(i) for i in range(combo.count())]

    def pick(self, page, function):
        combo = page.thumb_function
        combo.activated.emit(combo.findData(function))  # as the user picks it

    def test_for_all_apps_it_is_as_it_was(self):
        store = self.make_store()
        page = self.page(store)
        self.assertEqual(self.functions(page), list(config.THUMB_FUNCTIONS))
        self.pick(page, "tabs")
        self.assertEqual((store.cfg["thumbwheel"]["function"], store.cfg["mouse"]["thumbwheel"]), ("tabs", []))
        self.assertEqual(page.thumb_function.currentData(), "tabs")
        self.assertTrue(page.thumb_ticks_row.isEnabled())

    def test_the_function_of_one_app(self):
        store = self.make_store()
        store.add_mouse_profile(KATE)
        page = self.page(store)
        page.apps.apps.setCurrentIndex(1)
        self.assertEqual(store.mouse_app, KATE)
        self.assertEqual(self.functions(page), [None] + list(config.THUMB_FUNCTIONS))
        self.assertEqual((page.thumb_function.currentIndex(), page.thumb_function.itemText(0)), (0, "As in all apps"))
        self.assertIn("Direction, speed and steps", page.apps.hint.text())
        self.assertFalse(page.thumb_ticks_row.isEnabled())  # sideways scrolling, as for all apps: no steps
        self.pick(page, "zoom")
        self.assertEqual(store.cfg["mouse"]["thumbwheel"], [own(KATE, "zoom")])
        self.assertEqual(store.cfg["thumbwheel"]["function"], "hscroll")  # every other app: unchanged
        self.assertEqual(page.thumb_function.currentData(), "zoom")
        self.assertTrue(page.thumb_ticks_row.isEnabled())
        page.thumb_invert.setChecked(True)
        page.thumb_speed.changed.emit(1.5)
        page.thumb_ticks.setChecked(False)
        self.assertEqual(store.cfg["thumbwheel"], {"function": "hscroll", "invert": True, "speed": 1.5, "ticks": False, "up": [], "down": []})
        page.thumb_function.activated.emit(0)
        self.assertEqual((store.cfg["mouse"]["thumbwheel"], store.cfg["thumbwheel"]["function"]), ([], "hscroll"))
        self.assertEqual(page.thumb_function.currentIndex(), 0)
        page.apps.apps.setCurrentIndex(0)
        self.assertEqual(self.functions(page), list(config.THUMB_FUNCTIONS))
        self.assertEqual(page.thumb_function.currentData(), "hscroll")

    def test_the_pages_show_the_same_app_and_go_back_when_it_goes(self):
        store = self.make_store()
        store.add_mouse_profile(KATE)
        scroll = self.page(store)
        buttons, rings = self.page(store, pages.ButtonsPage), self.page(store, ring_page.RingPage)
        buttons.apps.apps.setCurrentIndex(1)
        self.assertEqual((scroll.apps.apps.currentIndex(), scroll.thumb_function.itemData(0)), (1, None))
        self.pick(scroll, "volume")
        with mock.patch.object(pages.QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes):
            QTest.mouseClick(scroll.apps.remove_app, Qt.MouseButton.LeftButton)
        self.assertEqual((scroll.apps.apps.currentIndex(), buttons.apps.apps.currentIndex(),
                          rings.apps.apps.currentIndex(), store.mouse_app), (0, 0, 0, None))
        self.assertEqual((store.cfg["mouse"], self.functions(scroll)), ({"profiles": [], "thumbwheel": [], "thumbwheel_keys": [], "gestures": []},
                                                                        list(config.THUMB_FUNCTIONS)))

    def test_where_the_desktop_does_not_say_the_function_is_for_all_apps(self):
        from mxoptions import i18n
        self.addCleanup(i18n.setup, "en")
        for language, words in (("en", "does not tell"), ("uk", "не повідомляє")):
            with self.subTest(language=language):
                i18n.setup(language)
                store = self.make_store(supported=False)
                store.front.last_other = KATE
                store.cfg["mouse"]["profiles"] = [profile(KATE)]
                page = self.page(store)
                self.assertFalse(page.apps.apps.isEnabled() or page.apps.add_app.isEnabled())
                self.assertIn(words, page.apps.hint.text())
                self.pick(page, "volume")
                self.assertEqual((store.cfg["thumbwheel"]["function"], store.cfg["mouse"]["thumbwheel"]), ("volume", []))

    def test_names_for_assistive_technology_and_the_tab_order(self):
        from mxoptions import i18n
        self.addCleanup(i18n.setup, "en")
        for language, combo, remove, hint in (
                ("en", "Settings for", "Remove the settings of this app", "Direction, speed and steps"),
                ("uk", "Налаштування для", "Вилучити налаштування цієї програми", "Напрямок, швидкість і кроки")):
            with self.subTest(language=language):
                i18n.setup(language)
                store = self.make_store()
                store.add_mouse_profile(KATE)
                store.front.last_other = KWRITE
                page = self.page(store)
                store.set_mouse_app(KATE)
                chooser = page.apps
                self.assertEqual((chooser.apps.accessibleName(), chooser.remove_app.accessibleName()), (combo, remove))
                self.assertIs(chooser.findChildren(pages.QLabel)[0].buddy(), chooser.apps)
                self.assertIn(hint, chooser.hint.text())
                self.assertEqual(page.thumb_function.itemText(0), i18n._("As in all apps"))
                order, widget = [], chooser.apps
                while len(order) < 4:
                    widget = widget.nextInFocusChain()
                    if widget.isVisible() and widget.isEnabled() and widget.focusPolicy() & Qt.FocusPolicy.TabFocus:
                        order.append(widget)
                self.assertEqual(order[:3], [chooser.add_app, chooser.remove_app, page.thumb_function])
                self.assertTrue(page.thumb_invert.isAncestorOf(order[3]) or order[3] is page.thumb_invert)

    def test_without_a_thumb_wheel_the_card_and_its_chooser_are_hidden(self):
        store = self.make_store()
        page = self.page(store)
        self.assertTrue(page.apps.isVisible())
        store.update_state(dict(store.state, uinput=True, caps=dict(self.CAPS, thumbwheel=False)))
        self.assertFalse(page.thumb_card.isVisible() or page.apps.isVisible())
        self.assertEqual(store.front.wanted[-1], (page.apps, False))
        store.update_state(dict(store.state, uinput=True, caps=self.CAPS))
        self.assertTrue(page.apps.isVisible())


from tests.test_keyboard import Gate  # noqa: E402


def stoppable(*steps, repeat=False):
    return dict(macro(*steps, repeat=repeat), stop_on_press=True)


class PressAgainButtons(unittest.TestCase):
    """A button pressed again stops the macro with "stop_on_press" it started; gestures, ring bubbles and Trigger
    queue it as before, and so does a value that is not true. Real Executor, played by a Gate."""

    copy_settings, press = MacrosPerApp.copy_settings, MacrosPerApp.press

    def made(self, profiles=(), **buttons):
        made = make(self, profiles, buttons=buttons)
        return made, Gate(self, made.executor)

    def test_a_button_pressed_again_stops_its_macro(self):
        made, gate = self.made(back=stoppable("KEY_A", "KEY_B"))
        self.press(made, BACK)
        gate.playing()
        self.press(made, BACK)
        self.assertEqual(gate.drained(), ["KEY_A"])
        self.assertEqual(held(made), set())

    def test_equal_copies_and_an_app_with_an_equal_macro_keep_it_stoppable(self):
        made, gate = self.made([profile(KATE, back=stoppable("KEY_A", "KEY_B"))], back=stoppable("KEY_A", "KEY_B"))
        self.press(made, BACK)
        gate.playing()
        self.copy_settings(made)
        self.copy_settings(made)
        front(made, KATE)
        self.press(made, BACK)
        self.assertEqual(gate.drained(), ["KEY_A"])

    def test_gestures_ring_bubbles_and_trigger_queue_it_as_before(self):
        def gesture(made):
            self.press(made, GESTURE)

        def bubble(made):
            made.ring_action(stoppable("KEY_A", "KEY_B"), None)
            drain(made)

        def trigger(made):
            made.trigger_action(stoppable("KEY_A", "KEY_B"))
            drain(made)
        for run in (gesture, bubble, trigger):
            with self.subTest(run=run.__name__):
                gestures = dict(config.default_gestures(), tap=stoppable("KEY_A", "KEY_B"))
                made, gate = self.made(gesture=gestures)
                run(made)
                gate.playing()
                run(made)
                self.assertEqual(gate.drained(), ["KEY_A", "KEY_B"] * 2)

    def test_a_value_that_is_not_true_queues(self):
        for value in ("yes", 1):
            with self.subTest(value=value):
                made, gate = self.made(back=dict(macro("KEY_A", "KEY_B"), stop_on_press=value))
                self.press(made, BACK)
                gate.playing()
                self.press(made, BACK)
                self.assertEqual(gate.drained(), ["KEY_A", "KEY_B"] * 2)

    def test_a_reset_forgets_it(self):
        made, gate = self.made(back=stoppable("KEY_A", "KEY_B"))
        self.press(made, BACK)
        gate.playing()
        made._reset_input_state()
        self.press(made, BACK)
        self.assertEqual(gate.drained(), ["KEY_A", "KEY_A", "KEY_B"])

    def test_the_mouse_and_the_keyboard_keep_their_macros_apart(self):
        made, mouse = self.made()
        keys = actions.Executor(made.vinput, lambda *_args: None, owner="keyboard")
        board = Gate(self, keys)
        shared = stoppable("KEY_A", "KEY_B")
        made.executor.press(shared)
        made.executor.release(shared)
        mouse.playing()
        keys.press(shared)  # not playing on the keyboard: it starts there
        keys.release(shared)
        board.playing()
        keys.press(shared)  # stops the keyboard's only
        keys.release(shared)
        self.assertEqual(board.drained(), ["KEY_A"])
        self.assertEqual(mouse.drained(), ["KEY_A", "KEY_B"])


if __name__ == "__main__":
    unittest.main()


class _Told(_Engine):
    """Stands in for the engine behind a Store and keeps every copy of the settings it is given."""

    def __init__(self):
        self.configs = []

    def set_config(self, cfg):
        self.configs.append(copy.deepcopy(cfg))


class SuggestedSettings(unittest.TestCase):
    """Suggested settings for one app (config.APP_PRESETS): what they hold, what giving them to an app changes in
    the Store and then in the engine, and the Buttons page that offers them."""

    UNDO, REDO = actions.keys("LEFTCTRL", "Z"), actions.keys("LEFTCTRL", "Y")

    def make_store(self, supported=True):
        store = Store(copy.deepcopy(config.DEFAULTS), _Told())
        store.flush = lambda: None
        store._save_timer.timeout.disconnect()
        store.front = FakeFront(supported)
        return store

    def page(self, store):
        page = pages.ButtonsPage(store)
        self.addCleanup(page.deleteLater)
        page.resize(1100, 700)
        page.show()
        return page

    def test_what_each_set_holds(self):
        from mxoptions import i18n_uk
        self.assertEqual(config.APP_PRESETS, {
            "browser": ("Web browser", {"middle": NATIVE, "back": NATIVE, "forward": NATIVE}, "tabs"),
            "spreadsheet": ("Spreadsheet", {"back": self.UNDO, "forward": self.REDO}, "hscroll"),
            "documents": ("Documents and slides", {"back": self.UNDO, "forward": self.REDO}, "zoom"),
        })
        for preset, (label, buttons, function) in config.APP_PRESETS.items():
            with self.subTest(preset=preset):
                self.assertIn(label, i18n_uk.STRINGS)
                self.assertLessEqual(set(buttons), {"middle", "back", "forward"})  # gesture, wheel mode, panel: never
                self.assertTrue(all(map(config.profile_button_action, buttons.values())))
                self.assertIn(function, config.THUMB_FUNCTIONS)
                self.assertNotEqual(function, "keys")
        self.assertIs(actions.find_preset(self.UNDO), actions.PRESET_BY_ID["undo"])  # shown as the catalogue's Undo

    def test_only_what_an_app_does_as_in_all_apps_changes(self):
        store = self.make_store()
        mine = macro("KEY_F5", name="mine")
        store.cfg["buttons"]["middle"] = PASTE
        store.cfg["mouse"]["profiles"] = [profile(KATE, RING_A, back=mine), profile(KONSOLE, forward=COPY)]
        store.cfg["mouse"]["thumbwheel"] = [own(KONSOLE, "volume")]
        store.cfg["mouse"]["thumbwheel_keys"] = [{"app": KATE, "up": ["KEY_F1"], "down": []}]
        before = copy.deepcopy(store.cfg)
        self.assertEqual(config.preset_changes(store.cfg, KATE, "spreadsheet"), ({"forward": self.REDO}, "hscroll"))
        self.assertTrue(store.apply_mouse_preset(KATE, "spreadsheet"))
        expected = copy.deepcopy(before)
        expected["mouse"]["profiles"][0]["buttons"]["forward"] = self.REDO
        expected["mouse"]["thumbwheel"].append(own(KATE, "hscroll"))
        self.assertEqual(store.cfg, expected)  # its macro, ring and keystrokes, other apps, all apps: as they were
        self.assertEqual(store.engine.configs, [expected])  # in one go
        self.assertEqual(config.thumbwheel(store.cfg, KATE)["up"], ["KEY_F1"])
        self.assertTrue(store.apply_mouse_preset(KONSOLE, "browser"))
        self.assertEqual(store.mouse_profile(KONSOLE)["buttons"], {"forward": COPY, "middle": NATIVE, "back": NATIVE})
        self.assertEqual(config.app_thumb_function(store.cfg, KONSOLE), "volume")
        self.assertEqual(store.engine.configs[1:], [store.cfg])  # buttons alone: told too
        told = len(store.engine.configs)
        self.assertEqual(config.preset_changes(store.cfg, KATE, "documents"), ({}, None))  # all its own by now
        self.assertTrue(store.apply_mouse_preset(KATE, "documents"))
        self.assertFalse(store.apply_mouse_preset(KWRITE, "browser"))  # no profile
        self.assertEqual(config.preset_changes(store.cfg, KWRITE, "browser"), ({}, None))
        self.assertEqual(len(store.engine.configs), told)
        self.assertNotIn(KWRITE, json.dumps(store.cfg))
        self.assertEqual(store.mouse_profile(KATE)["buttons"], {"back": mine, "forward": self.REDO})
        self.assertEqual(config.clean(copy.deepcopy(store.cfg)), store.cfg)  # kept as they are when loaded

    def test_each_app_gets_copies_of_its_own(self):
        store = self.make_store()
        store.cfg["mouse"]["profiles"] = [profile(KATE), profile(KONSOLE)]
        for app in (KATE, KONSOLE):
            store.apply_mouse_preset(app, "spreadsheet")
        store.mouse_profile(KATE)["buttons"]["forward"]["keys"].append("KEY_F1")
        store.mouse_profile(KATE)["buttons"]["back"]["keys"].append("KEY_F2")
        self.assertEqual(store.mouse_profile(KONSOLE)["buttons"], {"back": self.UNDO, "forward": self.REDO})
        self.assertEqual(config.APP_PRESETS["spreadsheet"][1], {"back": self.UNDO, "forward": self.REDO})

    def test_kept_exported_imported_and_restored_as_any_profile(self):
        store = self.make_store()
        store.cfg["mouse"]["profiles"] = [profile(KATE)]
        store.apply_mouse_preset(KATE, "documents")
        current = copy.deepcopy(store.cfg)
        self.assertEqual(config.import_data(config.export_data(store.cfg), current)["mouse"], store.cfg["mouse"])
        store.remove_mouse_profile(KATE)
        self.assertEqual(store.cfg["mouse"], config.DEFAULTS["mouse"])  # its function goes with it
        store.add_mouse_profile(KATE)
        self.assertEqual(config.preset_changes(store.cfg, KATE, "documents"),
                         ({"back": self.UNDO, "forward": self.REDO}, "zoom"))  # added again: nothing of before
        store.apply_mouse_preset(KATE, "documents")
        store.restore_defaults()
        self.assertEqual(store.cfg["mouse"], config.DEFAULTS["mouse"])

    def test_the_engine_does_what_the_set_says_in_that_app_only(self):
        store = self.make_store()
        store.cfg["mouse"]["profiles"] = [profile(KATE, RING_A)]
        made = make(self, cfg=copy.deepcopy(store.cfg), real=False)
        front(made, KATE)
        made._button_down(PANEL)  # its ring opens and stays open
        rings = (made._ring_session, made._ring_floor, list(made.listener.rings))
        store.apply_mouse_preset(KATE, "spreadsheet")
        set_config(made, store.engine.configs[-1])
        self.assertEqual((made._ring_session, made._ring_floor, made.listener.rings), rings)  # same ring
        made.ring_action(PICK_A, made._ring_session)
        drain(made)
        made._button_up(PANEL)
        made._button_down(BACK)
        made._button_up(BACK)
        self.assertEqual(made.executor.ran, [PICK_A, self.UNDO])
        front(made, KONSOLE)
        made._button_down(BACK)  # all apps: the mouse's own back button, nothing run
        made._button_up(BACK)
        self.assertEqual(made.executor.ran, [PICK_A, self.UNDO])
        self.assertEqual(config.thumbwheel(made.cfg, KATE)["function"], "hscroll")

    def test_the_buttons_page_offers_them_for_the_app_chosen(self):
        store = self.make_store()
        store.cfg["buttons"]["back"] = COPY
        store.cfg["mouse"]["profiles"] = [profile(KATE, forward=PASTE)]
        page = self.page(store)
        chooser = page.apps
        self.assertTrue(chooser.suggest.isHidden())  # all apps: as before
        store.set_mouse_app(KATE)
        self.assertTrue(chooser.suggest.isVisible())
        menu = chooser.suggest.menu()
        self.assertEqual([action.text() for action in menu.actions()], ["Web browser", "Spreadsheet", "Documents and slides"])
        asked = []

        def answer(reply, change=lambda: None):
            def question(_parent, _title, text):
                asked.append(text)
                change()
                return reply
            return question

        before = copy.deepcopy(store.cfg)
        with mock.patch.object(pages.QMessageBox, "question", answer(QMessageBox.StandardButton.No)):
            menu.actions()[1].trigger()
        self.assertEqual(store.cfg, before)
        self.assertEqual(asked[-1].split("\n"), [
            'Apply the suggested settings "Spreadsheet" to kate?', "",
            "Back button: Undo", "Forward button: stays as set for this app (Paste)", "Thumb wheel: Horizontal scroll", "",
            "Logi Options+ also pans while the middle button is held here. These settings leave the middle button as "
            "it is: to pan, give it Gestures for this app and choose Pan.", "",
            'Everything else stays as it is. To undo one of these, choose "As in all apps" for it.'])
        with mock.patch.object(pages.QMessageBox, "question",
                               answer(QMessageBox.StandardButton.Yes, lambda: chooser.apps.setCurrentIndex(0))):
            menu.actions()[1].trigger()  # all apps chosen while it asks: still the app it asked about
        self.assertIsNone(store.mouse_app)
        self.assertEqual(store.mouse_profile(KATE)["buttons"], {"back": self.UNDO, "forward": PASTE})
        self.assertEqual(store.cfg["buttons"]["back"], COPY)
        self.assertTrue(chooser.suggest.isHidden())
        store.set_mouse_app(KATE)
        with mock.patch.object(pages.QMessageBox, "question",
                               answer(QMessageBox.StandardButton.Yes, lambda: store.remove_mouse_profile(KATE))), \
                mock.patch.object(pages.QMessageBox, "information") as information:
            menu.actions()[0].trigger()  # profile removed: no apply or notice
        information.assert_not_called()
        self.assertEqual(store.cfg["mouse"], config.DEFAULTS["mouse"])
        self.assertIn("Middle button: Default", asked[-1].split("\n"))
        self.assertNotIn("pans", asked[-1])

    def test_names_for_assistive_technology_the_tab_order_and_the_other_pages(self):
        from mxoptions import i18n
        self.addCleanup(i18n.setup, "en")
        for language, text, item in (("en", "Suggested settings", "Web browser"),
                                     ("uk", "Запропоновані налаштування", "Веббраузер")):
            with self.subTest(language=language):
                i18n.setup(language)
                store = self.make_store()
                store.cfg["mouse"]["profiles"] = [profile(KATE)]
                store.mouse_app = KATE
                chooser = self.page(store).apps
                self.assertEqual((chooser.suggest.text(), chooser.suggest.menu().actions()[0].text()), (text, item))
                self.assertIs(chooser.remove_app.nextInFocusChain(), chooser.suggest)
        store = self.make_store()
        for page in (ring_page.RingPage(store), pages.PointScrollPage(store)):
            self.addCleanup(page.deleteLater)
            self.assertIsNone(page.apps.suggest)  # their settings are not in the sets

    def test_changed_preview_never_applies_unshown_settings(self):
        message = "Settings changed while this dialog was open. Choose Suggested settings again."
        for case in ("button_removed", "function_removed", "profile_recreated", "button_customized", "unrelated_setting"):
            with self.subTest(case=case):
                store = self.make_store()
                self.assertTrue(store.add_mouse_profile(KATE))
                if case == "button_removed":
                    self.assertTrue(store.set_profile_button(KATE, "forward", PASTE))
                if case == "function_removed":
                    self.assertTrue(store.set_profile_thumb(KATE, "volume"))
                store.set_mouse_app(KATE)
                chooser = self.page(store).apps
                during = {}

                def answer(_parent, _title, text):
                    during["preview"] = text
                    during["before_change_pushes"] = len(store.engine.configs)
                    if case == "button_removed":
                        self.assertTrue(store.set_profile_button(KATE, "forward", None))
                    elif case == "function_removed":
                        self.assertTrue(store.set_profile_thumb(KATE, None))
                    elif case == "profile_recreated":
                        store.remove_mouse_profile(KATE)
                        self.assertTrue(store.add_mouse_profile(KATE))
                    elif case == "button_customized":
                        self.assertTrue(store.set_profile_button(KATE, "back", macro("KEY_F5", name="kept")))
                    else:
                        store.set("haptics", "battery_saving", False)
                    during["settings"] = copy.deepcopy(store.cfg)
                    during["pushes"] = len(store.engine.configs)
                    during["selection"] = store.mouse_app
                    return QMessageBox.StandardButton.Yes

                with mock.patch.object(pages.QMessageBox, "question", answer), \
                        mock.patch.object(pages.QMessageBox, "information") as information:
                    chooser.suggest.menu().actions()[1].trigger()
                if case == "button_removed":
                    self.assertIn("Forward button: stays as set for this app (Paste)", during["preview"])
                elif case == "function_removed":
                    self.assertIn("Thumb wheel: stays as set for this app", during["preview"])
                self.assertEqual(store.mouse_app, during["selection"])
                if case == "unrelated_setting":
                    self.assertEqual(during["pushes"], during["before_change_pushes"] + 1)
                    self.assertEqual(len(store.engine.configs), during["pushes"] + 1)
                    self.assertEqual(store.mouse_profile(KATE)["buttons"], {"back": self.UNDO, "forward": self.REDO})
                    self.assertEqual(config.app_thumb_function(store.cfg, KATE), "hscroll")
                    self.assertFalse(store.cfg["haptics"]["battery_saving"])
                    information.assert_not_called()
                else:
                    self.assertEqual(store.cfg, during["settings"])
                    self.assertEqual(len(store.engine.configs), during["pushes"])
                    information.assert_called_once_with(chooser, pages.APP_NAME, message)
                store._save_timer.stop()

    def test_app_controls_fit_inside_settings_window(self):
        from PySide6.QtCore import QEvent, QPoint, QRect
        from mxoptions import i18n
        from mxoptions.ui import theme
        from mxoptions.ui.main_window import MainWindow

        old_style, old_sheet, old_palette = APP.style().objectName(), APP.styleSheet(), APP.palette()
        self.addCleanup(i18n.setup, "en")
        try:
            APP.setStyle("Fusion")
            theme.apply(APP)
            for language in ("en", "uk"):
                i18n.setup(language)
                store = self.make_store()
                self.assertTrue(store.add_mouse_profile(KATE))
                self.assertTrue(store.set_profile_button(KATE, "gesture", config.default_gestures()))
                with (mock.patch.object(pages.FirmwareQuery, "start"),
                      mock.patch.object(pages.MouseView, "_prerender"),
                      mock.patch.object(threading.Thread, "start") as worker_start):
                    window = MainWindow(store, hide_on_close=False)
                    try:
                        window.show()
                        for width in (1180, 1200, 1280):
                            with self.subTest(language=language, width=width):
                                window.resize(width, 780)
                                store.set_mouse_app(None)
                                for _ in range(8):
                                    APP.processEvents()
                                window.buttons_page.view._settle.timeout.emit()
                                expected_width = window.width()
                                store.set_mouse_app(KATE)
                                for _ in range(8):
                                    APP.processEvents()
                                self.assertEqual(window.width(), expected_width)
                                chooser = window.buttons_page.apps
                                controls = (chooser.apps, chooser.add_app, chooser.remove_app, chooser.suggest)
                                for control in controls:
                                    self.assertTrue(chooser.rect().contains(control.geometry()), control.objectName())
                                for button in controls[1:]:
                                    self.assertGreaterEqual(button.width(), button.sizeHint().width(), button.text())
                                for index, control in enumerate(controls):
                                    for other in controls[index + 1:]:
                                        self.assertFalse(control.geometry().intersects(other.geometry()))
                                self.assertIs(chooser.remove_app.nextInFocusChain(), chooser.suggest)
                                picker = window.buttons_page.picker
                                for app in (None, KATE):  # the mode of the gesture button, for all apps and its own
                                    store.set_mouse_app(app)
                                    window.buttons_page.view.select("gesture")
                                    for _ in range(8):
                                        APP.processEvents()
                                    mode = picker.mode
                                    self.assertTrue(mode.isVisible(), app)
                                    self.assertTrue(picker.rect().contains(QRect(mode.mapTo(picker, QPoint()), mode.size())), app)
                                    self.assertGreaterEqual(mode.width(), mode.sizeHint().width(), app)
                                    self.assertEqual(window.width(), expected_width)
                    finally:
                        store._save_timer.stop()
                        window.hide()
                        window.deleteLater()
                        APP.sendPostedEvents(window, QEvent.Type.DeferredDelete)
                    worker_start.assert_not_called()
        finally:
            APP.setStyle(old_style)
            APP.setStyleSheet(old_sheet)
            APP.setPalette(old_palette)
