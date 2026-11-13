"""Settings the keyboard keeps itself (mxoptions/kbsettings.py): Fn lock read, changed, read back and, where the
keyboard takes something else, put back; never on what is not known for certain, and never in the way of the keys or
the mouse. The engine runs on the test's thread with real transports on FakePorts; devices, answers and ids are
synthetic (tests/support.py)."""

import copy
import json
import logging
import os
import queue
import struct
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.support import Actions, FakeDevice, FakePort  # noqa: E402
from tests.test_keyboard import COPY, UNIT_A, UNIT_B, Rig, identity, keyboard_device, mouse_device  # noqa: E402
from mxoptions import actions, config, device, engine, hidpp, kbsettings, keyboard  # noqa: E402

FnCodec = kbsettings.FnCodec
HOSTS = device.FN_INVERSION_HOSTS


def fn_keyboard(unit=UNIT_A, states=None, current=0, **kwargs):
    """A synthetic keyboard with Fn inversion per channel, on channel ``current`` of three. State 1: the bare top-row
    key gives its other function (value False); default state 1 too."""
    states = states if states is not None else {0: [1, 1], 1: [0, 1], 2: [1, 1]}
    return keyboard_device(unit, hosts=(3, current), settings={device.HOSTS_INFO: current, HOSTS: states}, **kwargs)


def fn(rig):
    return rig.state()["controls"].get("fn")


def packets(port, dev, feature, function):
    index = dev.index(feature)
    return [p for p in port.written if p[1] == 1 and p[2] == index and p[3] >> 4 == function]


class Stub:
    """A device for the codec alone: the features it lists and what each (feature, function) answers."""

    def __init__(self, features, answers=(), host_count=3):
        self.features, self.answers, self.host_count, self.calls = set(features), dict(answers), host_count, []

    def has(self, feature):
        return feature in self.features

    def call(self, feature, function, *params):
        self.calls.append((feature, function, bytes(params)))
        answer = self.answers.get((feature, function), b"")
        if isinstance(answer, Exception):
            raise answer
        return answer


class Decode(unittest.TestCase):
    def test_per_channel_state_default_and_what_does_not_fit(self):
        dev = Stub({HOSTS, device.HOSTS_INFO})
        for reply, value, default in ((b"\x01\x00\x00", True, True), (b"\x01\x01\x01", False, False),
                                      (b"\x01\x00\x01\xee\xee", True, False), (b"\x01\x01\x02", False, None),
                                      (b"\x01\x00\xff", True, None)):
            with self.subTest(reply=reply.hex()):
                dev.answers[HOSTS, 0] = reply
                self.assertEqual(FnCodec.read(dev, 1), {"value": value, "default": default})
                self.assertEqual(dev.calls[-1], (HOSTS, 0, b"\x01"))
        for reply in (b"\x02\x00\x00", b"\x01\x02\x00", b"\x01\xff\x00", b"\x01\x00", b""):
            with self.subTest(reply=reply.hex()), self.assertRaises(kbsettings.BadReply):
                dev.answers[HOSTS, 0] = reply
                FnCodec.read(dev, 1)

    def test_the_features_without_channels(self):
        dev = Stub({device.FN_INVERSION_DEFAULT})
        for reply, expected in ((b"\x00\x01", {"value": True, "default": False}),
                                (b"\x01\x00", {"value": False, "default": True}),
                                (b"\x01\x07", {"value": False, "default": None}),
                                (b"\x00", {"value": True, "default": None})):
            dev.answers[device.FN_INVERSION_DEFAULT, 0] = reply
            self.assertEqual(FnCodec.read(dev, None), expected)
        self.assertEqual(dev.calls[-1], (device.FN_INVERSION_DEFAULT, 0, b""))
        dev = Stub({device.FN_INVERSION})
        for reply, value in ((b"\x00\x05", True), (b"\x01", False), (b"\x01\x00", False), (b"\x00\x01", True)):
            dev.answers[device.FN_INVERSION, 0] = reply
            self.assertEqual(FnCodec.read(dev, None), {"value": value, "default": None})
        for reply in (b"\x11", b"\x10", b"\x02", b""):
            with self.subTest(reply=reply.hex()), self.assertRaises(kbsettings.BadReply):
                dev.answers[device.FN_INVERSION, 0] = reply
                FnCodec.read(dev, None)

    def test_setters_and_the_channel_they_name(self):
        for features, host, value, sent in (({HOSTS, device.HOSTS_INFO}, 2, True, (HOSTS, 1, b"\x02\x00")),
                                            ({HOSTS, device.HOSTS_INFO}, 0, False, (HOSTS, 1, b"\x00\x01")),
                                            ({device.FN_INVERSION_DEFAULT}, None, True, (device.FN_INVERSION_DEFAULT, 1, b"\x00")),
                                            ({device.FN_INVERSION}, None, False, (device.FN_INVERSION, 1, b"\x01"))):
            dev = Stub(features | {device.FN_INVERSION})  # the first feature listed in FnCodec.features is used
            FnCodec.write(dev, host, value)
            self.assertEqual(dev.calls, [sent])
        dev = Stub({device.HOSTS_INFO}, {(device.HOSTS_INFO, 0): b"\x01\x00\x03\x02\xee"})
        self.assertEqual(kbsettings.current_host(dev, 2), 2)
        self.assertEqual(dev.calls, [(device.HOSTS_INFO, 0, b"")])

    def test_a_channel_that_is_not_certain_is_unsupported(self):
        for features, reply, current in (({HOSTS}, b"\x00\x00\x03\x01", 1),                      # no host information
                                         ({device.HOSTS_INFO}, b"\x00\x00\x03\x03", 3),          # past the channels
                                         ({device.HOSTS_INFO}, b"\x00\x00\x03\x01", 0),          # not CHANGE_HOST's
                                         ({device.HOSTS_INFO}, b"\x00\x00\x03", 0)):             # too short
            with self.subTest(reply=reply.hex(), current=current), self.assertRaises(kbsettings.Unsupported):
                kbsettings.current_host(Stub(features, {(device.HOSTS_INFO, 0): reply}), current)
        self.assertFalse(FnCodec.supported(Stub({HOSTS})))
        self.assertTrue(FnCodec.supported(Stub({HOSTS, device.HOSTS_INFO})))
        self.assertTrue(FnCodec.supported(Stub({device.FN_INVERSION})))
        self.assertFalse(FnCodec.supported(Stub(set())))

    def test_only_a_bool_is_a_value(self):
        self.assertTrue(FnCodec.valid(True) and FnCodec.valid(False))
        for value in (1, 0, "true", None, [], 1.0):
            self.assertFalse(FnCodec.valid(value), value)


class Levels:
    """A made-up codec with four values on a made-up feature, for what a two-valued setting cannot show: a keyboard
    that takes a third value. Not a real feature or layout."""

    name = "level"
    features = (0x7F01,)

    @staticmethod
    def supported(dev):
        return dev.has(0x7F01)

    @staticmethod
    def needs_host(dev):
        return False

    @staticmethod
    def valid(value):
        return type(value) is int and 0 <= value <= 3

    @staticmethod
    def read(dev, host):
        state = dev.call(0x7F01, 0)[0]
        if state > 3:
            raise kbsettings.BadReply("out of range")
        return {"value": state, "default": None}

    @staticmethod
    def write(dev, host, value):
        dev.call(0x7F01, 1, value)


class Base(unittest.TestCase):
    """A rig with an Fn keyboard on slot 1 and a mouse on slot 2, found and read."""

    def make(self, kb=None, keys=None, **rig):
        self.dev = kb or fn_keyboard()
        self.rig = Rig(self, {1: self.dev, 2: mouse_device()}, keys=keys, **rig).start()
        return self.rig

    def hold(self, when):
        """``keyboard._call`` that holds the first request ``when`` picks back until the Event returned is set."""
        entered, release, real = threading.Event(), threading.Event(), keyboard._call

        def call(dev, request):
            if when(request) and not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(5.0))
            return real(dev, request)
        self.addCleanup(release.set)
        patcher = mock.patch.object(keyboard, "_call", call)
        patcher.start()
        self.addCleanup(patcher.stop)
        return entered, release

    def reached(self, entered, timeout=3.0):
        """Run what the engine's thread would until the held request is reached."""
        end = time.monotonic() + timeout
        while not entered.is_set() and time.monotonic() < end:
            try:
                self.rig.engine._handle(self.rig.engine._queue.get(timeout=0.02))
            except queue.Empty:
                pass
        return entered.is_set()

    def drain(self):
        time.sleep(0.4)
        while not self.rig.engine._queue.empty():
            self.rig.engine._handle(self.rig.engine._queue.get())

    def set(self, value, settle=True):
        self.rig.kb.set_setting("fn", value)
        if settle:
            self.rig.settle()


