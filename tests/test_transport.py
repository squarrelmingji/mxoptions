"""One request at a time on a transport, and requests in the background that give way: the real
hidpp.Transport on a FakePort, with synthetic devices that answer at once, late or never."""

import threading
import time
import unittest

from tests.support import FakeDevice, FakePort, open_fake
from mxoptions import device, hidpp
from mxoptions.hidpp import HidppCancelled, HidppClosed

MARGIN = 0.15


class Admission(unittest.TestCase):
    def setUp(self):
        self.mouse = FakeDevice("Synthetic mouse", kind=device.KIND_MOUSE)
        self.keys = FakeDevice()
        self.port = FakePort({1: self.keys, 2: self.mouse})
        self.transport = open_fake(self.port)
        self.addCleanup(self.transport.close)

    def background(self, devidx=1, timeout=1.0, deadline=None, cancel=None, results=None):
        """Start a background ping on its own thread; its outcome lands in ``results``."""
        results = results if results is not None else []

        def run():
            try:
                results.append(self.transport.request(devidx, 0, 1, b"\0\0\x01", timeout=timeout, background=True,
                                                      deadline=deadline, cancel=cancel))
            except Exception as error:  # noqa: BLE001 - kept for the test to look at
                results.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, results

    def test_alone_a_request_works_as_before(self):
        self.assertEqual(self.transport.ping(2), (4, 2))
        self.assertIsNone(self.transport.ping(5))  # empty slot: the receiver says so at once
        self.assertEqual(len(self.port.sent(5)), 1)

    def test_a_silent_keyboard_holds_the_mouse_up_by_one_short_answer_at_most(self):
        """The look for a keyboard that never answers goes on and on; the mouse is served in between."""
        self.keys.silent = True
        cancel = threading.Event()
        view = hidpp.ProbeTransport(self.transport, 0.2, cancel, time.monotonic() + 30)

        def keep_asking():
            while not cancel.is_set():
                try:
                    view.ping(1)
                except (HidppCancelled, HidppClosed):
                    return
        thread = threading.Thread(target=keep_asking, daemon=True)
        thread.start()
        try:
            worst = 0.0
            for _ in range(8):
                time.sleep(0.05)
                start = time.monotonic()
                self.assertEqual(self.transport.ping(2), (4, 2))
                worst = max(worst, time.monotonic() - start)
            self.assertLess(worst, 0.2 + 0.1)
        finally:
            cancel.set()
            thread.join(1.0)
        self.assertFalse(thread.is_alive())

    def test_the_foreground_goes_before_a_waiting_background_request(self):
        self.mouse.delay = 0.3
        first = threading.Thread(target=lambda: self.transport.ping(2), daemon=True)
        first.start()
        time.sleep(0.05)
        thread, results = self.background()
        time.sleep(0.05)
        self.transport.ping(2)  # waits for the first, then goes before the background request
        thread.join(2.0)
        order = [packet[1] for packet in self.port.written]
        self.assertEqual(order, [2, 2, 1])
        self.assertEqual(results[0][:2], bytes([4, 2]))

    def test_many_background_requests_never_crowd_the_foreground(self):
        """At most one background request is ever ahead of the mouse, and no two are on the way at once."""
        self.keys.delay = 0.1
        threads = [self.background()[0] for _ in range(3)]
        time.sleep(0.02)
        start = time.monotonic()
        self.transport.ping(2)
        self.assertLess(time.monotonic() - start, 0.1 + MARGIN)
        for thread in threads:
            thread.join(2.0)
        self.assertEqual(self.port.most_in_flight, 1)
        self.assertEqual(len(self.port.sent(1)), 3)

    def test_a_background_request_gives_up_at_its_deadline_unsent(self):
        self.mouse.delay = 0.6
        holder = threading.Thread(target=lambda: self.transport.ping(2), daemon=True)
        holder.start()
        time.sleep(0.05)
        start = time.monotonic()
        thread, results = self.background(deadline=start + 0.2)
        thread.join(2.0)
        self.assertIsInstance(results[0], HidppCancelled)
        self.assertAlmostEqual(time.monotonic() - start, 0.2, delta=MARGIN)
        self.assertEqual(self.port.sent(1), [])
        holder.join(2.0)

    def test_a_background_request_called_off_while_waiting_is_never_sent(self):
        self.mouse.delay = 0.3
        holder = threading.Thread(target=lambda: self.transport.ping(2), daemon=True)
        holder.start()
        time.sleep(0.05)
        cancel = threading.Event()
        thread, results = self.background(cancel=cancel)
        cancel.set()
        holder.join(2.0)
        thread.join(2.0)
        self.assertIsInstance(results[0], HidppCancelled)
        self.assertEqual(self.port.sent(1), [])

    def test_a_failed_write_frees_the_transport(self):
        for background in (False, True):
            with self.subTest(background=background):
                self.port.fail_write = True
                with self.assertRaises(HidppClosed):
                    self.transport.request(2, 0, 1, b"\0\0\x01", background=background)
                self.assertEqual(self.transport.ping(2), (4, 2))
                thread, results = self.background(devidx=2)
                thread.join(2.0)
                self.assertEqual(results[0][:2], bytes([4, 2]))

    def test_closing_wakes_everyone_waiting(self):
        self.mouse.silent = True
        errors = []

        def foreground():
            try:
                self.transport.request(2, 0, 1, b"\0\0\x01", timeout=5.0)
            except HidppClosed as error:
                errors.append(error)
        holder = threading.Thread(target=foreground, daemon=True)
        holder.start()
        time.sleep(0.05)
        waiter = threading.Thread(target=foreground, daemon=True)
        waiter.start()
        thread, results = self.background(deadline=time.monotonic() + 10)
        time.sleep(0.05)
        start = time.monotonic()
        self.transport.close()
        for each in (holder, waiter, thread):
            each.join(2.0)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(len(errors), 2)
        self.assertIsInstance(results[0], HidppClosed)

    def test_a_background_answer_is_not_waited_for_past_the_deadline(self):
        self.keys.silent = True
        start = time.monotonic()
        thread, results = self.background(timeout=5.0, deadline=start + 0.2)
        thread.join(2.0)
        self.assertIsInstance(results[0], HidppCancelled)
        self.assertAlmostEqual(time.monotonic() - start, 0.2, delta=MARGIN)
        self.assertEqual(len(self.port.sent(1)), 1)


