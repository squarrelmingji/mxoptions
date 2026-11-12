"""The mouse and the keyboard holding keys through the one virtual keyboard (vinput.Holds), on its own
and inside both backends with what they send to the system recorded instead of sent."""

import copy
import ctypes
import importlib
import sys
import threading
import time
import types
import unittest
from unittest import mock

from tests.support import FakeInput
from mxoptions import actions, vinput

CTRL, C = "KEY_LEFTCTRL", "KEY_C"


class Holds(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.holds = vinput.Holds(lambda name, down: self.sent.append((name, down)))

    def test_a_key_held_by_two_goes_up_with_the_last(self):
        self.holds.press([CTRL], "mouse")
        self.holds.press([CTRL], "keyboard")
        self.holds.release([CTRL], "mouse")
        self.assertEqual(self.sent, [(CTRL, True)])
        self.holds.release([CTRL], "keyboard")
        self.assertEqual(self.sent, [(CTRL, True), (CTRL, False)])

    def test_letting_go_of_what_one_never_held_changes_nothing(self):
        self.holds.press([CTRL], "keyboard")
        for _ in range(3):
            self.holds.release([CTRL], "mouse")
        self.assertEqual(self.sent, [(CTRL, True)])
        self.assertEqual(self.holds.held(), {CTRL})

    def test_one_owner_holding_twice_lets_go_twice(self):
        self.holds.press([CTRL], "mouse")
        self.holds.press([CTRL], "mouse")
        self.holds.release([CTRL], "mouse")
        self.assertEqual(self.sent, [(CTRL, True)])
        self.holds.release([CTRL], "mouse")
        self.assertEqual(self.sent[-1], (CTRL, False))

    def test_a_tap_leaves_held_keys_alone(self):
        self.holds.press([CTRL], "keyboard")
        self.holds.tap([CTRL, C], wait=0)
        self.assertEqual(self.sent, [(CTRL, True), (C, True), (C, False)])

    def test_resetting_one_owner_keeps_the_other_s_keys(self):
        self.holds.press([CTRL], "keyboard")
        self.holds.press([CTRL, "KEY_LEFTSHIFT"], "mouse")
        self.holds.release_all("mouse")
        self.assertEqual(self.sent, [(CTRL, True), ("KEY_LEFTSHIFT", True), ("KEY_LEFTSHIFT", False)])
        self.holds.release_all()
        self.assertEqual(self.sent[-1], (CTRL, False))
        self.assertEqual(self.holds.held(), set())


class Executors(unittest.TestCase):
    def setUp(self):
        self.input = FakeInput()
        self.mouse = actions.Executor(self.input, lambda action, phase: None)
        self.keyboard = actions.Executor(self.input, lambda action, phase: None, owner="keyboard")
        self.addCleanup(self.mouse.close)
        self.addCleanup(self.keyboard.close)

    def test_the_mouse_starting_over_keeps_the_keyboard_s_ctrl_down(self):
        self.keyboard.press(actions.keys("LEFTCTRL"))
        self.mouse.press(actions.keys("LEFTCTRL", "C"))
        self.mouse.reset()
        self.assertEqual(self.input.sent, [(CTRL, True), (C, True), (C, False)])
        self.keyboard.release(actions.keys("LEFTCTRL"))
        self.assertEqual(self.input.sent[-1], (CTRL, False))

    def test_a_reset_stops_a_repeating_macro(self):
        macro = {"type": "macro", "name": "synthetic", "repeat": True, "steps": [{"do": "keys", "keys": ["KEY_A"]}]}
        started = threading.Event()
        real_tap = self.input.tap
        self.input.tap = lambda names: (started.set(), real_tap(names))
        self.keyboard.press(macro)
        self.assertTrue(started.wait(2.0))
        self.keyboard.reset()
        self.mouse.press(actions.keys("LEFTSHIFT"))
        before = len(self.input.sent)
        threading.Event().wait(0.2)
        taps = [entry for entry in self.input.sent[before:] if entry[0] == "KEY_A"]
        self.assertLessEqual(len(taps), 2)  # the round under way may finish; no more come
        threading.Event().wait(0.2)
        self.assertEqual([entry for entry in self.input.sent[before + len(taps):] if entry[0] == "KEY_A"], [])
        self.assertIn("KEY_LEFTSHIFT", self.input._holds.held())


    def test_a_reset_ends_the_macro_under_way_and_those_queued(self):
        tapped = []
        self.input.tap = lambda names: tapped.append(tuple(names))
        one_shot = {"type": "macro", "steps": [{"do": "delay", "ms": 300}, {"do": "keys", "keys": ["KEY_A"]}]}
        repeating = {"type": "macro", "repeat": True, "steps": [{"do": "keys", "keys": ["KEY_B"]}, {"do": "delay", "ms": 300},
                                                               {"do": "keys", "keys": ["KEY_C"]}]}
        for first in (one_shot, repeating):
            with self.subTest(repeat=first is repeating):
                tapped.clear()
                self.mouse.press({"type": "macro", "repeat": True, "steps": [{"do": "keys", "keys": ["KEY_M"]}]})
                self.keyboard.press(first)
                self.keyboard.trigger({"type": "macro", "steps": [{"do": "keys", "keys": ["KEY_Q"]}]})  # queued behind it
                threading.Event().wait(0.1)
                start = time.monotonic()
                self.keyboard.reset()
                threading.Event().wait(0.5)
                mine = [names for names in tapped if names != ("KEY_M",)]
                self.assertEqual(mine, [("KEY_B",)] if first is repeating else [])  # nothing after the reset
                self.assertIn(("KEY_M",), tapped[-3:])  # the mouse's macro goes on
                self.mouse.reset()
                self.assertLess(time.monotonic() - start, 0.6)
                done = threading.Event()
                self.input.tap = lambda names: (tapped.append(tuple(names)), done.set())
                self.keyboard.trigger({"type": "macro", "steps": [{"do": "keys", "keys": ["KEY_Z"]}]})  # and later ones run
                self.assertTrue(done.wait(1.0))
                self.assertEqual(tapped[-1], ("KEY_Z",))
                self.input.tap = lambda names: tapped.append(tuple(names))


class Steps(unittest.TestCase):
    """The action and vibration steps of a macro, played by each executor on its macro thread."""

    def setUp(self):
        self.input = FakeInput()
        self.devices, self.hooks, self.played = [], [], []
        made = lambda owner: actions.Executor(  # noqa: E731
            self.input, lambda action, phase: self.devices.append(action), lambda name, phase: self.hooks.append((name, phase)),
            owner=owner, haptic=self.played.append)
        self.mouse, self.keyboard = made("mouse"), made("keyboard")
        self.addCleanup(self.mouse.close)
        self.addCleanup(self.keyboard.close)

    def play(self, executor, *steps):
        """Run a macro of ``steps`` and wait for it to end (its last step, a text, says so)."""
        done = threading.Event()
        with mock.patch.object(actions, "insert_text", lambda _vinput, _text: done.set()):
            executor.trigger({"type": "macro", "steps": [*steps, {"do": "text", "text": "end"}]})
            self.assertTrue(done.wait(2.0))

    def test_a_mouse_macro_runs_only_steps_that_would_be_kept(self):
        on_thread = []
        real_tap = self.input.tap
        self.input.tap = lambda names: (on_thread.append(threading.current_thread().name), real_tap(names))
        self.play(self.mouse,
                  {"do": "action", "action": {"type": "host", "host": 1}}, {"do": "action", "action": {"type": "flow"}},
                  {"do": "action", "action": {"type": "wheel_mode"}}, {"do": "action", "action": {"type": "dpi_cycle"}},
                  {"do": "action", "action": {"type": "macro", "steps": [{"do": "keys", "keys": ["KEY_Z"]}]}},
                  {"do": "action", "action": "keys"}, {"do": "action"}, {"do": "haptic", "waveform": True},
                  {"do": "haptic", "waveform": 0x10}, {"do": "haptic"}, {"do": "warp"},
                  {"do": "action", "action": {"type": "keys", "keys": ["KEY_A"]}},
                  {"do": "action", "action": {"type": "hook", "name": "paint.brush"}}, {"do": "haptic", "waveform": 4})
        self.assertEqual(self.input.sent, [("KEY_A", True), ("KEY_A", False)])
        self.assertEqual(on_thread, ["macro"])
        self.assertEqual(self.devices, [])  # nothing that talks to a device
        self.assertEqual(self.hooks, [("paint.brush", "trigger")])
        self.assertEqual(self.played, [4])

    def test_nested_keys_leave_the_keyboard_s_ctrl_down(self):
        self.keyboard.press(actions.keys("LEFTCTRL"))
        self.play(self.keyboard, {"do": "action", "action": actions.keys("LEFTCTRL", "C")})
        self.play(self.mouse, {"do": "action", "action": actions.keys("LEFTCTRL", "V")})
        self.assertEqual(self.input.sent, [(CTRL, True), (C, True), (C, False), ("KEY_V", True), ("KEY_V", False)])
        self.assertEqual(self.input._holds.held(), {CTRL})
        self.keyboard.release(actions.keys("LEFTCTRL"))
        self.assertEqual(self.input.sent[-1], (CTRL, False))

    def test_a_reset_stops_repeating_and_queued_steps_of_its_own_executor_only(self):
        tapped = []
        self.input.tap = lambda names: tapped.append(tuple(names))
        key = lambda name: {"do": "action", "action": {"type": "keys", "keys": [name]}}  # noqa: E731
        self.mouse.press({"type": "macro", "repeat": True, "steps": [key("KEY_M")]})
        self.keyboard.press({"type": "macro", "repeat": True, "steps": [key("KEY_B"), {"do": "delay", "ms": 300},
                                                                        {"do": "haptic", "waveform": 4}]})
        self.keyboard.trigger({"type": "macro", "steps": [key("KEY_Q"), {"do": "haptic", "waveform": 7}]})  # queued behind
        threading.Event().wait(0.1)
        self.keyboard.reset()
        threading.Event().wait(0.5)
        mine = [names for names in tapped if names != ("KEY_M",)]
        self.assertEqual(mine, [("KEY_B",)])
        self.assertEqual(self.played, [])  # the vibration after the wait, and the queued one, never came
        self.assertIn(("KEY_M",), tapped[-3:])  # the mouse's macro goes on
        self.mouse.reset()

    def test_a_command_started_by_a_step_is_not_waited_for(self):
        with mock.patch.object(actions.subprocess, "Popen") as popen:
            self.play(self.keyboard, {"do": "action", "action": {"type": "command", "command": "sleep 60"}},
                      {"do": "action", "action": {"type": "script", "name": "s", "script": "true"}},
                      {"do": "action", "action": actions.keys("A")})
        self.assertEqual([call.args[0] for call in popen.call_args_list], ["sleep 60", ["bash", "-c", "true"]])
        self.assertEqual(self.input.sent[-2:], [("KEY_A", True), ("KEY_A", False)])


class SourceCancel(unittest.TestCase):
    """Executor.cancel(action): only the macros started from that one action object stop (app profiles)."""

    def setUp(self):
        self.input = FakeInput()
        self.tapped = []
        self.input.tap = lambda names: self.tapped.append(names[0])
        self.keyboard = actions.Executor(self.input, lambda action, phase: None, owner="keyboard")
        self.addCleanup(self.keyboard.close)

    def wait_for(self, name, timeout=2.0):
        end = time.monotonic() + timeout
        while name not in self.tapped and time.monotonic() < end:
            time.sleep(0.01)
        self.assertIn(name, self.tapped)

    @staticmethod
    def macro(*names, wait=0, repeat=False):
        steps = [{"do": "keys", "keys": [name]} for name in names]
        if wait:
            steps.insert(1, {"do": "delay", "ms": wait})
        return {"type": "macro", "repeat": repeat, "steps": steps}

    def test_the_one_playing_stops_before_its_next_step_its_wait_cut_short(self):
        changed, other = self.macro("KEY_A", "KEY_B", wait=3000), self.macro("KEY_M", "KEY_N")
        self.keyboard.press(changed)
        self.keyboard.press(changed)  # the same key again: queued behind
        self.keyboard.press(other)    # another key: queued too
        self.wait_for("KEY_A")
        time.sleep(0.1)  # in its wait now
        start = time.monotonic()
        self.keyboard.cancel(changed)
        self.wait_for("KEY_N")
        self.assertLess(time.monotonic() - start, 1.0)  # the wait of 3 s was cut short
        time.sleep(0.2)
        self.assertEqual(self.tapped, ["KEY_A", "KEY_M", "KEY_N"])  # no B, no second A: the other key's runs

    def test_an_unchanged_one_plays_on_and_a_queued_changed_one_is_dropped(self):
        unchanged, changed = self.macro("KEY_M", "KEY_N", wait=300), self.macro("KEY_A")
        self.keyboard.press(unchanged)
        self.keyboard.trigger(changed)
        self.wait_for("KEY_M")
        self.keyboard.cancel(changed)
        self.wait_for("KEY_N")
        time.sleep(0.2)
        self.assertEqual(self.tapped, ["KEY_M", "KEY_N"])

    def test_a_repeating_one_stops_and_a_command_it_started_runs_on(self):
        repeating = {"type": "macro", "repeat": True, "steps": [{"do": "command", "command": "sleep 60"},
                                                                {"do": "keys", "keys": ["KEY_R"]}, {"do": "delay", "ms": 50}]}
        with mock.patch.object(actions.subprocess, "Popen") as popen:
            self.keyboard.press(repeating)  # held down: it repeats
            self.wait_for("KEY_R")
            self.keyboard.cancel(repeating)
            time.sleep(0.3)
            count = len(self.tapped)
            time.sleep(0.3)
            self.assertEqual(len(self.tapped), count)  # stopped
            self.assertTrue(popen.called)
            started = popen.return_value
            self.assertFalse(started.kill.called or started.terminate.called)  # the command keeps running

    def test_an_equal_action_that_is_another_object_is_not_stopped(self):
        playing = self.macro("KEY_A", "KEY_B", wait=300)
        self.keyboard.press(playing)
        self.wait_for("KEY_A")
        self.keyboard.cancel(copy.deepcopy(playing))
        self.wait_for("KEY_B")

    def test_nothing_is_kept_once_the_macros_are_over(self):
        for _round in range(20):
            action = self.macro("KEY_A")
            self.keyboard.trigger(action)
            self.keyboard.press(action)
            self.keyboard.release(action)
        end = time.monotonic() + 2.0
        while (len(self.tapped) < 40 or len(self.keyboard._sources)) and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(len(self.keyboard._sources), 0)
        self.assertEqual(self.keyboard._macro_holds, {})

    def test_a_new_macro_after_a_cancel_or_a_reset_plays(self):
        action = self.macro("KEY_A", "KEY_B", wait=2000)
        for stop in (lambda: self.keyboard.cancel(action), self.keyboard.reset):
            self.tapped.clear()
            self.keyboard.trigger(action)
            self.wait_for("KEY_A")
            stop()
            self.tapped.clear()
            self.keyboard.trigger(self.macro("KEY_Z"))
            self.keyboard.trigger(action)  # the very same object again
            self.wait_for("KEY_Z")
            self.wait_for("KEY_A")
            self.keyboard.cancel(action)  # not left in its wait for the next round


class Backends(unittest.TestCase):
    """Both backends with the system left out: what they would send is recorded."""

    def check(self, made, sent):
        made.press([CTRL], "keyboard")
        made.press([CTRL, C], "mouse")
        made.release_all("mouse")
        made.tap([CTRL, "KEY_V"])
        self.assertEqual(sent, [(CTRL, True), (C, True), (C, False), ("KEY_V", True), ("KEY_V", False)])
        made.release([CTRL], "keyboard")
        self.assertEqual(sent[-1], (CTRL, False))

    def test_linux(self):
        from mxoptions import vinput_uinput
        made = vinput_uinput.VirtualInput.__new__(vinput_uinput.VirtualInput)
        sent = []
        made._lock = threading.Lock()
        made._holds = vinput.Holds(lambda name, down: sent.append((name, down)))
        self.check(made, sent)

    def test_windows(self):
        """Only its logic: SendInput itself is not reached from here."""
        fake_windows = types.ModuleType("mxoptions.windows")
        fake_windows.BRIGHTNESS_STEP = 10
        fake_windows.brightness = mock.Mock()
        with mock.patch.object(ctypes, "WinDLL", mock.MagicMock(), create=True), \
                mock.patch.dict(sys.modules, {"mxoptions.windows": fake_windows}):
            sys.modules.pop("mxoptions.vinput_windows", None)
            backend = importlib.import_module("mxoptions.vinput_windows")
            try:
                sent = []
                backend._send = lambda *inputs: sent.extend(
                    ((made.ki.wVk or made.ki.wScan), not made.ki.dwFlags & backend.KEY_UP) for made in (i.u for i in inputs))
                made = backend.VirtualInput()
                sent.clear()
                names = []
                made._holds = vinput.Holds(lambda name, down: names.append((name, down)) or backend.VirtualInput._key(name, down))
                self.check(made, names)
                self.assertEqual(len(sent), len(names))  # each of them did go to SendInput
                made.tap(["KEY_BRIGHTNESSUP"])  # no key for it: the screens are told directly, once
                fake_windows.brightness.change.assert_called_once_with(10)
            finally:
                sys.modules.pop("mxoptions.vinput_windows", None)


if __name__ == "__main__":
    unittest.main()