class Transaction(Base):
    def test_read_once_found_then_changed_and_read_back(self):
        rig = self.make()
        self.assertEqual(fn(rig), {"value": False, "default": False, "writable": True, "busy": False, "outcome": None})
        port, dev = rig.port, self.dev
        reads = len(packets(port, dev, HOSTS, 0))
        self.assertEqual(reads, 1)
        self.set(True)
        self.assertEqual(dev.setting_writes, [(HOSTS, b"\x00\x00")])
        self.assertEqual(dev.settings[HOSTS][0], [0, 1])
        self.assertEqual(fn(rig), {"value": True, "default": False, "writable": True, "busy": False, "outcome": None})
        self.assertEqual(len(packets(port, dev, HOSTS, 0)), reads + 2)  # fresh read before, read back after
        setter = packets(port, dev, HOSTS, 1)
        self.assertEqual(len(setter), 1)
        self.assertEqual(setter[0][4:6], b"\x00\x00")       # channel 0 (HostsInfo byte 3), state 0
        self.assertEqual(set(setter[0][6:]), {0})          # nothing else in it
        self.assertEqual(dev.settings[HOSTS][1], [0, 1])   # the other channels untouched
        self.assertEqual(dev.settings[HOSTS][2], [1, 1])

    def test_the_channel_is_the_one_the_keyboard_is_on(self):
        rig = self.make(fn_keyboard(current=2))
        self.set(True)
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x02\x00")])
        self.assertEqual(packets(rig.port, self.dev, HOSTS, 0)[-1][4], 2)

    def test_the_features_without_channels_are_changed_too(self):
        for feature, state in ((device.FN_INVERSION_DEFAULT, [1, 1]), (device.FN_INVERSION, [1])):
            with self.subTest(feature=hex(feature)):
                rig = self.make(keyboard_device(settings={feature: state}))
                self.assertEqual(fn(rig)["value"], False)
                self.set(True)
                self.assertEqual(self.dev.setting_writes, [(feature, b"\x00")])
                self.assertEqual(fn(rig)["value"], True)
                self.assertEqual(fn(rig)["outcome"], None)
                rig.stop()

    def test_a_channel_that_is_not_certain_is_never_written(self):
        cases = {
            "no host information": keyboard_device(settings={HOSTS: {0: [1, 1]}}),
            "past the channels": keyboard_device(hosts=(3, 0), settings={device.HOSTS_INFO: 3, HOSTS: {3: [1, 1]}}),
            "another channel": keyboard_device(hosts=(3, 0), settings={device.HOSTS_INFO: 1, HOSTS: {0: [1, 1], 1: [1, 1]}}),
        }
        for name, kb in cases.items():
            with self.subTest(name):
                rig = self.make(kb)
                control = fn(rig)
                self.assertTrue(control is None or (control["writable"] is False and control["value"] is None))
                sent = len(rig.port.written)
                for value in (True, False):
                    self.set(value)
                self.assertEqual(rig.port.written[sent:], [])  # not even asked
                self.assertEqual(kb.setting_writes, [])
                self.assertEqual(packets(rig.port, kb, HOSTS, 1), [])
                self.assertEqual([p[4] for p in packets(rig.port, kb, HOSTS, 0)], [])  # not even read
                rig.stop()

    def test_a_channel_no_longer_certain_at_the_change_sends_nothing(self):
        rig = self.make()
        self.dev.settings[device.HOSTS_INFO] = 1  # says another channel than CHANGE_HOST did
        self.set(True)
        self.assertEqual(packets(rig.port, self.dev, HOSTS, 1), [])
        self.assertEqual(fn(rig), {"value": None, "default": None, "writable": False, "busy": False,
                                   "outcome": "unsupported"})
        self.assertEqual(rig.kb._pending, {})

    def test_already_so_nothing_is_sent(self):
        rig = self.make()
        self.set(False)
        self.assertEqual(self.dev.setting_writes, [])
        self.assertIsNone(fn(rig)["outcome"])

    def test_a_keyboard_that_ignores_the_change(self):
        rig = self.make()
        self.dev.ignore_writes = True
        self.set(True)
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])  # once, nothing sent back
        self.assertEqual(fn(rig)["outcome"], "not-applied")
        self.assertEqual(fn(rig)["value"], False)

    def test_a_setter_refused_is_read_back(self):
        rig = self.make()
        self.dev.failing[(HOSTS, 1)] = 5
        self.set(True)
        self.assertEqual(len(packets(rig.port, self.dev, HOSTS, 1)), 1)
        self.assertEqual(fn(rig)["outcome"], "not-applied")
        self.assertEqual(fn(rig)["value"], False)

    def test_a_setter_without_an_answer(self):
        class Mute(FakeDevice):
            """Takes the change but does not answer it; then, if ``then_silent``, answers nothing at all."""
            then_silent = False

            def answer(self, feat_idx, function, params):
                reply = super().answer(feat_idx, function, params)
                if feat_idx < len(self.features) and self.features[feat_idx] == HOSTS and function == 1:
                    self.silent = self.then_silent
                    return None
                return reply

        for then_silent in (False, True):
            with self.subTest(then_silent=then_silent):
                kb = Mute(controls=(), hosts=(3, 0), settings={device.HOSTS_INFO: 0, HOSTS: {0: [1, 1]}})
                kb.then_silent = then_silent
                rig = self.make(kb)
                start = time.monotonic()
                self.set(True)
                self.assertEqual(len(packets(rig.port, kb, HOSTS, 1)), 1)  # never a second, blind one
                if not then_silent:
                    self.assertEqual(fn(rig), dict(fn(rig), value=True, outcome=None))
                    rig.stop()
                    continue
                self.assertEqual(fn(rig)["outcome"], "unconfirmed")
                self.assertIsNone(fn(rig)["value"])
                self.assertFalse(fn(rig)["writable"])
                at = rig.kb._settings_at
                self.assertGreaterEqual(at, start + keyboard.RECOVERY_RETRY)
                self.assertLessEqual(at, time.monotonic() + keyboard.RECOVERY_RETRY)
                kb.silent = False
                self.assertGreaterEqual(rig.kb.due(time.monotonic()), keyboard.RECOVERY_RETRY - 5.0)  # not before
                rig.kb._settings_at = time.monotonic()  # that time come
                rig.kb.tick(time.monotonic())
                rig.settle()
                self.assertEqual(fn(rig)["value"], True)
                self.assertTrue(fn(rig)["writable"])
                self.assertEqual(len(kb.setting_writes), 1)
                rig.stop()

    def test_a_keyboard_that_takes_another_value_gets_the_earlier_one_back(self):
        for taken, outcome, last in (([3], "restored", 1), ([3, 3], "unconfirmed", 3)):
            with self.subTest(outcome=outcome), mock.patch.dict(kbsettings.CODECS, level=Levels):
                kb = keyboard_device(settings={0x7F01: [1]})
                kb.taken[0x7F01] = list(taken)
                rig = self.make(kb)
                self.assertEqual(rig.state()["controls"]["level"]["value"], 1)
                rig.kb.set_setting("level", 2)
                rig.settle()
                self.assertEqual(kb.setting_writes, [(0x7F01, b"\x02"), (0x7F01, b"\x01")])  # the earlier value back
                self.assertEqual(rig.state()["controls"]["level"]["outcome"], outcome)
                self.assertEqual(rig.state()["controls"]["level"]["value"], last)
                rig.stop()

    def test_a_read_only_field_that_changes_does_not_count(self):
        rig = self.make()
        real = self.dev._setting

        def setting(feature, function, params):  # the setter also changes the default the keyboard tells
            reply = real(feature, function, params)
            if feature == HOSTS and function == 1:
                self.dev.settings[HOSTS][0][1] = 0
            return reply
        self.dev._setting = setting
        self.set(True)
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])  # no restore
        self.assertEqual(fn(rig), dict(fn(rig), value=True, default=True, outcome=None))

    def test_an_answer_that_does_not_fit_is_unknown_and_never_written(self):
        for reply in (b"\x01\x00\x00", b"\x00\x02\x00", b"\x00\xff\x01"):  # another channel, no state
            with self.subTest(reply=reply.hex()):
                kb = fn_keyboard()
                kb.replies[(HOSTS, 0)] = reply
                rig = self.make(kb)
                self.assertEqual(fn(rig), {"value": None, "default": None, "writable": False, "busy": False,
                                           "outcome": None})
                sent = len(rig.port.written)
                self.set(True)
                self.assertEqual(kb.setting_writes, [])
                self.assertEqual(rig.port.written[sent:], [])  # not even asked
                rig.stop()

    def test_an_answer_gone_bad_before_the_change_sends_nothing(self):
        rig = self.make()
        self.dev.replies[(HOSTS, 0)] = b"\x00\x05\x00"
        self.set(True)
        self.assertEqual(self.dev.setting_writes, [])
        self.assertEqual(fn(rig)["outcome"], "unknown")
        self.assertIsNone(fn(rig)["value"])
        self.assertFalse(fn(rig)["busy"])

    def test_a_keyboard_without_the_feature_has_no_control(self):
        rig = self.make(keyboard_device())
        self.assertEqual(rig.state()["controls"], {})
        self.set(True)
        rig.kb.set_setting("fn", True)
        self.assertEqual(self.dev.setting_writes, [])
        self.assertNotIn("fn", rig.state()["settings"])

    def test_what_is_not_a_change_of_a_setting_it_has_is_dropped(self):
        rig = self.make()
        sent = len(rig.port.written)
        for name, value in (("fn", 1), ("fn", 0), ("fn", "true"), ("fn", None), ("fn", [True]), ("other", True),
                            (["fn"], True), (None, True), (7, True), ({"fn": 1}, True), (b"fn", True)):
            with self.subTest(name=name, value=value):
                rig.engine.set_keyboard_setting(name, value)
                rig.engine._handle(rig.engine._queue.get(timeout=1.0))
                self.assertIsNone(rig.kb._task)
                self.assertEqual(rig.kb._pending, {})
        self.assertEqual(rig.port.written[sent:], [])
        rig.link(1, False)
        rig.engine.set_keyboard_setting("fn", True)
        rig.engine._handle(rig.engine._queue.get(timeout=1.0))
        self.assertEqual(rig.kb._pending, {})
        self.assertEqual(self.dev.setting_writes, [])
        rig.link(1, True)  # and the engine's way in works once it is back
        rig.settle()
        rig.engine.set_keyboard_setting("fn", True)
        rig.engine._handle(rig.engine._queue.get(timeout=1.0))
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])


class Order(Base):
    def test_keys_called_for_before_the_setter_goes_first_and_the_change_follows(self):
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is kbsettings.current_host)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))
        self.assertTrue(fn(rig)["busy"])
        new = copy.deepcopy(rig.engine.cfg)
        new["keyboard"]["keys"] = {"0x0f01": COPY}
        rig.engine._apply_config(new)
        self.assertEqual(rig.kb._task.__name__, "_configure")  # the write was called off
        self.assertEqual(rig.kb._pending, {"fn": True})  # and its value kept
        release.set()
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])
        self.assertTrue(rig.key(0x0F01)["active"])
        def at(feature, function):
            index = self.dev.index(feature)
            return next(i for i, p in enumerate(rig.port.written) if p[1] == 1 and p[2] == index and p[3] >> 4 == function)
        divert, setter = at(device.REPROG_CONTROLS_V4, 3), at(HOSTS, 1)
        self.assertLess(divert, setter)

    def test_once_the_setter_is_on_its_way_easy_switch_waits_for_the_read_back(self):
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is FnCodec.write)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))
        task = rig.kb._task
        rig.kb.device_action({"type": "host", "host": 1}, "press")
        self.assertIs(rig.kb._task, task)  # not called off
        self.assertEqual(rig.kb._wish, ("host", 1))
        release.set()
        rig.settle()
        self.assertEqual(self.dev.switched, [1])
        self.assertEqual(fn(rig)["value"], True)
        self.assertIsNone(fn(rig)["outcome"])
        def at(feature, function, after=-1):  # where in the order of everything sent, after ``after``
            index = self.dev.index(feature)
            return next(i for i, p in enumerate(rig.port.written)
                        if i > after and p[1] == 1 and p[2] == index and p[3] >> 4 == function)
        setter = at(HOSTS, 1)
        readback = at(HOSTS, 0, setter)
        switch = at(device.CHANGE_HOST, 1)
        self.assertLess(setter, readback)
        self.assertLess(readback, switch)

    def test_a_read_is_called_off_by_the_keys_and_done_again_after(self):
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is FnCodec.read)
        rig.kb._settings_at = 0.0
        rig.kb.tick(time.monotonic())
        self.assertTrue(self.reached(entered))
        rig.kb.device_action({"type": "host", "host": 1}, "press")
        self.assertEqual(rig.kb._task.__name__, "_switch")
        self.assertEqual(rig.kb._settings_at, 0.0)  # read again after
        release.set()
        rig.settle()
        self.assertEqual(self.dev.switched, [1])

    def test_a_change_waits_while_a_key_it_follows_is_down(self):
        rig = self.make(keys={"0x0f01": COPY})
        rig.keys_down(1, self.dev, 0x0F01)
        self.set(True)
        self.assertEqual(self.dev.setting_writes, [])
        self.assertTrue(fn(rig)["busy"])
        self.assertEqual(rig.kb.due(time.monotonic()), float("inf"))  # nothing to wake up for: the key-up starts it
        rig.keys_down(1, self.dev)
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])
        self.assertFalse(fn(rig)["busy"])

    def test_a_read_due_while_a_change_waits_for_a_key_is_made_and_nothing_sent(self):
        rig = self.make(keys={"0x0f01": COPY})
        rig.keys_down(1, self.dev, 0x0F01)
        self.set(True)
        reads = len(packets(rig.port, self.dev, HOSTS, 0))
        self.dev.settings[HOSTS][0][0] = 0
        rig.note(1, self.dev, HOSTS, b"")
        rig.settle()
        self.assertEqual(len(packets(rig.port, self.dev, HOSTS, 0)), reads + 1)
        self.assertEqual(packets(rig.port, self.dev, HOSTS, 1), [])
        self.assertEqual(fn(rig)["value"], True)
        self.assertEqual(rig.kb._pending, {"fn": True})
        rig.keys_down(1, self.dev)
        rig.settle()
        self.assertEqual(packets(rig.port, self.dev, HOSTS, 1), [])  # already so: nothing to send
        self.assertEqual(rig.kb._pending, {})

    def test_a_key_down_after_the_read_holds_the_setter_back(self):
        rig = self.make(keys={"0x0f01": COPY})
        entered, release = self.hold(lambda request: request[0] is FnCodec.read)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))
        rig.keys_down(1, self.dev, 0x0F01)
        release.set()
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [])
        self.assertEqual(rig.kb._pending, {"fn": True})
        rig.keys_down(1, self.dev)
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])