class Reports(unittest.TestCase):
    def test_what_a_keyboard_types_is_neither_passed_on_nor_logged(self):
        """A Bluetooth keyboard sends its keystrokes on the node its HID++ traffic uses."""
        port = FakePort({0xFF: FakeDevice()}, receiver=False)
        heard = []
        transport = open_fake(port, receiver=False, on_notification=lambda *args: heard.append(args))
        self.addCleanup(transport.close)
        with self.assertNoLogs(level="DEBUG"):
            port.notify(bytes([0x01, 0x00, 0x00, 0x0B, 0x0C, 0x0D, 0x00, 0x00, 0x00]))  # synthetic keystrokes
            self.assertEqual(transport.ping(0xFF), (4, 2))
        self.assertEqual(heard, [])
        self.assertIsNone(transport.on_report)


class ProbeSlot(unittest.TestCase):
    def test_what_each_slot_holds(self):
        """Only a keyboard (kind 0) is taken for one; numpads, presenters and mice are something else."""
        devices = {1: FakeDevice(), 2: FakeDevice("Synthetic mouse", kind=device.KIND_MOUSE),
                   3: FakeDevice("Synthetic numpad", kind=2), 4: FakeDevice("Synthetic presenter", kind=6),
                   5: FakeDevice("Synthetic trackball", kind=device.KIND_TRACKBALL)}
        asleep = devices[4]
        port = FakePort(devices)
        transport = open_fake(port)
        self.addCleanup(transport.close)
        found = {devidx: device.probe_slot(transport, devidx)[0] for devidx in transport.slots()}
        self.assertEqual(found, {1: "keyboard", 2: "mouse", 3: "other", 4: "other", 5: "mouse", 6: "empty"})
        asleep.silent = True
        self.assertEqual(device.probe_slot(transport, 4), ("asleep", None))
        old = FakeDevice()
        old.answer = lambda feat_idx, function, params: bytes([1, 0, 0])  # a HID++ 1.0 era protocol version
        port.devices[3] = old
        self.assertEqual(device.probe_slot(transport, 3), ("old", None))

    def test_the_mouse_is_looked_for_exactly_as_before(self):
        """The new arguments of find_mouse change nothing in what is sent; keyboards come out of ``others``."""
        sequences = []
        for kwargs in ({}, {"others": [], "skip": ()}):
            port = FakePort({1: FakeDevice(), 2: FakeDevice("Synthetic mouse", kind=device.KIND_MOUSE)})
            transport = open_fake(port)
            try:
                found = device.find_mouse(transport, **kwargs)
            finally:
                transport.close()
            self.assertEqual((found.devidx, found.name), (2, "Synthetic mouse"))
            # the ping byte is the time of day: everything else is compared
            sequences.append([p[:6] + p[7:] if p[2] == 0 and p[3] >> 4 == 1 else p for p in port.written])
            if kwargs:
                self.assertEqual([(dev.devidx, dev.kind) for dev in kwargs["others"]], [(1, device.KIND_KEYBOARD)])
        self.assertEqual(sequences[0], sequences[1])

    def test_skipped_slots_are_not_asked(self):
        port = FakePort({1: FakeDevice()})
        transport = open_fake(port)
        self.addCleanup(transport.close)
        self.assertIsNone(device.find_mouse(transport, skip=(1,)))
        self.assertEqual(port.sent(1), [])


if __name__ == "__main__":
    unittest.main()