class Going(Base):
    def test_gone_before_the_setter_it_never_goes(self):
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is FnCodec.read)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))
        rig.link(1, False)
        release.set()
        self.drain()
        self.assertEqual(self.dev.setting_writes, [])
        self.assertEqual(rig.state()["controls"], {})
        rig.link(1, True)
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [])  # not asked for again
        self.assertEqual(fn(rig)["value"], False)

    def test_gone_after_the_setter_nothing_is_sent_back(self):
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is FnCodec.read and self.dev.setting_writes)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))
        rig.link(1, False)
        release.set()
        self.drain()
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])
        self.assertEqual(rig.state()["controls"], {})
        self.assertFalse(rig.state()["connected"])
        rig.link(1, True)
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])
        self.assertEqual(fn(rig), dict(fn(rig), value=True, outcome=None))  # read afresh: what the keyboard has

    def test_another_unit_never_gets_what_was_asked_of_the_first(self):
        rig = self.make(keys={"0x0f01": COPY})
        rig.keys_down(1, self.dev, 0x0F01)
        self.set(True)
        self.assertEqual(rig.kb._pending, {"fn": True})
        rig.link(1, False)
        unit_b = rig.port.devices[1] = fn_keyboard(UNIT_B)
        rig.link(1, True)
        rig.settle()
        rig.keys_down(1, unit_b)
        rig.settle()
        self.assertEqual(unit_b.setting_writes, [])
        self.assertEqual(self.dev.setting_writes, [])
        self.assertEqual(fn(rig)["value"], False)

    def test_on_exit_a_change_asked_for_never_goes(self):
        rig = self.make(keys={"0x0f01": COPY})
        rig.keys_down(1, self.dev, 0x0F01)
        self.set(True)
        start = time.monotonic()
        rig.kb.shutdown()
        self.assertLess(time.monotonic() - start, keyboard.SHUTDOWN_BUDGET)
        self.assertEqual(self.dev.setting_writes, [])

    def test_on_exit_during_the_read_the_setter_never_goes(self):
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is FnCodec.read)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))
        exiting = threading.Thread(target=rig.kb.shutdown)
        exiting.start()
        self.assertTrue(rig.kb._called_off.wait(2.0))
        release.set()
        exiting.join(5.0)
        time.sleep(0.2)
        self.assertEqual(self.dev.setting_writes, [])
        self.assertEqual(packets(rig.port, self.dev, HOSTS, 1), [])


class Notified(Base):
    def test_a_report_of_a_change_on_the_keyboard_is_read_not_believed(self):
        rig = self.make()
        reads = len(packets(rig.port, self.dev, HOSTS, 0))
        self.dev.settings[HOSTS][0][0] = 0  # its own Fn lock shortcut
        rig.note(1, self.dev, HOSTS, b"\x00\x01")  # says state 1: not taken from the report
        rig.settle()
        self.assertEqual(len(packets(rig.port, self.dev, HOSTS, 0)), reads + 1)
        self.assertEqual(fn(rig)["value"], True)
        self.assertEqual(self.dev.setting_writes, [])

    def test_a_burst_of_reports_is_one_read_more(self):
        rig = self.make()
        reads = len(packets(rig.port, self.dev, HOSTS, 0))
        entered, release = self.hold(lambda request: request[0] is FnCodec.read)
        for _ in range(5):
            rig.port.notify(bytes([0x11, 1, self.dev.index(HOSTS), 0x00]) + bytes(16))
        handled, end = 0, time.monotonic() + 3.0
        while (handled < 5 or not entered.is_set()) and time.monotonic() < end:
            try:
                item = rig.engine._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            handled += item[0] == "note"
            rig.engine._handle(item)
        self.assertTrue(entered.is_set())
        self.assertEqual(handled, 5)
        release.set()
        rig.settle()
        self.assertEqual(len(packets(rig.port, self.dev, HOSTS, 0)), reads + 2)


class LastWins(Base):
    def last_wins(self, last):
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is FnCodec.read)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))
        for value in (False, True, False, last):
            rig.kb.set_setting("fn", value)
        self.assertEqual(rig.kb._pending, {"fn": last})
        release.set()
        rig.settle()
        self.assertEqual(fn(rig)["value"], last)
        return self.dev.setting_writes

    def test_before_the_setter_only_the_last_value_goes(self):
        self.assertEqual(self.last_wins(True), [(HOSTS, b"\x00\x00")])

    def test_before_the_setter_the_last_value_back_to_what_it_was_sends_nothing(self):
        self.assertEqual(self.last_wins(False), [])

    def test_after_the_setter_the_next_value_is_a_change_of_its_own(self):
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is FnCodec.write)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))
        rig.kb.set_setting("fn", False)  # too late to call the first back
        rig.kb.set_setting("fn", True)
        rig.kb.set_setting("fn", False)
        self.assertEqual(rig.kb._pending, {"fn": False})
        release.set()
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00"), (HOSTS, b"\x00\x01")])
        self.assertEqual(fn(rig)["value"], False)
        self.assertIsNone(fn(rig)["outcome"])


class Guarded(Base):
    """No setting is changed where no key may be (keyboard.KeyboardSide._blocked): other software for the device
    running, a keyboard that does not say which one it is, a journal that cannot be read. It is still read."""

    def rivals(self, *names):
        """Other software is seen to start or stop, as the engine looks for it."""
        self.rig.rivals = list(names)
        self.rig.engine._look_for_rivals(time.monotonic())

    def refused(self, rig, reason):
        self.assertEqual(rig.state()["reason"], reason)
        self.assertEqual(fn(rig), dict(fn(rig), value=False, writable=False, busy=False))  # read all the same
        sent = len(rig.port.written)
        for value in (True, False, True):
            rig.engine.set_keyboard_setting("fn", value)
            rig.engine._handle(rig.engine._queue.get(timeout=1.0))
        rig.settle()
        self.assertEqual(rig.kb._pending, {})
        self.assertEqual(rig.port.written[sent:], [])  # not even asked
        self.assertEqual(self.dev.setting_writes, [])

    def test_never_on_a_keyboard_that_does_not_say_which_one_it_is(self):
        rig = self.make(fn_keyboard(info=False))
        self.assertEqual(len(packets(rig.port, self.dev, HOSTS, 0)), 1)
        self.refused(rig, "identity")

    def test_never_while_other_software_runs_and_again_once_it_has_stopped(self):
        rig = self.make(rivals=["Solaar"])
        self.refused(rig, "rivals")
        self.rivals()
        self.assertIsNone(rig.state()["reason"])
        self.assertTrue(fn(rig)["writable"])  # without the keyboard having to be found again
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [])  # nothing asked for while it ran is sent now
        self.set(True)
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])
        self.assertEqual(fn(rig), dict(fn(rig), value=True, outcome=None))

    def test_never_while_the_journal_cannot_be_read_and_again_once_it_can(self):
        rig = self.make(journal="not a journal")
        self.refused(rig, "journal")
        os.remove(rig.journal)
        rig.kb._next_recovery = 0.0  # its time come: the keys are tried again
        rig.kb.tick(time.monotonic())
        rig.settle()
        self.assertIsNone(rig.state()["reason"])
        self.assertTrue(fn(rig)["writable"])
        self.set(True)
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])

    def test_other_software_seen_during_the_read_before_the_change_sends_nothing(self):
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is FnCodec.read)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))
        self.rivals("Solaar")
        self.assertEqual(rig.kb._pending, {})
        release.set()
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [])
        self.assertEqual(packets(rig.port, self.dev, HOSTS, 1), [])
        self.assertEqual(fn(rig), dict(fn(rig), value=False, writable=False, busy=False, outcome=None))
        self.rivals()
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [])  # not sent once it has stopped either

    def test_a_journal_gone_bad_during_the_read_before_the_change_sends_nothing(self):
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is FnCodec.read)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))
        with open(rig.journal, "w") as f:
            f.write("not a journal")
        release.set()
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [])
        self.assertEqual(rig.state()["reason"], "journal")
        self.assertEqual(fn(rig), dict(fn(rig), value=False, writable=False, busy=False, outcome=None))
        self.assertEqual(rig.kb._pending, {})

    def test_a_setter_waiting_behind_the_mouse_is_called_off_once_other_software_is_seen(self):
        rig = self.make()
        mouse = rig.port.devices[2]
        entered, release = self.hold(lambda request: request[0] is FnCodec.write)
        self.set(True, settle=False)
        self.assertTrue(self.reached(entered))  # on the keyboard's thread, not asked of the receiver yet
        mouse.delay = 0.5  # the mouse's own request holds the receiver meanwhile
        foreground = threading.Thread(target=rig.transport().request, args=(2, 0, 1, bytes([0, 0, 1])))
        foreground.start()
        time.sleep(0.05)
        release.set()
        time.sleep(0.1)
        self.assertTrue(rig.transport()._busy)
        self.assertIs(rig.kb._asked[0], FnCodec.write)  # waiting for its turn
        rig.kb.device_action({"type": "host", "host": 1}, "press")  # waits for the change to be over
        self.rivals("Solaar")
        foreground.join()
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [])
        self.assertEqual(packets(rig.port, self.dev, HOSTS, 1), [])
        self.assertEqual(self.dev.switched, [1])  # what was wished for after it still goes
        self.assertEqual(fn(rig), dict(fn(rig), writable=False, busy=False, outcome="unconfirmed"))
        self.assertEqual(rig.kb._settings_at, 0.0)  # read again: whether it went is for the keyboard to say

    def taken(self, gone_bad):
        """The made-up four-value codec: the keyboard takes 3 for 2; ``gone_bad`` makes changes not allowed while
        the read back is on its way. Returns the rig, after."""
        kb = keyboard_device(settings={0x7F01: [1]})
        kb.taken[0x7F01] = [3]
        rig = self.make(kb)
        entered, release = self.hold(lambda request: request[0] is Levels.read and kb.setting_writes)
        rig.kb.set_setting("level", 2)
        self.assertTrue(self.reached(entered))
        gone_bad(rig)
        release.set()
        rig.settle()
        self.assertEqual(kb.setting_writes, [(0x7F01, b"\x02")])  # never the earlier value sent back
        return rig

    def test_other_software_seen_after_the_setter_nothing_is_sent_back(self):
        with mock.patch.dict(kbsettings.CODECS, level=Levels):
            rig = self.taken(lambda rig: self.rivals("Solaar"))
            self.assertEqual(rig.state()["controls"]["level"], dict(rig.state()["controls"]["level"], value=3,
                                                                    writable=False, busy=False, outcome="unconfirmed"))

    def test_a_journal_gone_bad_after_the_setter_nothing_is_sent_back(self):
        def gone_bad(rig):
            with open(rig.journal, "w") as f:
                f.write("not a journal")
        with mock.patch.dict(kbsettings.CODECS, level=Levels):
            rig = self.taken(gone_bad)
            self.assertEqual(rig.state()["controls"]["level"], dict(rig.state()["controls"]["level"], value=3,
                                                                    writable=False, busy=False, outcome="unconfirmed"))

    def test_a_change_waiting_for_a_key_is_dropped_once_other_software_is_seen(self):
        rig = self.make(keys={"0x0f01": COPY})
        rig.keys_down(1, self.dev, 0x0F01)
        self.set(True)
        self.assertEqual(rig.kb._pending, {"fn": True})
        self.rivals("Solaar")
        self.rivals()
        rig.keys_down(1, self.dev)
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [])  # asked for before: never sent after
        self.assertTrue(fn(rig)["writable"])
        self.set(True)  # asked for afresh: sent
        self.assertEqual(self.dev.setting_writes, [(HOSTS, b"\x00\x00")])

    def test_other_software_seen_with_no_keyboard_or_no_change_under_way(self):
        rig = self.make()
        self.rivals("Solaar")
        self.assertIsNone(rig.kb._task)
        self.assertEqual(fn(rig), dict(fn(rig), value=False, writable=False))
        self.rivals()
        rig.link(1, False)
        self.rivals("Solaar")
        self.rivals()
        rig.link(1, True)
        rig.settle()
        self.assertTrue(fn(rig)["writable"])


class Mouse(Base):
    def test_the_mouse_is_heard_while_a_change_is_slow(self):
        rig = self.make()
        mouse = rig.port.devices[2]
        rig.engine.cfg["buttons"]["back"] = actions.keys("LEFTSHIFT")
        self.dev.delays = dict.fromkeys((HOSTS, device.HOSTS_INFO), 0.2)
        start = time.monotonic()
        self.set(True, settle=False)
        self.assertLess(time.monotonic() - start, 0.1)  # handed to the keyboard's thread
        for _ in range(3):
            time.sleep(0.05)
            self.assertIsNotNone(rig.kb._asked)  # the keyboard is still being waited for ...
            rig.mouse_actions.ran.clear()
            start = time.monotonic()
            rig.port.notify(bytes([0x11, 2, mouse.index(device.REPROG_CONTROLS_V4), 0]) + struct.pack("!HHHH", 0x0053, 0, 0, 0).ljust(16, b"\0"))
            while not rig.mouse_actions.ran:  # ... and the mouse is heard meanwhile
                rig.engine._handle(rig.engine._queue.get(timeout=1.0))
            self.assertLess(time.monotonic() - start, 0.1)
            self.assertEqual(rig.mouse_actions.ran, [actions.keys("LEFTSHIFT")])
            rig.port.notify(bytes([0x11, 2, mouse.index(device.REPROG_CONTROLS_V4), 0]) + bytes(16))
        rig.settle()
        self.assertEqual(fn(rig)["value"], True)
        writers = {w for p, w in zip(rig.port.written, rig.port.writers) if p in packets(rig.port, self.dev, HOSTS, 1)
                   or p in packets(rig.port, self.dev, HOSTS, 0)}
        self.assertEqual(writers, {"keyboard"})


class Kept(Base):
    def test_nothing_of_it_is_in_the_settings(self):
        with mock.patch.object(config, "save", side_effect=AssertionError("saved")), \
                mock.patch.object(config, "write_durable", side_effect=AssertionError("written")):
            rig = self.make()
            before, exported = copy.deepcopy(rig.engine.cfg), config.export_data(rig.engine.cfg)
            self.set(True)
            self.dev.ignore_writes = True
            self.set(False)
            rig.note(1, self.dev, HOSTS, b"")
            rig.settle()
            self.assertEqual(rig.engine.cfg, before)
            self.assertEqual(config.export_data(rig.engine.cfg), exported)

    def test_logs_and_state_name_the_setting_and_the_outcome_only(self):
        with self.assertLogs("mxoptions", logging.DEBUG) as logs:
            rig = self.make(fn_keyboard(current=2, states={2: [1, 1]}))
            self.set(True)
            self.dev.ignore_writes = True
            self.set(False)
            self.dev.replies[(HOSTS, 0)] = b"\x02\x07\x09"
            self.set(False)
            rig.kb.set_setting(["no"], True)
        text = "\n".join(logs.output)
        state = json.dumps(rig.state())
        for private in (identity(UNIT_A), UNIT_A.hex(), "5e015e02", "020709", "\\x07", "flow_link", rig.kb.link):
            self.assertNotIn(private, text)
            self.assertNotIn(private, state)
        for control in rig.state()["controls"].values():
            self.assertEqual(set(control), {"value", "default", "writable", "busy", "outcome"})
        self.assertEqual(rig.state()["controls"]["fn"]["outcome"], "unknown")
        self.assertIn("keyboard setting fn: the keyboard did not take the change", text)


class Page(unittest.TestCase):
    """The Fn lock row of the Keyboard page (Qt offscreen)."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication
        from mxoptions.ui import keyboard_page
        from mxoptions.ui.store import Store
        self.app = QApplication.instance() or QApplication([])
        self.pages = keyboard_page
        calls = self.calls = []

        class Engine:
            def set_keyboard_setting(self, name, value):
                calls.append((name, value))

            def __getattr__(self, name):
                return lambda *args: None
        patcher = mock.patch.object(keyboard_page.firmware, "available", lambda: False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(copy.deepcopy(config.DEFAULTS), Engine())
        self.store.flush = lambda: None
        self.store._save_timer.timeout.disconnect()
        self.page = keyboard_page.KeyboardPage(self.store)
        self.addCleanup(self.page.deleteLater)
        self.page.resize(900, 700)
        self.page.show()
        self.switch, self.row = self.page.fn_switch, self.page.settings_rows["fn"]

    def show(self, control=None, connected=True, **extra):
        found = {"connected": connected, "known": True, "name": "Synthetic keyboard", "battery": None, "current_host": 0,
                 "hosts": [], "keys": [], "reason": None, "failed": None, "firmware": None, "settings": ["fn"],
                 "controls": {"fn": dict({"value": True, "default": None, "writable": True, "busy": False,
                                          "outcome": None}, **(control or {}))} if control is not None else {}}
        found.update(extra)
        self.store.update_state(dict(self.store.state, keyboard=found))

    def test_a_switch_only_while_the_keyboard_lets_it_be_changed(self):
        self.show()
        self.assertTrue(self.switch.isHidden())
        self.assertIn("MX Options does not", self.row.note.text())
        self.assertEqual(self.row.info.text(), "")
        self.show({})
        self.assertFalse(self.switch.isHidden())
        self.assertTrue(self.switch.isChecked() and self.switch.isEnabled())
        self.assertEqual(self.switch.accessibleName(), "Use F1–F12 without Fn")
        self.assertEqual(self.row.info.text(), self.pages.FN_HINT)
        self.assertTrue(self.row.note.isHidden())
        self.show({}, connected=False)
        self.assertTrue(self.switch.isHidden())
        self.assertEqual(self.calls, [])

    def test_disabled_while_unknown_not_writable_or_busy(self):
        for control, note in (({"value": None, "writable": False}, "The keyboard did not say how this is set."),
                              ({"writable": False}, ""), ({"busy": True}, "")):
            with self.subTest(control=control):
                self.show(control)
                self.assertFalse(self.switch.isEnabled())
                self.assertEqual(self.row.note.text(), note)
        self.assertEqual(self.calls, [])

    def test_a_toggle_asks_the_engine_once_and_the_switch_shows_the_keyboard(self):
        self.show({"value": False})
        self.assertFalse(self.switch.isChecked())
        self.switch.click()
        self.assertEqual(self.calls, [("fn", True)])
        self.show({"value": False, "busy": True})
        self.assertTrue(self.switch.isChecked())  # what was asked for, until the keyboard answers
        self.show({"value": False, "outcome": "not-applied"})
        self.assertFalse(self.switch.isChecked())
        self.assertEqual(self.row.note.text(), "The keyboard did not take the change.")
        self.show({"value": True})
        self.assertTrue(self.switch.isChecked())
        self.assertEqual(self.calls, [("fn", True)])  # following the keyboard asks nothing

    def test_every_outcome_has_its_note(self):
        for outcome, text in self.pages.OUTCOMES.items():
            with self.subTest(outcome):
                self.show({"outcome": outcome})
                self.assertEqual(self.row.note.text(), text)
                self.assertFalse(self.row.note.isHidden())

    def test_the_new_words_are_translated(self):
        from mxoptions import i18n_uk
        for text in (*self.pages.OUTCOMES.values(), self.pages.FN_HINT, "Use F1–F12 without Fn",
                     "Settings the keyboard controls itself. Changes are made on the keyboard. Its own shortcuts may "
                     "change them too."):
            self.assertIn(text, i18n_uk.STRINGS)



# -- keys turned off (0x4521) and the operating system layout (0x4531, 0x4530 shown) --------------------------------

DisableCodec, PlatformCodec = kbsettings.DisableCodec, kbsettings.PlatformCodec
DK, MP, DP = device.DISABLE_KEYS, device.MULTI_PLATFORM, device.DUAL_PLATFORM
ALL_KEYS = ["caps_lock", "num_lock", "scroll_lock", "insert", "super"]


def info(flags=0x02, platforms=3, count=4, hosts=3, slot=0, current=1):
    """MULTI_PLATFORM function 0's answer."""
    return bytes([flags, 0, platforms, count, hosts, slot, current])


def descriptor(platform, index, mask):
    """MULTI_PLATFORM function 1's answer: platform, its own index, operating systems (big-endian), versions."""
    return bytes([platform, index]) + mask.to_bytes(2, "big") + b"\x01\x02\x03\x04"


# platform 0: Windows (0x0100) + Android (0x1000) in two descriptors; 1: macOS + iOS; 2: Linux
DESCRIPTORS = [descriptor(0, 0, 0x0100), descriptor(0, 1, 0x1000), descriptor(1, 2, 0x6000), descriptor(2, 3, 0x0400)]
CHOICES = [{"index": 0, "os": ["windows", "android"]}, {"index": 1, "os": ["macos", "ios"]}, {"index": 2, "os": ["linux"]}]


def settings_keyboard(disable=(0x1F, 0x00), platform=None, dual=None, hosts_info=True, current=0, unit=UNIT_A, **kwargs):
    """A synthetic keyboard with keys it can turn off ((caps, current) bits) and, given, a multi-platform layout
    ({"info", "descriptors"}) or a dual one ([platform]), on channel ``current`` of three."""
    settings = {}
    if hosts_info:
        settings[device.HOSTS_INFO] = current
    if disable is not None:
        settings[DK] = {"caps": disable[0], "current": disable[1]}
    if platform is not None:
        settings[MP] = copy.deepcopy(platform)
    if dual is not None:
        settings[DP] = list(dual)
    return keyboard_device(unit, hosts=(3, current), settings=settings, **kwargs)


def multi(**kwargs):
    return {"info": info(**kwargs), "descriptors": list(DESCRIPTORS)}


def control(rig, name):
    return rig.state()["controls"].get(name)


class MuteAfter(FakeDevice):
    """Takes a setter of ``mute`` (feature, function) but answers nothing from then on."""
    mute = None

    def answer(self, feat_idx, function, params):
        reply = super().answer(feat_idx, function, params)
        if feat_idx < len(self.features) and (self.features[feat_idx], function) == self.mute:
            self.silent = True
            return None
        return reply


class DisableDecode(unittest.TestCase):
    def test_bits_read_and_shown_by_name(self):  # DK01
        dev = Stub({DK}, {(DK, 0): b"\x1f", (DK, 1): b"\x05\xee"})
        read = DisableCodec.read(dev, None)
        self.assertEqual(read, {"value": 0x05, "caps": 0x1F})
        self.assertEqual(dev.calls, [(DK, 0, b""), (DK, 1, b"")])
        self.assertEqual(DisableCodec.public(read), {"value": ["caps_lock", "scroll_lock"], "keys": ALL_KEYS})
        dev = Stub({DK}, {(DK, 0): b"\x1d", (DK, 1): b"\x00"})  # no Num Lock
        self.assertEqual(DisableCodec.public(DisableCodec.read(dev, None)),
                         {"value": [], "keys": ["caps_lock", "scroll_lock", "insert", "super"]})
        DisableCodec.write(dev, None, 0x83)
        self.assertEqual(dev.calls[-1], (DK, 2, b"\x83"))
        self.assertFalse(DisableCodec.needs_host(dev))
        self.assertTrue(DisableCodec.supported(dev))
        self.assertFalse(DisableCodec.supported(Stub({device.DISABLE_KEYS_BY_USAGE})))

    def test_bits_nobody_asked_about_go_back_as_read(self):  # DK02
        before = {"value": 0x81, "caps": 0x9F}
        self.assertEqual(DisableCodec.target(before, {"num_lock": True}), 0x83)
        self.assertEqual(DisableCodec.target(before, {"caps_lock": False}), 0x80)
        self.assertEqual(DisableCodec.target(before, {"caps_lock": False, "super": True, "insert": False}), 0x90)
        self.assertEqual(DisableCodec.public(before), {"value": ["caps_lock"], "keys": ALL_KEYS})  # 0x80 not named

    def test_what_does_not_fit_is_not_believed(self):  # DK03, DK13
        for answers in ({(DK, 0): b"\x0f", (DK, 1): b"\x10"},          # turned off but cannot be
                        {(DK, 0): b"", (DK, 1): b"\x00"},              # short
                        {(DK, 0): b"\x1f", (DK, 1): b""}):
            with self.subTest(answers=answers), self.assertRaises(kbsettings.BadReply):
                DisableCodec.read(Stub({DK}, answers), None)
        dev = Stub({DK}, {(DK, 0): b""})
        with self.assertRaises(kbsettings.BadReply):
            DisableCodec.read(dev, None)
        self.assertEqual(dev.calls, [(DK, 0, b"")])  # nothing more asked
        error = hidpp.HidppError("synthetic")
        with self.assertRaises(hidpp.HidppError):
            DisableCodec.read(Stub({DK}, {(DK, 0): b"\x1f", (DK, 1): error}), None)

    def test_a_key_it_cannot_turn_off_is_unsupported(self):  # DK04
        for before in ({"value": 0, "caps": 0x1D}, {"value": 0, "caps": 0}):
            with self.assertRaises(kbsettings.Unsupported):
                DisableCodec.target(before, {"num_lock": True})
        with self.assertRaises(kbsettings.Unsupported):
            DisableCodec.target({"value": 0, "caps": 0x1D}, {"caps_lock": True, "num_lock": False})

    def test_only_keys_by_name_and_on_or_off_are_a_value(self):  # DK10
        self.assertTrue(DisableCodec.valid({"caps_lock": True}))
        self.assertTrue(DisableCodec.valid(dict.fromkeys(ALL_KEYS, False)))
        for value in (0x01, 1, True, None, [], ["caps_lock"], frozenset({"caps_lock"}), {}, {"caps_lock": 1},
                      {"caps_lock": None}, {1: True}, {"Caps_Lock": True}, {"unknown": True}, {b"caps_lock": True},
                      dict(dict.fromkeys(ALL_KEYS, True), other=True), "caps_lock"):
            self.assertFalse(DisableCodec.valid(value), value)
        self.assertEqual(DisableCodec.merge(None, {"caps_lock": True}), {"caps_lock": True})
        self.assertEqual(DisableCodec.merge({"caps_lock": True, "insert": True}, {"caps_lock": False}),
                         {"caps_lock": False, "insert": True})


class PlatformDecode(unittest.TestCase):
    def stub(self, first=None, rows=DESCRIPTORS, features=(MP, device.HOSTS_INFO)):
        answers = {(MP, 0): first if first is not None else info()}
        dev = Stub(features, answers)
        real = dev.call

        def call(feature, function, *params):
            if feature == MP and function == 1:
                dev.calls.append((feature, function, bytes(params)))
                row = rows[params[0]] if params[0] < len(rows) else b""
                if isinstance(row, Exception):
                    raise row
                return row
            return real(feature, function, *params)
        dev.call = call
        return dev

    def test_layouts_by_platform_each_with_every_system_it_covers(self):  # OS01, OS02
        dev = self.stub(info(platforms=5, count=4))  # five platforms said, four descriptors: byte 3 counts
        read = PlatformCodec.read(dev, 0)
        self.assertEqual(read, {"value": 1, "choices": CHOICES, "writable": True})
        self.assertEqual(dev.calls, [(MP, 0, b"")] + [(MP, 1, bytes([i])) for i in range(4)])
        dev = self.stub(info(count=2, current=0), [descriptor(0, 0, 0x2000), descriptor(1, 1, 0x0020)])
        self.assertEqual(PlatformCodec.read(dev, 0)["choices"], [{"index": 0, "os": ["macos"]}, {"index": 1, "os": []}])
        every = sum(bit for _name, bit in kbsettings.OS_BITS)
        dev = self.stub(info(count=1, current=0), [descriptor(0, 0, every)])
        self.assertEqual(PlatformCodec.read(dev, 0)["choices"][0]["os"], [name for name, _bit in kbsettings.OS_BITS])
        self.assertEqual(PlatformCodec.public(read), {"value": 1, "choices": CHOICES})

    def test_read_only_where_the_keyboard_says_so_or_the_channel_is_not_known(self):  # OS06, OS04
        self.assertFalse(PlatformCodec.read(self.stub(info(flags=0x01)), 0)["writable"])
        read = PlatformCodec.read(self.stub(info(slot=2)), None)  # no channel asked: byte 5 not compared
        self.assertEqual(read, {"value": 1, "choices": CHOICES, "writable": False})
        self.assertFalse(PlatformCodec.needs_host(Stub({MP})))
        self.assertTrue(PlatformCodec.needs_host(Stub({MP, device.HOSTS_INFO})))
        self.assertFalse(PlatformCodec.needs_host(Stub({DP, device.HOSTS_INFO})))

    def test_what_does_not_fit_is_not_believed(self):  # OS03, OS04, OS10
        bad = {
            "short": (info()[:6], DESCRIPTORS),
            "another channel": (info(slot=1), DESCRIPTORS),
            "channel past the count": (info(slot=3, hosts=3), DESCRIPTORS),
            "current platform undefined": (info(current=0xFF), DESCRIPTORS),
            "current platform without a descriptor": (info(current=2, count=3), DESCRIPTORS),
            "platform past the count": (info(platforms=2), DESCRIPTORS),
            "descriptor index not echoed": (info(), [descriptor(0, 1, 0x0100)] + DESCRIPTORS[1:]),
            "descriptor for no system": (info(), [descriptor(0, 0, 0)] + DESCRIPTORS[1:]),
            "descriptor short": (info(), [DESCRIPTORS[0][:7]] + DESCRIPTORS[1:]),
            "descriptor missing": (info(count=5), DESCRIPTORS),
        }
        for name, (first, rows) in bad.items():
            with self.subTest(name), self.assertRaises(kbsettings.BadReply):
                PlatformCodec.read(self.stub(first, rows), 0)
        dev = self.stub(info(), DESCRIPTORS[:2] + [hidpp.HidppError("synthetic")] + DESCRIPTORS[3:])
        with self.assertRaises(hidpp.HidppError):  # mid-list: no partial list
            PlatformCodec.read(dev, 0)
        for count in (0, 17, 0xFF):
            with self.subTest(count=count):
                dev = self.stub(info(count=count))
                with self.assertRaises(kbsettings.Unsupported):
                    PlatformCodec.read(dev, 0)
                self.assertEqual(dev.calls, [(MP, 0, b"")])  # no descriptor asked for
        rows = [descriptor(i % 3, i, 0x0100) for i in range(16)]
        self.assertEqual(len(PlatformCodec.read(self.stub(info(count=16), rows), 0)["choices"]), 3)

    def test_the_setter_names_the_channel_and_only_a_platform_it_has(self):  # OS07, OS08
        dev = Stub({MP, device.HOSTS_INFO})
        PlatformCodec.write(dev, 2, 1)
        self.assertEqual(dev.calls, [(MP, 3, b"\x02\x01")])
        with self.assertRaises(kbsettings.Unsupported):
            PlatformCodec.write(dev, None, 1)
        with self.assertRaises(kbsettings.Unsupported):
            PlatformCodec.write(Stub({DP}), 0, 1)
        self.assertEqual(dev.calls, [(MP, 3, b"\x02\x01")])
        before = {"value": 1, "choices": CHOICES, "writable": True}
        self.assertEqual(PlatformCodec.target(before, 2), 2)
        for request, writable in ((3, True), (7, True), (2, False)):
            with self.assertRaises(kbsettings.Unsupported):
                PlatformCodec.target(dict(before, writable=writable), request)

    def test_dual_platform_is_shown_never_set(self):  # DP01, DP02
        for byte, value in ((0, 0), (1, 1)):
            dev = Stub({DP}, {(DP, 0): bytes([byte, 0xEE])})
            self.assertEqual(PlatformCodec.read(dev, None), {"value": value, "writable": False,
                                                             "choices": [dict(c, os=list(c["os"])) for c in kbsettings.DUAL_CHOICES]})
        for reply in (b"\x02", b"\xff", b""):
            with self.subTest(reply=reply.hex()), self.assertRaises(kbsettings.BadReply):
                PlatformCodec.read(Stub({DP}, {(DP, 0): reply}), None)
        both = self.stub(features=(MP, DP, device.HOSTS_INFO))
        PlatformCodec.read(both, 0)
        self.assertNotIn(DP, {call[0] for call in both.calls})  # 0x4530 never asked when 0x4531 is listed
        read = PlatformCodec.read(Stub({DP}, {(DP, 0): b"\x00"}), None)
        read["choices"][0]["os"].append("mutated")
        self.assertEqual(kbsettings.DUAL_CHOICES[0]["os"], ["ios", "macos"])

    def test_only_a_platform_index_is_a_value(self):  # OS13
        self.assertTrue(PlatformCodec.valid(0) and PlatformCodec.valid(0xFE))
        for value in (True, False, -1, 0xFF, 256, "1", 1.0, None, [1], {"index": 1}):
            self.assertFalse(PlatformCodec.valid(value), value)


class SettingsBase(Base):
    def make(self, kb=None, keys=None, **rig):
        return super().make(kb or settings_keyboard(platform=multi()), keys=keys, **rig)

    def ask(self, name, value, settle=True):
        self.rig.kb.set_setting(name, value)
        if settle:
            self.rig.settle()

    def setters(self, feature):
        return [params for f, params in self.dev.setting_writes if f == feature]


class DisableChange(SettingsBase):
    def test_read_once_then_changed_and_read_back(self):  # DK05
        rig = self.make()
        self.assertEqual(control(rig, "disable"), {"value": [], "keys": ALL_KEYS, "writable": True, "busy": False,
                                                   "outcome": None})
        reads = len(packets(rig.port, self.dev, DK, 1))
        self.ask("disable", {"super": True})
        self.assertEqual(self.setters(DK), [b"\x10"])
        self.assertEqual(len(packets(rig.port, self.dev, DK, 1)), reads + 2)  # fresh read before, read back after
        setter = packets(rig.port, self.dev, DK, 2)
        self.assertEqual(len(setter), 1)
        self.assertEqual(set(setter[0][5:]), {0})  # nothing but the bits
        self.assertEqual(control(rig, "disable"), dict(control(rig, "disable"), value=["super"], outcome=None))
        self.ask("disable", {"super": False})
        self.assertEqual(self.setters(DK), [b"\x10", b"\x00"])

    def test_bits_nobody_asked_about_are_kept(self):  # DK02 on the wire
        rig = self.make(settings_keyboard(disable=(0x9F, 0x81)))
        self.assertEqual(control(rig, "disable")["value"], ["caps_lock"])
        self.assertEqual(control(rig, "disable")["keys"], ALL_KEYS)
        self.ask("disable", {"num_lock": True})
        self.assertEqual(self.setters(DK), [b"\x83"])
        self.assertIsNone(control(rig, "disable")["outcome"])

    def test_already_so_nothing_is_sent(self):  # DK06
        rig = self.make(settings_keyboard(disable=(0x1F, 0x01)))
        self.ask("disable", {"caps_lock": True, "num_lock": False})
        self.assertEqual(self.setters(DK), [])
        self.assertEqual(packets(rig.port, self.dev, DK, 2), [])

    def test_a_key_it_cannot_turn_off_sends_nothing(self):  # DK04
        rig = self.make(settings_keyboard(disable=(0x1D, 0x00)))
        self.assertEqual(control(rig, "disable")["keys"], ["caps_lock", "scroll_lock", "insert", "super"])
        self.ask("disable", {"caps_lock": True, "num_lock": True})
        self.assertEqual(packets(rig.port, self.dev, DK, 2), [])
        self.assertEqual(control(rig, "disable"), dict(control(rig, "disable"), value=[], outcome="unsupported", busy=False))
        self.assertEqual(rig.kb._pending, {})

    def test_what_does_not_fit_is_unknown_and_never_written(self):  # DK03, DK13
        for name, change in (("outside the capabilities", lambda kb: kb.settings[DK].update(current=0x20)),
                             ("refused read", lambda kb: kb.failing.update({(DK, 1): 5})),
                             ("refused capabilities", lambda kb: kb.failing.update({(DK, 0): 5}))):
            with self.subTest(name):
                kb = settings_keyboard()
                change(kb)
                rig = self.make(kb)
                self.assertEqual(control(rig, "disable"), {"value": None, "writable": False, "busy": False, "outcome": None})
                sent = len(rig.port.written)
                self.ask("disable", {"caps_lock": True})
                self.assertEqual(rig.port.written[sent:], [])  # not even asked
                rig.stop()
        rig = self.make()
        self.dev.settings[DK]["current"] = 0x40  # gone bad before the change
        self.ask("disable", {"caps_lock": True})
        self.assertEqual(packets(rig.port, self.dev, DK, 2), [])
        self.assertEqual(control(rig, "disable"), {"value": None, "writable": False, "busy": False, "outcome": "unknown"})

    def test_a_keyboard_that_ignores_or_refuses_the_change(self):  # DK12
        rig = self.make()
        self.dev.ignore_writes = True
        self.ask("disable", {"insert": True})
        self.assertEqual(self.setters(DK), [b"\x08"])  # once, nothing sent back
        self.assertEqual(control(rig, "disable"), dict(control(rig, "disable"), value=[], outcome="not-applied"))
        self.dev.ignore_writes = False
        self.dev.failing[(DK, 2)] = 5
        self.ask("disable", {"insert": True})
        self.assertEqual(len(packets(rig.port, self.dev, DK, 2)), 2)
        self.assertEqual(control(rig, "disable")["outcome"], "not-applied")

    def test_another_set_taken_gets_the_whole_earlier_one_back(self):  # DK11
        for taken, outcome, last in (([0xA3], "restored", ["caps_lock"]), ([0xA3, 0xA3], "unconfirmed", ["caps_lock", "num_lock"])):
            with self.subTest(outcome=outcome):
                kb = settings_keyboard(disable=(0xBF, 0x81))
                kb.taken[DK] = list(taken)
                rig = self.make(kb)
                self.ask("disable", {"num_lock": True})
                self.assertEqual(self.setters(DK), [b"\x83", b"\x81"])  # the earlier set, unnamed bit too
                self.assertEqual(control(rig, "disable"), dict(control(rig, "disable"), value=last, outcome=outcome))
                rig.stop()

    def test_no_read_back_is_unconfirmed_and_read_again_later(self):  # DK12 (timeout)
        kb = MuteAfter(controls=(), hosts=(3, 0), settings={DK: {"caps": 0x1F, "current": 0}})
        kb.mute = (DK, 2)
        rig = self.make(kb)
        start = time.monotonic()
        self.ask("disable", {"caps_lock": True})
        self.assertEqual(len(packets(rig.port, kb, DK, 2)), 1)  # never a second, blind one
        self.assertEqual(control(rig, "disable"), {"value": None, "writable": False, "busy": False, "outcome": "unconfirmed"})
        self.assertGreaterEqual(rig.kb._settings_at, start + keyboard.RECOVERY_RETRY)


class DisableToggles(SettingsBase):
    """Keys switched one after another: each change names its key alone, and goes over what a fresh read says."""

    def held(self):
        return self.hold(lambda request: request[0] is DisableCodec.read)

    def test_two_keys_before_the_setter_go_in_one(self):  # DK07
        rig = self.make(settings_keyboard(disable=(0x9F, 0x80)))
        entered, release = self.held()
        self.ask("disable", {"caps_lock": True}, settle=False)
        self.assertTrue(self.reached(entered))
        self.ask("disable", {"num_lock": True}, settle=False)
        self.assertEqual(rig.kb._pending, {"disable": {"caps_lock": True, "num_lock": True}})
        self.assertTrue(control(rig, "disable")["busy"])
        release.set()
        rig.settle()
        self.assertEqual(self.setters(DK), [b"\x83"])
        self.assertEqual(control(rig, "disable")["value"], ["caps_lock", "num_lock"])

    def test_on_and_off_again_before_the_setter_sends_nothing(self):  # DK08
        rig = self.make()
        entered, release = self.held()
        self.ask("disable", {"caps_lock": True}, settle=False)
        self.assertTrue(self.reached(entered))
        self.ask("disable", {"caps_lock": False}, settle=False)
        release.set()
        rig.settle()
        self.assertEqual(self.setters(DK), [])

    def test_a_key_after_the_setter_is_a_change_of_its_own_over_the_first(self):  # DK09
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is DisableCodec.write)
        self.ask("disable", {"caps_lock": True}, settle=False)
        self.assertTrue(self.reached(entered))
        self.ask("disable", {"num_lock": True}, settle=False)
        self.assertEqual(rig.kb._pending, {"disable": {"num_lock": True}})
        release.set()
        rig.settle()
        self.assertEqual(self.setters(DK), [b"\x01", b"\x03"])  # never 0x02 alone
        self.assertEqual(control(rig, "disable")["value"], ["caps_lock", "num_lock"])

    def test_bits_changed_on_the_keyboard_meanwhile_are_kept(self):  # DK10 (case 5)
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is DisableCodec.read)
        self.ask("disable", {"caps_lock": True}, settle=False)
        self.assertTrue(self.reached(entered))
        self.dev.settings[DK]["current"] = 0x14  # someone else turned Scroll Lock and Super off
        release.set()
        rig.settle()
        self.assertEqual(self.setters(DK), [b"\x15"])

    def test_a_report_is_read_not_believed_and_a_burst_is_one_read(self):  # DK14
        rig = self.make()
        reads = len(packets(rig.port, self.dev, DK, 1))
        self.dev.settings[DK]["current"] = 0x08
        rig.note(1, self.dev, DK, b"\x1f")
        rig.settle()
        self.assertEqual(len(packets(rig.port, self.dev, DK, 1)), reads + 1)
        self.assertEqual(control(rig, "disable")["value"], ["insert"])
        entered, release = self.held()
        for _ in range(5):
            rig.port.notify(bytes([0x11, 1, self.dev.index(DK), 0x00]) + bytes(16))
        self.assertTrue(self.reached(entered))
        self.drain()
        release.set()
        rig.settle()
        self.assertLessEqual(len(packets(rig.port, self.dev, DK, 1)), reads + 3)  # the one held, and at most one more
        self.assertEqual(self.dev.setting_writes, [])

    def test_what_is_not_a_change_of_keys_it_has_is_dropped(self):  # DK15, GX09
        rig = self.make()
        sent = len(rig.port.written)
        for name, value in (("disable", 0x01), ("disable", ["caps_lock"]), ("disable", {"caps_lock": 1}),
                            ("disable", {}), ("disable", {1: True}), ("disable", None), ("disable", frozenset({"insert"})),
                            (["disable"], {"caps_lock": True}), (None, {"caps_lock": True}), (5, {"caps_lock": True}),
                            ("platform", "1"), ("platform", True), ("platform", -1), ("platform", 0xFF), ("platform", None)):
            with self.subTest(name=name, value=value):
                rig.engine.set_keyboard_setting(name, value)
                rig.engine._handle(rig.engine._queue.get(timeout=1.0))
                self.assertIsNone(rig.kb._task)
                self.assertEqual(rig.kb._pending, {})
        self.assertEqual(rig.port.written[sent:], [])
        rig.link(1, False)
        for name, value in (("disable", {"caps_lock": True}), ("platform", 2)):
            rig.engine.set_keyboard_setting(name, value)
            rig.engine._handle(rig.engine._queue.get(timeout=1.0))
        self.assertEqual(rig.kb._pending, {})
        rig.link(1, True)
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [])


class PlatformChange(SettingsBase):
    def test_read_then_set_on_this_channel_and_read_back(self):  # OS07
        for current in (0, 2):
            with self.subTest(current=current):
                rig = self.make(settings_keyboard(platform=multi(slot=current), current=current))
                self.assertEqual(control(rig, "platform"), {"value": 1, "choices": CHOICES, "writable": True,
                                                            "busy": False, "outcome": None})
                self.ask("platform", 2)
                self.assertEqual(self.setters(MP), [bytes([current, 2])])
                setter = packets(rig.port, self.dev, MP, 3)
                self.assertEqual(len(setter), 1)
                self.assertEqual(setter[0][4:6], bytes([current, 2]))
                self.assertNotEqual(setter[0][4], 0xFF)
                self.assertEqual(set(setter[0][6:]), {0})
                self.assertEqual(control(rig, "platform"), dict(control(rig, "platform"), value=2, outcome=None))
                rig.stop()

    def test_already_so_or_not_a_platform_it_has_sends_nothing(self):  # OS08, OS09
        rig = self.make()
        self.ask("platform", 1)
        self.assertEqual(self.setters(MP), [])
        self.assertIsNone(control(rig, "platform")["outcome"])
        self.ask("platform", 7)
        self.assertEqual(packets(rig.port, self.dev, MP, 3), [])
        self.assertEqual(control(rig, "platform"), dict(control(rig, "platform"), value=1, outcome="unsupported"))

    def test_read_only_where_the_keyboard_says_so(self):  # OS06 and G5 through the transaction
        rig = self.make(settings_keyboard(platform=multi(flags=0x01)))
        self.assertEqual(control(rig, "platform"), dict(control(rig, "platform"), value=1, writable=False))
        self.assertFalse(rig.kb._controls["platform"]["writable"])
        self.ask("platform", 2)  # as from a page still showing it writable
        self.assertEqual(rig.kb._pending, {})
        self.assertEqual(packets(rig.port, self.dev, MP, 3), [])
        rig.stop()
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is PlatformCodec.read)
        self.ask("platform", 2, settle=False)
        self.assertTrue(self.reached(entered))
        self.dev.settings[MP]["info"] = info(flags=0x00)  # no longer lets software set it
        release.set()
        rig.settle()
        self.assertEqual(packets(rig.port, self.dev, MP, 3), [])
        self.assertEqual(control(rig, "platform"), dict(control(rig, "platform"), value=1, writable=False,
                                                        outcome="unsupported"))

    def test_a_channel_that_is_not_certain_is_never_written(self):  # OS04
        rig = self.make(settings_keyboard(platform=multi(), hosts_info=False))  # without HOSTS_INFO: shown only
        self.assertEqual(control(rig, "platform"), {"value": 1, "choices": CHOICES, "writable": False, "busy": False,
                                                    "outcome": None})
        sent = len(rig.port.written)
        self.ask("platform", 2)
        self.assertEqual(rig.port.written[sent:], [])
        rig.stop()
        cases = {"HOSTS_INFO not CHANGE_HOST's": dict(platform=multi(slot=1)),
                 "byte 5 another channel": dict(platform=multi(slot=1), current=0),
                 "byte 5 past the count": dict(platform=multi(slot=0, hosts=0))}
        for name, kwargs in cases.items():
            with self.subTest(name):
                kb = settings_keyboard(**kwargs)
                if name.startswith("HOSTS_INFO"):
                    kb.settings[device.HOSTS_INFO] = 1  # says channel 1, CHANGE_HOST said 0
                rig = self.make(kb)
                self.assertEqual(control(rig, "platform"), {"value": None, "writable": False, "busy": False, "outcome": None})
                sent = len(rig.port.written)
                self.ask("platform", 2)
                self.assertEqual(rig.port.written[sent:], [])
                self.assertEqual(kb.setting_writes, [])
                rig.stop()

    def test_what_does_not_fit_is_unknown_and_never_written(self):  # OS03, OS05, OS10
        cases = {"undefined platform": multi(current=0xFF), "too many descriptors": multi(count=17),
                 "no descriptors": multi(count=0), "bad echo": {"info": info(), "descriptors": [descriptor(0, 3, 0x100)] * 4},
                 "refused descriptor": multi()}
        for name, platform in cases.items():
            with self.subTest(name):
                kb = settings_keyboard(platform=platform)
                if name == "refused descriptor":
                    kb.failing[(MP, 1)] = 5
                rig = self.make(kb)
                self.assertEqual(control(rig, "platform"), {"value": None, "writable": False, "busy": False, "outcome": None})
                if name == "too many descriptors":
                    self.assertEqual(packets(rig.port, kb, MP, 1), [])
                sent = len(rig.port.written)
                self.ask("platform", 0)
                self.assertEqual(rig.port.written[sent:], [])
                rig.stop()
        rig = self.make()
        self.dev.settings[MP]["descriptors"][2] = descriptor(1, 2, 0)  # gone bad before the change
        self.ask("platform", 2)
        self.assertEqual(packets(rig.port, self.dev, MP, 3), [])
        self.assertEqual(control(rig, "platform"), {"value": None, "writable": False, "busy": False, "outcome": "unknown"})

    def test_ignored_restored_and_unconfirmed(self):  # OS09
        rig = self.make()
        self.dev.ignore_writes = True
        self.ask("platform", 2)
        self.assertEqual(self.setters(MP), [b"\x00\x02"])
        self.assertEqual(control(rig, "platform")["outcome"], "not-applied")
        rig.stop()
        for taken, outcome, last in (([0], "restored", 1), ([0, 0], "unconfirmed", 0)):
            with self.subTest(outcome=outcome):
                kb = settings_keyboard(platform=multi())
                kb.taken[MP] = list(taken)
                rig = self.make(kb)
                self.ask("platform", 2)
                self.assertEqual(self.setters(MP), [b"\x00\x02", b"\x00\x01"])
                self.assertEqual(control(rig, "platform"), dict(control(rig, "platform"), value=last, outcome=outcome))
                rig.stop()
        kb = MuteAfter(controls=(), hosts=(3, 0), settings={device.HOSTS_INFO: 0, MP: multi()})
        kb.mute = (MP, 3)
        rig = self.make(kb)
        self.ask("platform", 2)
        self.assertEqual(len(packets(rig.port, kb, MP, 3)), 1)
        self.assertEqual(control(rig, "platform"), {"value": None, "writable": False, "busy": False, "outcome": "unconfirmed"})

    def test_a_report_is_read_not_believed(self):  # OS11
        rig = self.make()
        reads = len(packets(rig.port, self.dev, MP, 0))
        self.dev.settings[MP]["info"] = info(current=0)  # the keyboard's own shortcut
        rig.note(1, self.dev, MP, b"\x00\x02")
        rig.settle()
        self.assertEqual(len(packets(rig.port, self.dev, MP, 0)), reads + 1)
        self.assertEqual(control(rig, "platform")["value"], 0)
        self.assertEqual(self.dev.setting_writes, [])

    def test_dual_platform_is_shown_and_never_written(self):  # DP01, DP02
        rig = self.make(settings_keyboard(dual=[1]))
        self.assertEqual(control(rig, "platform"), {"value": 1, "choices": [dict(c, os=list(c["os"])) for c in kbsettings.DUAL_CHOICES],
                                                    "writable": False, "busy": False, "outcome": None})
        self.ask("platform", 0)
        self.assertEqual(packets(rig.port, self.dev, DP, 1) + packets(rig.port, self.dev, DP, 2), [])
        reads = len(packets(rig.port, self.dev, DP, 0))
        rig.note(1, self.dev, DP, b"\x00")
        rig.settle()
        self.assertEqual(len(packets(rig.port, self.dev, DP, 0)), reads + 1)
        self.assertEqual(self.dev.setting_writes, [])
        rig.stop()
        rig = self.make(settings_keyboard(platform=multi(), dual=[0]))
        self.assertEqual(packets(rig.port, self.dev, DP, 0), [])  # never asked when 0x4531 is listed
        self.ask("platform", 2)
        self.assertEqual(self.setters(MP), [b"\x00\x02"])
        self.assertEqual(packets(rig.port, self.dev, DP, 0) + packets(rig.port, self.dev, DP, 1), [])

    def test_the_mouse_is_heard_while_many_descriptors_are_slow(self):  # OS12
        rows = [descriptor(i % 3, i, 0x0100 << (i % 3)) for i in range(16)]
        rig = self.make(settings_keyboard(platform={"info": info(count=16), "descriptors": rows}))
        mouse = rig.port.devices[2]
        rig.engine.cfg["buttons"]["back"] = actions.keys("LEFTSHIFT")
        self.dev.delays = {MP: 0.03, device.HOSTS_INFO: 0.03}
        self.ask("platform", 2, settle=False)
        for _ in range(3):
            time.sleep(0.05)
            self.assertIsNotNone(rig.kb._asked)
            rig.mouse_actions.ran.clear()
            start = time.monotonic()
            rig.port.notify(bytes([0x11, 2, mouse.index(device.REPROG_CONTROLS_V4), 0]) + struct.pack("!HHHH", 0x0053, 0, 0, 0).ljust(16, b"\0"))
            while not rig.mouse_actions.ran:
                rig.engine._handle(rig.engine._queue.get(timeout=1.0))
            self.assertLess(time.monotonic() - start, 0.1)
            rig.port.notify(bytes([0x11, 2, mouse.index(device.REPROG_CONTROLS_V4), 0]) + bytes(16))
        rig.settle()
        self.assertEqual(self.setters(MP), [b"\x00\x02"])


class SettingsCommon(SettingsBase):
    """What holds for every setting holds for these two: the guard, the wishes, the keys, going and staying."""

    CHANGES = (("disable", {"caps_lock": True}, DisableCodec, DK), ("platform", 2, PlatformCodec, MP))

    def rivals(self, *names):
        self.rig.rivals = list(names)
        self.rig.engine._look_for_rivals(time.monotonic())

    def test_never_where_no_key_may_be_changed(self):  # GX04 (F10 guard)
        for kwargs, reason in ((dict(rivals=["Solaar"]), "rivals"), (dict(journal="not a journal"), "journal")):
            with self.subTest(reason):
                rig = self.make(**kwargs)
                self.assertEqual(rig.state()["reason"], reason)
                for name, value, _codec, _feature in self.CHANGES:
                    self.assertFalse(control(rig, name)["writable"])
                    self.assertIsNotNone(control(rig, name)["value"])  # read all the same
                    rig.engine.set_keyboard_setting(name, value)
                    rig.engine._handle(rig.engine._queue.get(timeout=1.0))
                rig.settle()
                self.assertEqual(rig.kb._pending, {})
                self.assertEqual(self.dev.setting_writes, [])
                rig.stop()
        rig = self.make(settings_keyboard(platform=multi(), info=False))
        self.assertEqual(rig.state()["reason"], "identity")
        self.ask("disable", {"caps_lock": True})
        self.ask("platform", 2)
        self.assertEqual(self.dev.setting_writes, [])

    def test_other_software_seen_before_the_setter_sends_nothing(self):  # GX04
        for name, value, codec, feature in self.CHANGES:
            with self.subTest(name):
                rig = self.make()
                entered, release = self.hold(lambda request, codec=codec: request[0] is codec.read)
                self.ask(name, value, settle=False)
                self.assertTrue(self.reached(entered))
                self.rivals("Solaar")
                self.assertEqual(rig.kb._pending, {})
                release.set()
                rig.settle()
                self.rivals()
                rig.settle()
                self.assertEqual(self.dev.setting_writes, [])
                self.assertEqual(packets(rig.port, self.dev, feature, 3 if feature == MP else 2), [])
                rig.stop()

    def test_a_setter_waiting_is_called_off_once_other_software_is_seen(self):  # GX04 (admission)
        for name, value, codec, feature in self.CHANGES:
            with self.subTest(name):
                rig = self.make()
                mouse = rig.port.devices[2]
                entered, release = self.hold(lambda request, codec=codec: request[0] is codec.write)
                self.ask(name, value, settle=False)
                self.assertTrue(self.reached(entered))
                mouse.delay = 0.5
                foreground = threading.Thread(target=rig.transport().request, args=(2, 0, 1, bytes([0, 0, 1])))
                foreground.start()
                time.sleep(0.05)
                release.set()
                time.sleep(0.1)
                self.assertIs(rig.kb._asked[0], codec.write)
                self.rivals("Solaar")
                foreground.join()
                rig.settle()
                self.assertEqual(self.dev.setting_writes, [])
                self.assertEqual(control(rig, name), dict(control(rig, name), writable=False, outcome="unconfirmed"))
                rig.stop()

    def test_other_software_after_the_setter_nothing_is_sent_back(self):  # GX04 (restore guard)
        kb = settings_keyboard(disable=(0x1F, 0x00), platform=multi())
        kb.taken[DK] = [0x03]
        rig = self.make(kb)
        entered, release = self.hold(lambda request: request[0] is DisableCodec.read and kb.setting_writes)
        self.ask("disable", {"caps_lock": True}, settle=False)
        self.assertTrue(self.reached(entered))
        self.rivals("Solaar")
        release.set()
        rig.settle()
        self.assertEqual(self.setters(DK), [b"\x01"])  # never the earlier set sent back
        self.assertEqual(control(rig, "disable"), dict(control(rig, "disable"), value=["caps_lock", "num_lock"],
                                                       writable=False, outcome="unconfirmed"))

    def test_the_earlier_value_goes_back_only_where_the_read_back_lets_it(self):  # restore admission
        def unsettable(kb):
            kb.settings[MP]["info"] = bytes([0]) + kb.settings[MP]["info"][1:]  # no longer settable by software

        def layout_gone(kb):
            kb.settings[MP]["descriptors"][2] = descriptor(0, 2, 0x6000)

        def layout_two_gone(kb):
            kb.settings[MP]["descriptors"][3] = descriptor(0, 3, 0x0400)

        def super_gone(kb):
            kb.settings[DK]["caps"] = 0x0F

        def bit_0x20_gone(kb):
            kb.settings[DK]["caps"] = 0x9F

        cases = (  # (disable bits, taken, change after each setter, name, asked, setters sent, value then, outcome)
            ((0x1F, 0x00), [0], unsettable, "platform", 2, [b"\x00\x02"], 0, "unconfirmed"),
            ((0x1F, 0x00), [0], layout_gone, "platform", 2, [b"\x00\x02"], 0, "unconfirmed"),
            ((0x1F, 0x10), [0x01], super_gone, "disable", {"caps_lock": True}, [b"\x11"], ["caps_lock"], "unconfirmed"),
            ((0x1F, 0x00), [0], layout_two_gone, "platform", 2, [b"\x00\x02", b"\x00\x01"], 1, "restored"),
            ((0xBF, 0x81), [0x03], bit_0x20_gone, "disable", {"num_lock": True}, [b"\x83", b"\x81"], ["caps_lock"],
             "restored"),  # the unnamed bit 0x80 goes back with it, as the keyboard still has it
        )
        for disable, taken, change, name, asked, setters, value, outcome in cases:
            with self.subTest(change.__name__):
                kb = settings_keyboard(disable=disable, platform=multi())
                feature, setter = (MP, 3) if name == "platform" else (DK, 2)
                kb.taken[feature] = list(taken)

                def changing(feat_idx, function, params, kb=kb, answer=kb.answer, sets=(feature, setter), change=change):
                    reply = answer(feat_idx, function, params)
                    if feat_idx < len(kb.features) and (kb.features[feat_idx], function) == sets:
                        change(kb)
                    return reply

                kb.answer = changing
                rig = self.make(kb)
                self.ask(name, asked)
                self.assertEqual(self.setters(feature), setters)
                self.assertEqual(control(rig, name), dict(control(rig, name), value=value, outcome=outcome))
                rig.stop()

    def test_a_wish_calls_the_change_off_and_it_follows_merged(self):  # GX08
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is DisableCodec.read)
        self.ask("disable", {"caps_lock": True}, settle=False)
        self.assertTrue(self.reached(entered))
        self.ask("disable", {"num_lock": True}, settle=False)
        new = copy.deepcopy(rig.engine.cfg)
        new["keyboard"]["keys"] = {"0x0f01": COPY}
        rig.engine._apply_config(new)
        self.assertEqual(rig.kb._task.__name__, "_configure")
        self.assertEqual(rig.kb._pending, {"disable": {"caps_lock": True, "num_lock": True}})
        release.set()
        rig.settle()
        self.assertEqual(self.setters(DK), [b"\x03"])
        self.assertTrue(rig.key(0x0F01)["active"])
        rig.stop()
        rig = self.make()
        entered, release = self.hold(lambda request: request[0] is PlatformCodec.read)
        self.ask("platform", 2, settle=False)
        self.assertTrue(self.reached(entered))
        rig.kb.device_action({"type": "host", "host": 1}, "press")  # Easy-Switch goes first
        self.assertEqual(rig.kb._pending, {"platform": 2})
        release.set()
        rig.settle()
        self.assertEqual(self.dev.switched, [1])
        self.assertEqual(self.setters(MP), [])  # not while the keyboard may be on its way

    def test_a_change_waits_while_a_key_is_down(self):  # GX05
        rig = self.make(keys={"0x0f01": COPY})
        rig.keys_down(1, self.dev, 0x0F01)
        self.ask("disable", {"super": True})
        self.ask("platform", 0)
        self.assertEqual(self.dev.setting_writes, [])
        self.assertEqual(rig.kb._pending, {"disable": {"super": True}, "platform": 0})
        rig.keys_down(1, self.dev)
        rig.settle()
        self.assertEqual(self.dev.setting_writes, [(DK, b"\x10"), (MP, b"\x00\x00")])

    def test_gone_replaced_or_exiting_never_sends_or_sends_back(self):  # GX03
        for name, value, codec, feature in self.CHANGES:
            with self.subTest(name, when="before the setter"):
                rig = self.make()
                entered, release = self.hold(lambda request, codec=codec: request[0] is codec.read)
                self.ask(name, value, settle=False)
                self.assertTrue(self.reached(entered))
                rig.link(1, False)
                release.set()
                self.drain()
                rig.link(1, True)
                rig.settle()
                self.assertEqual(self.dev.setting_writes, [])
                rig.stop()
            with self.subTest(name, when="after the setter"):
                kb = settings_keyboard(platform=multi())
                kb.taken.update({DK: [0x03], MP: [0]})
                rig = self.make(kb)
                entered, release = self.hold(lambda request, codec=codec: request[0] is codec.read and kb.setting_writes)
                self.ask(name, value, settle=False)
                self.assertTrue(self.reached(entered))
                rig.link(1, False)
                release.set()
                self.drain()
                self.assertEqual(len(kb.setting_writes), 1)  # nothing sent back
                rig.stop()
            with self.subTest(name, when="another unit"):
                rig = self.make(keys={"0x0f01": COPY})
                rig.keys_down(1, self.dev, 0x0F01)
                self.ask(name, value)
                rig.link(1, False)
                other = rig.port.devices[1] = settings_keyboard(platform=multi(), unit=UNIT_B)
                rig.link(1, True)
                rig.settle()
                rig.keys_down(1, other)
                rig.settle()
                self.assertEqual(other.setting_writes, [])
                rig.stop()
            with self.subTest(name, when="exit"):
                rig = self.make(keys={"0x0f01": COPY})
                rig.keys_down(1, self.dev, 0x0F01)
                self.ask(name, value)
                start = time.monotonic()
                rig.kb.shutdown()
                self.assertLess(time.monotonic() - start, keyboard.SHUTDOWN_BUDGET)
                self.assertEqual(self.dev.setting_writes, [])
                rig.stop()

    def test_nothing_is_put_back_on_finding_reconnecting_or_new_settings(self):  # GX02
        with mock.patch.object(config, "save", side_effect=AssertionError("saved")), \
                mock.patch.object(config, "write_durable", side_effect=AssertionError("written")):
            rig = self.make()
            before, exported = copy.deepcopy(rig.engine.cfg), config.export_data(rig.engine.cfg)
            self.ask("disable", {"insert": True})
            self.ask("platform", 2)
            writes = list(self.dev.setting_writes)
            self.dev.settings[DK]["current"] = 0
            self.dev.settings[MP]["info"] = info(current=1)
            rig.link(1, False)
            rig.link(1, True)
            rig.settle()
            new = copy.deepcopy(rig.engine.cfg)
            new["keyboard"]["keys"] = {"0x0f01": COPY}
            rig.engine._apply_config(new)
            rig.settle()
            self.assertEqual(self.dev.setting_writes, writes)
            self.assertEqual(control(rig, "disable")["value"], [])  # what the keyboard says, not what was asked
            self.assertEqual(control(rig, "platform")["value"], 1)
            self.assertEqual(config.export_data(before), exported)
            self.assertNotIn("disable", json.dumps(rig.engine.cfg))
            self.assertNotIn("platform", json.dumps(rig.engine.cfg))

    def test_state_is_plain_json_and_tells_no_bits_slots_or_unit(self):  # GX06, GX07
        def plain(value):
            if isinstance(value, dict):
                return all(type(k) is str and plain(v) for k, v in value.items())
            if isinstance(value, list):
                return all(plain(v) for v in value)
            return value is None or type(value) in (str, int, bool)
        with self.assertLogs("mxoptions", logging.DEBUG) as logs:
            kb = settings_keyboard(disable=(0x9F, 0x81), platform=multi(slot=2), current=2)
            rig = self.make(kb)
            self.ask("disable", {"num_lock": True})
            kb.ignore_writes = True
            self.ask("platform", 2)
            kb.settings[DK]["current"] = 0x40
            self.ask("disable", {"insert": True})
            rig.kb.set_setting("platform", "x")
        controls = rig.state()["controls"]
        self.assertTrue(plain(controls))
        self.assertEqual(json.loads(json.dumps(controls)), controls)
        self.assertEqual(set(controls["disable"]), {"value", "writable", "busy", "outcome"})
        self.assertEqual(set(controls["platform"]), {"value", "choices", "writable", "busy", "outcome"})
        state, shown = json.dumps(rig.state()), json.dumps(controls)
        for private in (identity(UNIT_A), UNIT_A.hex(), "5e015e02", rig.kb.link):
            self.assertNotIn(private, state)
        for private in ("caps", "slot", "host", "\\x", "mask", "129", "131", "159"):  # 0x81, 0x83, 0x9f
            self.assertNotIn(private, shown)
        lines = [line for line in logs.output if "setting" in line]
        self.assertTrue(lines)
        for line in lines:
            self.assertNotRegex(line, r"\d|\\x|identity|mask")
        self.assertIn("keyboard setting platform: the keyboard did not take the change", "\n".join(lines))


class SettingsPage(unittest.TestCase):
    """The keys to turn off and the layout on the Keyboard page (Qt offscreen)."""

    def setUp(self):
        Page.setUp(self)
        self.disable_row, self.platform_row = self.page.settings_rows["disable"], self.page.settings_rows["platform"]
        self.box = self.page.platform_box

    def show(self, disable=None, platform=None, reason=None, connected=True):
        generic = {"writable": True, "busy": False, "outcome": None}
        controls = {}
        if disable is not None:
            controls["disable"] = dict(generic, **dict({"value": ["insert"], "keys": ALL_KEYS}, **disable))
        if platform is not None:
            controls["platform"] = dict(generic, **dict({"value": 1, "choices": CHOICES}, **platform))
        found = {"connected": connected, "known": True, "name": "Synthetic keyboard", "battery": None, "current_host": 0,
                 "hosts": [], "keys": [], "reason": reason, "failed": None, "firmware": None,
                 "settings": ["platform", "disable"], "controls": controls}
        self.store.update_state(dict(self.store.state, keyboard=found))

    def shown_keys(self):
        return [key for key, row in self.page.disable_rows.items() if not row.isHidden()]

    def test_a_row_for_each_key_it_can_turn_off(self):  # UI01
        self.show()
        self.assertEqual(self.shown_keys(), [])
        self.assertIn("MX Options does not", self.disable_row.note.text())
        self.assertTrue(self.box.isHidden())
        self.show({"keys": ["caps_lock", "scroll_lock", "super"], "value": ["super"]}, {})
        self.assertEqual(self.shown_keys(), ["caps_lock", "scroll_lock", "super"])
        self.assertEqual([key for key, switch in self.page.disable_switches.items() if switch.isChecked()], ["super"])
        self.assertEqual(self.disable_row.note.text(), self.pages.DISABLE_NOTE)
        self.assertEqual(self.disable_row.info.text(), self.pages.DISABLE_HINT)
        self.show({"value": None, "writable": False})
        self.store.state["keyboard"]["controls"]["disable"].pop("keys")  # as a failed read is published
        self.store.update_state(copy.deepcopy(self.store.state))
        self.assertEqual(self.shown_keys(), [])
        self.assertEqual(self.disable_row.note.text(), "The keyboard did not say how this is set.")
        self.show({}, {}, connected=False)
        self.assertEqual(self.shown_keys(), [])
        self.assertTrue(self.box.isHidden())
        self.assertEqual(self.calls, [])

    def test_layouts_by_their_systems_numbered_where_alike(self):  # UI02
        self.show(None, {})
        self.assertFalse(self.box.isHidden())
        self.assertEqual([self.box.itemText(i) for i in range(self.box.count())], ["Windows, Android", "macOS, iOS", "Linux"])
        self.assertEqual(self.box.currentIndex(), 1)
        self.assertIs(self.platform_row.title.buddy(), self.box)
        self.assertEqual(self.platform_row.info.text(), self.pages.PLATFORM_HINT)
        self.assertEqual(self.pages.layout_labels([{"index": 0, "os": []}, {"index": 4, "os": ["linux"]},
                                                   {"index": 6, "os": ["linux"]}, {"index": 7, "os": ["webos", "tizen"]}]),
                         ["Layout 1", "Linux · Layout 5", "Linux · Layout 7", "webOS, Tizen"])
        self.show(None, {"value": None, "writable": False, "choices": []})
        self.assertEqual(self.box.count(), 0)
        self.assertFalse(self.box.isEnabled())

    def test_disabled_while_unknown_not_writable_busy_or_held(self):  # UI03
        for change in ({"writable": False}, {"busy": True}, {"value": None, "writable": False}):
            with self.subTest(change=change):
                self.show(change, change)
                self.assertFalse(any(switch.isEnabled() for switch in self.page.disable_switches.values()))
                self.assertFalse(self.box.isEnabled())
        self.show({"writable": False}, {"writable": False})
        self.assertEqual(self.platform_row.note.text(), "The keyboard does not let MX Options change this.")
        self.show({"writable": False}, {"writable": False}, reason="rivals")
        self.assertEqual(self.platform_row.note.text(), "")  # the banner says why
        self.show({}, {})
        self.assertTrue(all(switch.isEnabled() for switch in self.page.disable_switches.values()))
        self.assertTrue(self.box.isEnabled())
        self.assertEqual(self.calls, [])

    def test_one_engine_call_per_toggle_or_pick_none_on_refresh(self):  # UI04
        self.show({}, {})
        self.page.disable_switches["caps_lock"].click()
        self.assertEqual(self.calls, [("disable", {"caps_lock": True})])
        self.show({"busy": True}, {})
        self.assertTrue(self.page.disable_switches["caps_lock"].isChecked())  # as asked, until the keyboard answers
        self.show({"value": ["insert", "caps_lock"]}, {})
        self.page.disable_switches["insert"].click()
        self.assertEqual(self.calls[-1], ("disable", {"insert": False}))
        self.show({"value": ["caps_lock"]}, {"value": 2})
        self.assertEqual(self.box.currentIndex(), 2)
        self.box.activated.emit(0)
        self.assertEqual(self.calls[-1], ("platform", 0))
        self.box.activated.emit(2)  # the one it has: nothing to ask
        self.show({"value": []}, {"value": 0})
        self.assertEqual(self.calls, [("disable", {"caps_lock": True}), ("disable", {"insert": False}), ("platform", 0)])

    def test_every_outcome_has_its_note(self):  # UI05
        for outcome, text in self.pages.OUTCOMES.items():
            with self.subTest(outcome):
                self.show({"outcome": outcome}, {"outcome": outcome})
                self.assertEqual(self.disable_row.note.text(), text)
                self.assertEqual(self.platform_row.note.text(), text)

    def test_names_for_assistive_technology_and_words_translated(self):  # UI06
        self.show({}, {})
        self.assertEqual([switch.accessibleName() for switch in self.page.disable_switches.values()],
                         ["Turn off the Caps Lock key", "Turn off the Num Lock key", "Turn off the Scroll Lock key",
                          "Turn off the Insert key", "Turn off the Windows/Super key"])
        self.assertEqual(self.box.accessibleName(), "Operating system layout")
        from mxoptions import i18n_uk
        for text in (self.pages.DISABLE_NOTE, self.pages.DISABLE_HINT, self.pages.PLATFORM_HINT, "Turn off the {} key",
                     "Layout {}", "Windows/Super key"):
            self.assertIn(text, i18n_uk.STRINGS)


if __name__ == "__main__":
    unittest.main()
