"""Flow as the program runs it: two or three services on this machine pair by code, agree on their
sides, hand the mouse over at the edge and carry what was copied. The mouse, the desktop and the
clipboard are stand-ins; the network between the two is real, on the loopback interface."""

import copy
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import tests.support  # noqa: E402,F401
from tests.test_flow import free_port  # noqa: E402
from mxoptions import clipboard, config, flow, flow_edges, flow_files, flow_service, vinput  # noqa: E402
from mxoptions.ui.store import Store  # noqa: E402

APP = QGuiApplication.instance() or QApplication([])  # the Flow page is drawn too
HOSTS = [{"paired": True, "name": "desk"}, {"paired": True, "name": "laptop"}, {"paired": False, "name": ""}]


class Engine:
    """Stands in for the engine: remembers what Flow asked of it."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, *args))

    def asked(self, name):
        return [call[1:] for call in self.calls if call[0] == name]


class Clipboard:
    """One computer's clipboard."""

    PNG = clipboard.PNG
    signature = staticmethod(clipboard.signature)

    def __init__(self):
        self.content = {"files": [], "image": None, "text": ""}
        self.written = []

    def rich(self):
        return True

    def available(self):
        return True

    def read(self, image_limit=None):
        return dict(self.content)

    def write(self, text=None, image=None, files=None):
        self.written.append({"text": text, "image": image, "files": files})
        self.content = {"files": list(files or []), "image": image, "text": text or ""}
        return True


class Pointer:
    def __init__(self):
        self.moves = []

    def move(self, point, desktop):
        self.moves.append((point, desktop))

    def close(self):
        pass


class Computer:
    def __init__(self, port):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg["flow"]["port"] = port
        self.engine = Engine()
        self.store = Store(cfg, self.engine)
        self.store.flush = lambda: None  # these settings are made up: never write them anywhere
        self.store._save_timer.timeout.disconnect()
        self.flow = flow_service.FlowService(self.store)
        self.flow.clipboard = self.clipboard = Clipboard()

    @property
    def cfg(self):
        return self.store.cfg["flow"]

    def entry(self, index=0):
        """What the settings hold about one of the other computers."""
        return self.cfg["peers"][index]

    def peer(self, index=0):
        """What the service says about one of the other computers ({} if there is none)."""
        peers = self.flow.status()["peers"]
        return peers[index] if len(peers) > index else {}

    def pair_of(self, index=0):
        return flow.pair_id(self.entry(index)["key"])

    def linked(self, count=1):
        return sum(1 for peer in self.flow.status()["peers"] if peer["linked"]) >= count

    def vibrated(self):
        return [call[0] for call in self.engine.asked("play_haptic")]

    def mouse(self, connected, channel=0):
        self.store.update_state(dict(self.store.state, connected=connected, current_host=channel, hosts=HOSTS if connected else []))

    def at_edge(self, edge, along, full=False):
        """What the edge watcher says when the pointer comes to rest ``along`` (0..1) the edge of a 1000x1000 screen."""
        x, y = {"right": (999, along * 999), "left": (0, along * 999), "top": (along * 999, 0), "bottom": (along * 999, 999)}[edge]
        self.flow._edge_report({"at": True, "edge": edge, "x": x, "y": y, "screen": [0, 0, 1000, 1000], "full": full})


def wait(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def pair_them(test, host, guest, peers_of_host=1):
    """Pair ``guest`` with ``host`` by code, as their settings pages would, and wait for the link."""
    guest.flow.watch_offers(True)
    host.flow.start_pairing()
    test.assertTrue(wait(lambda: guest.flow.status()["offers"]), "the offer was never seen")
    offer = guest.flow.status()["offers"][0]
    guest.flow.pair(offer["address"], offer["port"], host.flow.status()["pairing"]["code"])
    test.assertTrue(wait(lambda: host.linked(peers_of_host) and guest.linked()), "no link after pairing")
    guest.flow.watch_offers(False)


@unittest.skipUnless(flow.supported(), "this Python has no pre-shared keys for TLS")
@unittest.skipIf(sys.platform == "win32", "Windows does not carry a broadcast between two programs on one machine, which is how they find each other here")
class Services(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="mxoptions-test-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        changes = [(flow, "RETRY", 0.2), (flow, "HERE_INTERVAL", 0.2), (flow, "OFFER_INTERVAL", 0.1),
                   (flow, "DISCOVERY_PORT", free_port()), (flow, "PAIR_PORT", free_port()),
                   (flow, "broadcast_addresses", lambda: ["127.255.255.255"]),
                   (flow_files, "STAGING", os.path.join(self.folder, "arrived")),
                   (flow_edges, "create", lambda *args, **more: None),  # stay off the session bus and the desktop
                   (vinput, "create_pointer", Pointer)]
        for module, name, value in changes:
            self.addCleanup(setattr, module, name, getattr(module, name))
            setattr(module, name, value)
        self.desk, self.laptop = self.computer(), self.computer()

    def computer(self):
        made = Computer(free_port())
        self.addCleanup(made.flow.close)
        return made


class TwoServices(Services):
    def pair(self):
        pair_them(self, self.desk, self.laptop)

    def meet(self):
        """Pair, and let each computer see the mouse once: the desk is channel 1, the laptop channel 2, the mouse at the desk."""
        self.pair()
        self.laptop.mouse(True, 1)
        self.laptop.mouse(False)
        self.desk.mouse(True, 0)
        self.assertTrue(wait(lambda: self.desk.peer().get("channel") == 1))
        self.assertTrue(wait(lambda: self.laptop.peer().get("present") is True))
        time.sleep(0.4)  # in case each called the other: let the two calls become one
        APP.processEvents()

    def cross(self, along=0.5):
        """The pointer reaches the desk's edge and the engine reports that the mouse has gone."""
        self.desk.at_edge("right", along)
        self.desk.flow.on_engine(("left", 1))

    def test_pairing_by_code_sets_everything_up(self):
        self.pair()
        self.assertEqual(self.desk.entry()["key"], self.laptop.entry()["key"])
        self.assertTrue(flow.key_usable(self.desk.entry()["key"]))
        self.assertTrue(self.desk.cfg["enabled"] and self.laptop.cfg["enabled"])
        self.assertEqual(self.laptop.entry()["address"], f"127.0.0.1:{self.desk.cfg['port']}")
        self.assertIsNone(self.desk.flow.status()["pairing"])
        # the computer that showed the code keeps its side, the other one takes the facing one
        self.assertTrue(wait(lambda: self.laptop.entry()["edge"] == "left"))
        self.assertEqual(self.desk.entry()["edge"], "right")

    def test_a_wrong_code_is_said_and_counted(self):
        self.laptop.flow.watch_offers(True)
        self.desk.flow.start_pairing()
        self.assertTrue(wait(lambda: self.laptop.flow.status()["offers"]))
        offer = self.laptop.flow.status()["offers"][0]
        wrong = "000000" if self.desk.flow.status()["pairing"]["code"] != "000000" else "111111"
        self.laptop.flow.pair(offer["address"], offer["port"], wrong)
        self.assertTrue(wait(lambda: self.laptop.flow.status()["pair_error"] == "code"))
        self.assertTrue(wait(lambda: self.desk.flow.status()["pairing"]["left"] == 2))
        self.assertEqual(self.laptop.cfg["peers"], [])

    def test_moving_the_other_computer_on_one_moves_it_on_both(self):
        self.pair()
        self.assertTrue(wait(lambda: self.laptop.entry()["edge"] == "left"))
        self.laptop.flow.place(self.laptop.pair_of(), "top")
        self.assertTrue(wait(lambda: self.desk.entry()["edge"] == "bottom"))

    def test_the_edge_arms_the_push_and_the_pointer_arrives_on_the_other_side(self):
        self.meet()
        self.desk.at_edge("right", 0.25)
        self.assertEqual(self.desk.engine.asked("flow_arm"), [(1, "right", 10.0)])
        self.desk.flow.on_engine(("left", 1))
        self.assertTrue(wait(lambda: self.laptop.engine.asked("expect_mouse")))
        (point, _desktop), = self.laptop.flow._pointer.moves
        screen = QGuiApplication.primaryScreen().geometry()
        self.assertEqual(point[0], screen.x() + flow.ENTRY_INSET)  # through the laptop's left edge
        self.assertAlmostEqual(point[1], screen.y() + 0.25 * (screen.height() - 1), delta=1.5)

    def test_a_mouse_that_has_only_just_arrived_is_not_pushed_straight_back(self):
        self.meet()
        self.addCleanup(setattr, flow_service, "ARRIVAL_QUIET", flow_service.ARRIVAL_QUIET)
        flow_service.ARRIVAL_QUIET = 0.3
        self.cross()
        self.assertTrue(wait(lambda: self.laptop.engine.asked("expect_mouse")))
        self.desk.mouse(False)
        self.laptop.at_edge("left", 0.5)   # the pointer was put near the edge; the mouse is still on its way
        self.laptop.mouse(True, 1)
        self.assertTrue(wait(lambda: self.laptop.peer().get("present") is False))
        self.laptop.at_edge("left", 0.6)   # it is here, and the hand that kept moving reaches the edge at once
        self.assertEqual(self.laptop.engine.asked("flow_arm"), [])
        time.sleep(0.35)
        self.laptop.at_edge("left", 0.7)   # a moment later the edge leads back as usual
        self.assertEqual(self.laptop.engine.asked("flow_arm"), [(0, "left", 10.0)])

    def test_corners_a_full_screen_window_and_leaving_the_edge_do_not_arm(self):
        self.meet()
        self.desk.at_edge("right", 0.03)             # inside the corner kept free (10 % by default)
        self.desk.at_edge("right", 0.5, full=True)   # a game in front
        self.assertEqual(self.desk.engine.asked("flow_arm"), [])
        self.desk.at_edge("right", 0.5)
        self.desk.at_edge("right", 0.03)             # slid into the corner: the push is called off
        self.assertEqual(len(self.desk.engine.asked("flow_arm")), 1)
        self.assertEqual(len(self.desk.engine.asked("flow_disarm")), 1)
        self.desk.store.set("flow", "fullscreen", True)
        self.desk.at_edge("right", 0.5, full=True)
        self.assertEqual(len(self.desk.engine.asked("flow_arm")), 2)

    def test_an_edge_nobody_is_behind_does_nothing(self):
        self.meet()
        self.desk.at_edge("left", 0.5)
        self.assertEqual(self.desk.engine.asked("flow_arm"), [])

    def test_nothing_is_armed_while_the_other_computer_does_not_know_its_channel(self):
        self.pair()
        self.desk.mouse(True, 0)
        self.desk.at_edge("right", 0.5)
        self.assertEqual(self.desk.engine.asked("flow_arm"), [])

    def test_copied_text_crosses_once(self):
        self.meet()
        self.desk.clipboard.content["text"] = "скопійовано на столі"
        self.cross()
        self.assertTrue(wait(lambda: self.laptop.clipboard.written))
        self.assertEqual(self.laptop.clipboard.written[0]["text"], "скопійовано на столі")
        self.cross()  # the same text again: the laptop has it already
        self.assertTrue(wait(lambda: len(self.laptop.engine.asked("expect_mouse")) == 2))
        self.assertEqual(len(self.laptop.clipboard.written), 1)

    def test_what_was_copied_is_sent_again_if_the_link_failed_it(self):
        self.meet()
        self.desk.clipboard.content["text"] = "must arrive"
        real, lost = self.desk.flow.link.send, []

        def lossy(message, data=None, pair=None):  # the network swallows the clipboard once
            if message.get("t") == "clipboard" and not lost:
                lost.append(message)
                return
            real(message, data, pair=pair)

        self.desk.flow.link.send = lossy
        self.cross()
        self.desk.mouse(False)  # the mouse has gone over
        self.assertTrue(wait(lambda: self.laptop.engine.asked("expect_mouse")))
        self.assertEqual(self.laptop.clipboard.written, [])
        self.desk.flow.link._call(lambda: [conn.close() for conn in self.desk.flow.link._conns.values()])  # the link breaks and comes back
        self.assertTrue(wait(lambda: self.laptop.clipboard.written, timeout=10))
        self.assertEqual(self.laptop.clipboard.written[0]["text"], "must arrive")

    def test_a_computer_that_does_not_answer_is_not_sent_the_mouse(self):
        self.meet()
        pair = self.desk.pair_of()
        self.desk.flow._on_link("quiet", True, pair)
        self.desk.at_edge("right", 0.5)
        self.assertEqual(self.desk.engine.asked("flow_arm"), [])
        self.assertTrue(self.desk.peer()["quiet"])
        self.desk.flow._on_link("quiet", False, pair)
        self.desk.at_edge("right", 0.6)
        self.assertEqual(len(self.desk.engine.asked("flow_arm")), 1)

    def test_a_copied_picture_crosses(self):
        self.meet()
        picture = b"\x89PNG\r\n\x1a\n" + os.urandom(200_000)
        self.desk.clipboard.content["image"] = picture
        self.cross()
        self.assertTrue(wait(lambda: self.laptop.clipboard.written))
        self.assertEqual(self.laptop.clipboard.written[0]["image"], picture)

    def test_copied_files_cross_and_wait_on_the_clipboard(self):
        self.meet()
        source = os.path.join(self.folder, "report.txt")
        with open(source, "w") as f:
            f.write("звіт")
        self.desk.clipboard.content["files"] = [source]
        self.cross()
        self.assertTrue(wait(lambda: self.laptop.clipboard.written, timeout=12))
        # the laptop's clipboard was left alone until the files were there: they are the only thing written to it
        self.assertEqual(len(self.laptop.clipboard.written), 1)
        arrived, = self.laptop.clipboard.written[0]["files"]
        self.assertTrue(arrived.startswith(flow_files.STAGING) and arrived.endswith("report.txt"))
        with open(arrived) as f:
            self.assertEqual(f.read(), "звіт")
        # the hand is told where the mouse is: here it never left the desk, so the desk plays it
        self.assertTrue(wait(lambda: self.desk.vibrated() == [self.desk.cfg["cue_arrived"]]))
        self.assertEqual(self.laptop.vibrated(), [])
        # coming back with the same files on the clipboard does not send them home again
        self.laptop.mouse(True, 1)
        self.desk.mouse(False)
        self.assertTrue(wait(lambda: self.laptop.peer().get("present") is False))
        self.laptop.at_edge("left", 0.5)
        self.laptop.flow.on_engine(("left", 0))
        self.assertTrue(wait(lambda: self.desk.engine.asked("expect_mouse")))
        time.sleep(0.3)
        APP.processEvents()
        self.assertEqual(self.desk.clipboard.written, [])
        self.assertIsNone(self.laptop.flow.status()["transfer"])

    def test_the_vibration_is_played_where_the_mouse_is(self):
        self.meet()
        pair = self.laptop.pair_of()
        self.laptop.flow._cue("failed", pair)            # the mouse is at the desk: the laptop passes it on
        self.assertTrue(wait(lambda: self.desk.vibrated() == [self.desk.cfg["cue_failed"]]))
        self.laptop.mouse(True, 1)
        self.laptop.flow._cue("arrived", pair)           # the mouse is here
        self.assertEqual(self.laptop.vibrated(), [self.laptop.cfg["cue_arrived"]])
        self.laptop.store.set("flow", "cue_arrived", None)
        self.laptop.flow._cue("arrived", pair)           # "no vibration" chosen
        self.assertEqual(len(self.laptop.vibrated()), 1)

    def test_while_files_are_arriving_the_old_clipboard_is_not_carried_back(self):
        self.meet()
        self.laptop.clipboard.content["text"] = "old text on the laptop"
        self.laptop.flow._on_link("files-coming", {"count": 1, "total": 50_000_000}, self.laptop.pair_of())  # a long transfer has begun
        self.assertEqual(self.laptop.clipboard.written, [])                           # nothing is put on the clipboard yet
        self.assertEqual(self.laptop.flow.status()["transfer"]["total"], 50_000_000)
        self.laptop.mouse(True, 1)
        self.desk.mouse(False)
        self.laptop.at_edge("left", 0.5)
        self.laptop.flow.on_engine(("left", 0))                                       # the pointer goes back meanwhile
        self.assertTrue(wait(lambda: self.desk.engine.asked("expect_mouse")))
        time.sleep(0.3)
        APP.processEvents()
        self.assertEqual(self.desk.clipboard.written, [])

    def test_files_the_other_side_would_not_take_are_offered_again_next_time(self):
        self.meet()
        source = os.path.join(self.folder, "big.bin")
        with open(source, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024))
        self.laptop.store.set("flow", "files_max_mb", 1)   # the laptop accepts less than the desk would send
        self.desk.clipboard.content["files"] = [source]
        self.cross()
        self.assertTrue(wait(lambda: self.desk.vibrated() == [self.desk.cfg["cue_failed"]]))  # felt where the mouse is
        self.laptop.store.set("flow", "files_max_mb", 100)
        self.cross()                                       # the same files are still on the desk's clipboard
        self.assertTrue(wait(lambda: self.laptop.clipboard.written, timeout=12))

    def test_files_over_the_limit_here_stay_and_the_hand_is_told(self):
        self.meet()
        source = os.path.join(self.folder, "big.bin")
        with open(source, "wb") as f:
            f.write(b"x" * (2 * 1024 * 1024))
        self.desk.store.set("flow", "files_max_mb", 1)
        self.desk.clipboard.content["files"] = [source]
        self.laptop.mouse(True, 1)   # by the time anything is said, the mouse is at the laptop
        self.cross()
        self.assertTrue(wait(lambda: self.laptop.vibrated() == [self.laptop.cfg["cue_failed"]]))
        self.assertEqual(self.laptop.clipboard.written, [])

    def test_the_mouse_follows_the_keyboard_both_computers_see(self):
        self.meet()
        self.desk.store.set("flow", "mouse_follows_keyboard", True)
        keyboard(self.desk, LINK, True)
        self.assertTrue(wait(lambda: (self.laptop.flow._heard.get(self.laptop.pair_of()) or {}).get("here")))
        keyboard(self.desk, LINK, False, ago=0.0)  # its Easy-Switch key sends it to the laptop, which finds it
        keyboard(self.laptop, LINK, True)
        self.assertTrue(wait(lambda: self.desk.engine.asked("flow_leave")))
        self.assertEqual(self.desk.engine.asked("flow_leave"), [(1, False)])
        self.desk.flow.on_engine(("left", 1))
        self.assertTrue(wait(lambda: self.laptop.engine.asked("expect_mouse")))
        self.desk.mouse(False)
        self.laptop.mouse(True, 1)
        time.sleep(0.3)
        APP.processEvents()
        self.assertEqual(self.laptop.engine.asked("flow_leave"), [])  # the keyboard is where the mouse is: nothing to follow
        self.assertEqual(len(self.desk.engine.asked("flow_leave")), 1)

    def test_an_older_version_on_the_other_side_neither_sends_nor_minds_the_keyboard(self):
        self.meet()
        self.desk.store.set("flow", "mouse_follows_keyboard", True)
        keyboard(self.laptop, LINK, True)
        self.assertTrue(wait(lambda: self.desk.flow._heard.get(self.desk.pair_of(), {}).get("here")))
        self.assertNotIn("keyboard", self.desk.flow._peers[self.desk.pair_of()])  # as the copy of an older version drops it
        old = {key: value for key, value in self.desk.flow._peers[self.desk.pair_of()].items() if key != "id"}
        keyboard(self.desk, LINK, False, ago=0.0)
        for _ in range(2):
            self.desk.flow._on_link("status", dict(old), self.desk.pair_of())  # what an older version says
        self.assertEqual(self.desk.engine.asked("flow_leave"), [])
        self.assertEqual(self.desk.peer()["channel"], 1)

    def test_forgetting_the_other_computer_switches_flow_off(self):
        self.pair()
        self.desk.flow.unpair(self.desk.pair_of())
        self.assertEqual(self.desk.cfg["peers"], [])
        self.assertFalse(self.desk.cfg["enabled"])
        self.assertTrue(wait(lambda: not self.laptop.linked()))

    def test_settings_from_before_there_could_be_two_are_taken_over(self):
        old = {"flow": {"enabled": True, "key": "ABCD-EFGH-IJKL-MNOP", "peer": "192.168.1.5", "peer_name": "laptop",
                        "edge": "left", "edge_at": 5.0}}
        link = config.clean(old)["flow"]
        self.assertEqual(link["peers"], [{"key": "ABCD-EFGH-IJKL-MNOP", "id": "", "address": "192.168.1.5", "name": "laptop",
                                          "edge": "left", "edge_at": 5.0}])
        self.assertNotIn("key", link)


LINK, OTHER_LINK = "0123456789abcdef", "fedcba9876543210"  # made-up keyboard links (keyboard.flow_link)
KEY = "SYNT-HETI-CKEY-0001"
NOTHING = object()


def keyboard(computer, link, here, ago=None, sent=False):
    """What the keyboard side tells Flow on ``computer`` (KeyboardSide._tell_flow): ``ago`` seconds since it left."""
    computer.flow.on_engine(("keyboard", {"link": link, "here": here,
                                          "drop": None if ago is None else (time.monotonic() - ago, sent)}))


class Follow(Services):
    """The mouse following the keyboard, with what the other computer says made up and handed in directly."""

    def setUp(self):
        super().setUp()
        self.x = self.desk
        self.x.store.set_section("flow", dict(self.x.cfg, enabled=True, mouse_follows_keyboard=True, peers=[
            {"key": KEY, "id": "peer", "address": "", "name": "peer", "edge": "right", "edge_at": 0.0}]))
        self.pair = flow.pair_id(KEY)
        self.token = flow_service.keyboard_token(KEY, LINK)
        self.x.mouse(True, 0)
        keyboard(self.x, LINK, True)

    def say(self, here=None, seq=0, kind="status", token=None, field=NOTHING, channel=1):
        payload = {"name": "peer", "channel": channel, "present": False, "edge": "left", "edge_at": 0.0}
        if field is not NOTHING:
            payload["keyboard"] = field
        elif here is not None:
            payload["keyboard"] = {"token": token or self.token, "here": here, "seq": seq}
        self.x.flow._on_link(kind, payload, self.pair)

    def up(self, here=None, seq=0, **more):
        self.say(here, seq, kind="up", **more)

    def moves(self):
        return self.x.engine.asked("flow_leave")

    def test_a_keyboard_that_arrives_there_after_leaving_here_takes_the_mouse_along(self):
        self.up(False, 1)
        keyboard(self.x, LINK, False, ago=0.0)
        with self.assertLogs("mxoptions.flow_service", "INFO") as logs:
            self.say(True, 2)
        self.assertEqual(self.moves(), [(1, False)])  # to that computer's channel, as the action does: no edge
        self.assertIn("follows the keyboard", logs.output[-1])

    def test_tokens_are_the_pairing_s_own(self):
        token = flow_service.keyboard_token(KEY, LINK)
        self.assertRegex(token, r"\A[0-9a-f]{16}\Z")
        self.assertEqual(token, flow_service.keyboard_token(KEY.lower().replace("-", " "), LINK))  # the key as both hold it
        self.assertNotEqual(token, flow_service.keyboard_token("SYNT-HETI-CKEY-0002", LINK))
        self.assertNotEqual(token, flow_service.keyboard_token(KEY, OTHER_LINK))
        self.assertNotIn(LINK, token)

    def test_anything_but_the_exact_field_is_left_out(self):
        good = {"token": self.token, "here": True, "seq": 2}
        bad = [{"token": flow_service.keyboard_token(KEY, OTHER_LINK)}, {"token": self.token.upper()},
               {"token": self.token[:15]}, {"token": self.token + "0"}, {"token": 7}, {"token": None},
               {"here": 1}, {"here": "true"}, {"here": None}, {"seq": -1}, {"seq": 2 ** 31}, {"seq": True},
               {"seq": 2.0}, {"seq": "2"}, {"seq": None}, {"more": 1}]
        fields = [dict(good, **change) for change in bad] + [
            {key: value for key, value in good.items() if key != left} for left in good] + [[], None, "here", {}, [good]]
        for field in fields:
            with self.subTest(field=field):
                self.x.flow._on_link("down", None, self.pair)
                self.up(False, 1)
                keyboard(self.x, LINK, False, ago=0.0)
                self.say(field=field)
                self.assertEqual(self.moves(), [])
                keyboard(self.x, LINK, True)
        self.x.flow._on_link("down", None, self.pair)
        self.up(False, 1)
        keyboard(self.x, LINK, False, ago=0.0)
        self.say(True, flow_service.SEQ_MAX)  # the largest there is still counts
        self.assertEqual(self.moves(), [(1, False)])

    def test_only_an_arrival_after_it_left_here_and_soon_after(self):
        self.up(True, 1)  # there already when the link came up
        keyboard(self.x, LINK, False, ago=0.0)
        self.say(True, 2)  # still there: no arrival
        self.say(field=NOTHING)  # an old version's status, or no keyboard there, says nothing of it either
        self.assertEqual(self.moves(), [])
        self.x.flow._on_link("down", None, self.pair)
        keyboard(self.x, LINK, True)
        self.up(False, 1)
        self.say(True, 2)  # arrived there while it was still here
        keyboard(self.x, LINK, False, ago=0.0)
        self.say(True, 3)
        self.assertEqual(self.moves(), [])
        self.x.flow._on_link("down", None, self.pair)
        self.up(False, 1)
        keyboard(self.x, LINK, False, ago=flow_service.FOLLOW_WINDOW + 0.5)
        self.say(True, 2)  # too late
        self.assertEqual(self.moves(), [])

    def test_not_while_switched_off_and_not_on_switching_it_on(self):
        self.x.store.set("flow", "mouse_follows_keyboard", False)
        self.up(False, 1)
        keyboard(self.x, LINK, False, ago=0.0)
        self.say(True, 2)
        self.x.store.set("flow", "mouse_follows_keyboard", True)
        self.say(True, 3)
        self.assertEqual(self.moves(), [])

    def test_not_after_it_came_back_here(self):
        self.up(False, 1)
        keyboard(self.x, LINK, False, ago=0.0)
        keyboard(self.x, LINK, True)
        self.say(True, 2)
        self.assertEqual(self.moves(), [])
        keyboard(self.x, LINK, False, ago=0.0)
        self.say(False, 3)
        self.say(True, 4)
        self.x.flow.on_engine(("refused", "held"))
        self.x.flow._follow_at = None
        keyboard(self.x, LINK, True)  # back after that try too
        self.say(False, 5)
        self.say(True, 6)
        self.assertEqual(self.moves(), [(1, False)])

    def test_another_keyboard_there_does_not_count(self):
        self.up(False, 1)
        keyboard(self.x, LINK, False, ago=0.0)
        self.say(True, 2, token=flow_service.keyboard_token(KEY, OTHER_LINK))
        self.say(False, 3)
        self.say(True, 4, token=flow_service.keyboard_token("SYNT-HETI-CKEY-0002", LINK))  # the token of another pairing
        self.assertEqual(self.moves(), [])

    def test_repeated_older_and_late_statuses_move_it_once_at_most(self):
        self.up(False, 5)
        keyboard(self.x, LINK, False, ago=0.0)
        self.say(True, 5)  # the same seq again
        self.say(True, 4)  # an older one
        self.assertEqual(self.moves(), [])
        self.say(False, 6)
        self.say(True, 7)
        self.say(False, 6)  # arrives late: nothing
        self.say(True, 8)
        self.say(False, 9)
        self.say(True, 10)  # a second arrival, for the same going
        self.assertEqual(self.moves(), [(1, False)])

    def test_a_new_link_starts_from_what_it_says_first(self):
        self.up(False, 1)
        keyboard(self.x, LINK, False, ago=0.0)
        self.up(True, 2)  # a second call that took over from the first: how things are, not a change
        self.x.flow._on_link("down", None, self.pair)
        self.up(True, 3)  # back after a break: the same
        self.say(True, 4)
        self.assertEqual(self.moves(), [])
        self.x.flow._on_link("down", None, self.pair)
        self.up(False, 0)  # the other computer started again: its count starts again too
        self.say(True, 1)
        self.assertEqual(self.moves(), [(1, False)])

    def test_a_keyboard_sent_away_from_here_is_not_followed(self):
        self.up(False, 1)
        keyboard(self.x, LINK, False, ago=0.0, sent=True)
        self.say(True, 2)
        self.say(False, 3)
        self.say(True, 4)
        self.assertEqual(self.moves(), [])

    def test_one_try_for_each_going_whatever_stops_it(self):
        self.up(False, 1)
        keyboard(self.x, LINK, False, ago=0.0)
        self.say(True, 2)
        self.x.flow.on_engine(("refused", "held"))  # a button was held: the engine kept the mouse
        self.say(False, 3)
        self.say(True, 4)
        self.assertEqual(self.moves(), [(1, False)])  # not tried again
        stops = {"on its way": lambda: setattr(self.x.flow, "_leaving", self.pair),
                 "just sent after it": lambda: None,  # FOLLOW_GRACE after the try above
                 "mouse not here": lambda: self.x.mouse(False),
                 "channel unknown": lambda: self.say(False, 5, channel=None),
                 "channel this one's": lambda: self.say(False, 5, channel=0),
                 "no answer": lambda: self.x.flow._on_link("quiet", True, self.pair)}
        for name, stop in stops.items():
            with self.subTest(name):
                self.x.flow._on_link("down", None, self.pair)
                self.up(False, 1)
                self.x.mouse(True, 0)
                keyboard(self.x, LINK, True)
                keyboard(self.x, LINK, False, ago=0.0)
                self.x.flow._follow_at = time.monotonic() if name == "just sent after it" else None
                stop()
                self.say(True, 6, channel=self.x.flow._peers[self.pair]["channel"])
                self.x.flow._leaving = None
                self.x.mouse(True, 0)
                self.x.flow._on_link("quiet", False, self.pair)
                self.say(False, 7)
                self.say(True, 8)  # the hindrance is gone, but this going had its try
                self.assertEqual(self.moves(), [(1, False)])

    def test_what_is_said_of_it_stays_out_of_the_peers_the_log_the_settings_and_the_bus(self):
        from mxoptions import api
        bus = api.MouseApi(self.x.store, None, lambda: None)
        emitted = []
        for name in ("ButtonPressed", "ButtonReleased", "Gesture", "WheelModeChanged", "Hook", "ConnectedChanged",
                     "BatteryChanged", "DpiChanged", "ConfigChanged"):
            getattr(bus, name).connect(lambda *args, n=name: emitted.append(n))
        with self.assertLogs("mxoptions", "DEBUG") as logs:
            self.up(False, 1)
            keyboard(self.x, LINK, False, ago=0.0)
            bus.on_event("flow", ("keyboard", {"link": LINK, "here": False, "drop": (0.0, False)}))
            self.say(True, 2)
            self.x.flow.on_engine(("left", 1))
            self.x.flow._send_status()
            APP.processEvents()
        self.assertEqual(self.moves(), [(1, False)])
        self.assertEqual(emitted, [])
        self.assertNotIn("keyboard", self.x.flow._peers[self.pair])
        for shown in ("\n".join(logs.output), json.dumps(self.x.flow.status()), json.dumps(self.x.store.cfg),
                      json.dumps(config.clean(copy.deepcopy(self.x.store.cfg))), json.dumps(self.x.store.state, default=str)):
            for secret in (LINK, self.token):
                self.assertNotIn(secret, shown)
        self.assertIs(self.x.store.cfg["flow"]["mouse_follows_keyboard"], True)

    def test_each_of_the_others_is_told_under_its_own_token_and_nobody_else(self):
        second = "SYNT-HETI-CKEY-0002"
        peers = self.x.cfg["peers"] + [{"key": second, "id": "peer2", "address": "", "name": "peer2", "edge": "left", "edge_at": 0.0}]
        self.x.store.set("flow", "peers", peers)
        self.assertTrue(wait(lambda: len(self.x.flow.link._status_for) == 2
                             and all("keyboard" in more for more in self.x.flow.link._status_for.values())))
        told = {pair: more["keyboard"] for pair, more in self.x.flow.link._status_for.items()}
        self.assertEqual(told, {self.pair: {"token": self.token, "here": True, "seq": 1},
                                flow.pair_id(second): {"token": flow_service.keyboard_token(second, LINK), "here": True, "seq": 1}})
        self.assertNotIn("keyboard", self.x.flow.link._status)  # never said to all alike
        keyboard(self.x, LINK, False, ago=0.0)
        self.assertTrue(wait(lambda: self.x.flow.link._status_for[self.pair]["keyboard"]["seq"] == 2))
        self.assertFalse(self.x.flow.link._status_for[self.pair]["keyboard"]["here"])


def kb_here(computer, link, here, channel=None, ago=None, sent=False):
    """As keyboard() above, with the paired channel the keyboard side names (None: not known)."""
    computer.flow.on_engine(("keyboard", {"link": link, "here": here, "channel": channel,
                                          "drop": None if ago is None else (time.monotonic() - ago, sent)}))


class KeyboardFollows(Services):
    """The keyboard following the mouse: what the other computer says of its keyboard is made up and handed in."""

    def setUp(self):
        super().setUp()
        self.x = self.desk
        self.x.store.set_section("flow", dict(self.x.cfg, enabled=True, keyboard_follows_mouse=True, peers=[
            {"key": KEY, "id": "peer", "address": "", "name": "peer", "edge": "right", "edge_at": 0.0}]))
        self.pair = flow.pair_id(KEY)
        self.token = flow_service.keyboard_token(KEY, LINK)
        self.x.mouse(True, 0)
        kb_here(self.x, LINK, True, channel=0)

    def say(self, channel=2, kind="status", token=None, field=NOTHING, keyboard=None):
        payload = {"name": "peer", "channel": 1, "present": False, "edge": "left", "edge_at": 0.0}
        if field is not NOTHING:
            payload["keyboard_host"] = field
        elif channel is not None:
            payload["keyboard_host"] = {"token": token or self.token, "channel": channel}
        if keyboard is not None:
            payload["keyboard"] = keyboard
        self.x.flow._on_link(kind, payload, self.pair)

    def up(self, channel=2, **more):
        self.say(channel, kind="up", **more)

    def asked(self):
        return self.x.engine.asked("keyboard_follow")

    def cancelled(self):
        return [call[0] for call in self.x.engine.asked("keyboard_follow_cancel")]

    def left(self):
        self.x.flow.on_engine(("left", 1))

    def test_the_field_is_taken_exactly_as_sent_or_not_at_all(self):
        good = {"token": self.token, "channel": 2}
        self.assertEqual(flow_service.keyboard_host_said(good), good)
        for channel in (0, 15):
            self.assertEqual(flow_service.keyboard_host_said(dict(good, channel=channel))["channel"], channel)
        bad = [dict(good, channel=value) for value in (16, -1, True, False, 2.0, "2", None)]
        bad += [dict(good, token=value) for value in (self.token.upper(), self.token[:15], self.token + "0", 7, None)]
        bad += [{"token": self.token}, {"channel": 2}, dict(good, here=True), [good], None, "x", {}]
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(flow_service.keyboard_host_said(value))
                self.x.flow._on_link("down", None, self.pair)
                self.up(field=value)
                self.left()
                self.assertEqual(self.asked(), [])

    def test_the_mouse_leaving_sends_the_keyboard_to_that_computer_s_channel(self):
        self.up(2)
        with self.assertNoLogs("mxoptions.flow_service", "INFO"):
            self.left()
        [(channel, link, event)] = self.asked()
        self.assertEqual((channel, link), (2, LINK))
        self.assertIsInstance(event, threading.Event)
        self.assertFalse(event.is_set())

    def test_each_reason_the_keyboard_stays(self):
        def s4a_sends_the_mouse():
            self.x.store.set("flow", "mouse_follows_keyboard", True)
            keyboard(self.x, LINK, True)
            self.say(None, keyboard={"token": self.token, "here": False, "seq": 1})
            kb_here(self.x, LINK, False, ago=0.0)
            self.say(None, keyboard={"token": self.token, "here": True, "seq": 2})
            self.assertEqual(self.x.engine.asked("flow_leave")[-1], (1, False))
        cases = {
            "follow-cause": s4a_sends_the_mouse,
            "not-here": lambda: kb_here(self.x, LINK, False, ago=0.0),
            "no-channel": lambda: self.say(None),
            "token": lambda: self.say(2, token=flow_service.keyboard_token(KEY, OTHER_LINK)),
            "token (another pairing's)": lambda: self.say(2, token=flow_service.keyboard_token("SYNT-HETI-CKEY-0002", LINK)),
        }
        for name, prepare in cases.items():
            with self.subTest(name):
                self.x.flow._on_link("down", None, self.pair)
                self.x.mouse(True, 0)
                kb_here(self.x, LINK, True, channel=0)
                self.up(2)
                prepare()
                with self.assertLogs("mxoptions.flow_service", "INFO") as logs:
                    self.left()
                self.assertIn(f"flow: the keyboard stays ({name.split(' ')[0]})", logs.output[-1])
                self.assertEqual(self.asked(), [])
                self.x.store.set("flow", "mouse_follows_keyboard", False)
        self.x.flow._on_link("down", None, self.pair)
        self.left()  # the link is down: nothing heard
        self.x.store.set("flow", "keyboard_follows_mouse", False)
        kb_here(self.x, LINK, True, channel=0)
        self.up(2)
        with self.assertNoLogs("mxoptions.flow_service", "INFO"):
            self.left()  # switched off: not a word but in the debug log
        self.assertEqual(self.asked(), [])

    def attempt(self):
        """Have the keyboard sent after the mouse; the Event the engine was given."""
        self.x.flow._on_link("down", None, self.pair)
        self.x.engine.calls.clear()
        self.up(2)
        self.left()
        [(_channel, _link, event)] = self.asked()
        self.say(2)  # the same again: nothing changes
        self.assertEqual(self.cancelled(), [])
        return event

    def test_what_calls_it_off_before_the_keyboard_goes(self):
        stops = {
            "the link goes down": lambda: self.x.flow._on_link("down", None, self.pair),
            "the link starts anew": lambda: self.up(2),
            "another channel said": lambda: self.say(3),
            "the field gone": lambda: self.say(None),
            "another keyboard said": lambda: self.say(2, token=flow_service.keyboard_token(KEY, OTHER_LINK)),
            "the mouse sent again": self.left,
            "the setting switched off": lambda: self.x.store.set("flow", "keyboard_follows_mouse", False),
        }
        for name, stop in stops.items():
            with self.subTest(name):
                self.x.store.set("flow", "keyboard_follows_mouse", True)
                event = self.attempt()
                stop()
                self.assertTrue(event.is_set())
                self.assertEqual(self.cancelled(), [event])

    def test_flow_switched_off_calls_it_off(self):
        event = self.attempt()
        self.x.store.set("flow", "enabled", False)
        self.assertTrue(event.is_set())
        self.assertEqual(self.cancelled(), [event])

    def test_forgetting_that_computer_calls_it_off(self):
        event = self.attempt()
        self.x.flow.unpair(self.pair)
        self.assertTrue(event.is_set())
        self.assertEqual(self.cancelled(), [event])

    def test_only_what_was_said_on_this_link(self):
        self.up(2)
        self.x.flow._on_link("down", None, self.pair)
        self.up(None)  # back, and says nothing of it now
        self.left()
        self.assertEqual(self.asked(), [])
        self.say(2)
        self.x.flow._on_link("down", None, self.pair)
        self.left()
        self.assertEqual(self.asked(), [])

    def test_each_of_the_others_is_told_the_channel_under_its_own_token(self):
        second = "SYNT-HETI-CKEY-0002"
        peers = self.x.cfg["peers"] + [{"key": second, "id": "peer2", "address": "", "name": "peer2", "edge": "left", "edge_at": 0.0}]
        self.x.store.set("flow", "peers", peers)

        def told():
            return self.x.flow.link._status_for
        self.assertTrue(wait(lambda: len(told()) == 2 and all("keyboard_host" in more for more in told().values())))
        self.assertEqual({pair: more["keyboard_host"] for pair, more in told().items()},
                         {self.pair: {"token": self.token, "channel": 0},
                          flow.pair_id(second): {"token": flow_service.keyboard_token(second, LINK), "channel": 0}})
        self.assertNotIn("keyboard_host", self.x.flow.link._status)
        kb_here(self.x, LINK, False, ago=0.0)  # gone elsewhere: how to have it back is still said
        self.assertTrue(wait(lambda: told()[self.pair]["keyboard"]["here"] is False))
        self.assertEqual(told()[self.pair]["keyboard_host"], {"token": self.token, "channel": 0})
        kb_here(self.x, LINK, True, channel=1)  # back, on another channel
        self.assertTrue(wait(lambda: told()[self.pair]["keyboard_host"]["channel"] == 1))
        kb_here(self.x, OTHER_LINK, True)  # another keyboard, whose pairing is not known
        self.assertTrue(wait(lambda: "keyboard_host" not in told()[self.pair]))
        self.assertIn("keyboard", told()[self.pair])

    def test_an_older_computer_s_status_is_read_as_before(self):
        self.x.store.set("flow", "mouse_follows_keyboard", True)
        self.up(2, keyboard={"token": self.token, "here": False, "seq": 1})
        self.assertNotIn("keyboard_host", self.x.flow._peers[self.pair])
        self.assertEqual(set(self.x.flow._peers[self.pair]), {"name", "channel", "present", "edge", "edge_at", "id", "quiet"})
        kb_here(self.x, LINK, False, ago=0.0)
        self.say(2, keyboard={"token": self.token, "here": True, "seq": 2})  # the mouse still follows the keyboard
        self.assertEqual(self.x.engine.asked("flow_leave"), [(1, False)])
        self.x.flow.on_engine(("refused", "held"))  # and the engine kept it after all
        self.x.flow._on_link("down", None, self.pair)
        kb_here(self.x, LINK, True, channel=0)
        self.up(None, keyboard={"token": self.token, "here": False, "seq": 1})  # a version without it
        with self.assertLogs("mxoptions.flow_service", "INFO") as logs:
            self.left()
        self.assertIn("stays (no-channel)", logs.output[-1])

    def test_nothing_of_it_is_in_the_log_the_status_the_settings_or_the_bus(self):
        from mxoptions import api
        bus = api.MouseApi(self.x.store, None, lambda: None)
        emitted = []
        for name in ("ButtonPressed", "ButtonReleased", "Gesture", "WheelModeChanged", "Hook", "ConnectedChanged",
                     "BatteryChanged", "DpiChanged", "ConfigChanged"):
            getattr(bus, name).connect(lambda *args, n=name: emitted.append(n))
        with self.assertLogs("mxoptions", "DEBUG") as logs:
            logging.getLogger("mxoptions").debug("start")
            self.up(2)
            bus.on_event("flow", ("keyboard", {"link": LINK, "here": True, "drop": None, "channel": 0}))
            self.left()
            self.x.flow._send_status()
            APP.processEvents()
        self.assertEqual(len(self.asked()), 1)
        self.assertEqual(emitted, [])
        for shown in ("\n".join(logs.output), json.dumps(self.x.flow.status()), json.dumps(self.x.store.cfg),
                      json.dumps(self.x.store.state, default=str)):
            for secret in (LINK, self.token, "keyboard_host"):
                self.assertNotIn(secret, shown)


class KeyboardFollowsOverTheLink(TwoServices):
    """Both computers as the program runs them, over a real link: what one says, the other hears."""

    def test_the_keyboard_goes_where_the_mouse_went_and_nothing_goes_back(self):
        self.meet()
        for computer in (self.desk, self.laptop):
            computer.store.set("flow", "keyboard_follows_mouse", True)
            computer.store.set("flow", "mouse_follows_keyboard", True)
        kb_here(self.laptop, LINK, True, channel=1)  # the keyboard was at the laptop, on its channel 2
        kb_here(self.laptop, LINK, False, ago=60.0)
        kb_here(self.desk, LINK, True, channel=0)  # and is at the desk now
        self.assertTrue(wait(lambda: self.desk.flow._kb_hosts.get(self.desk.pair_of())))
        self.assertEqual(self.desk.flow._kb_hosts[self.desk.pair_of()],
                         {"token": flow_service.keyboard_token(self.desk.entry()["key"], LINK), "channel": 1})
        self.cross()
        self.assertEqual([call[:2] for call in self.desk.engine.asked("keyboard_follow")], [(1, LINK)])
        kb_here(self.desk, LINK, False, ago=0.0, sent=True)  # sent by this program: the mouse does not chase it
        kb_here(self.laptop, LINK, True, channel=1)
        self.desk.mouse(False)
        self.laptop.mouse(True, 1)
        time.sleep(0.5)
        APP.processEvents()
        for computer in (self.desk, self.laptop):
            self.assertEqual(computer.engine.asked("flow_leave"), [])
        self.assertEqual(self.laptop.engine.asked("keyboard_follow"), [])  # the mouse arriving moves nothing
        self.assertEqual(len(self.desk.engine.asked("keyboard_follow")), 1)


class Page(unittest.TestCase):
    """The switch on the Flow page: there once a keyboard was found, off at first."""

    def test_the_setting_is_off_at_first_and_only_ever_true_or_false(self):
        self.assertIs(config.DEFAULTS["flow"]["mouse_follows_keyboard"], False)
        self.assertEqual(config.DEFAULTS["version"], 6)
        for value, kept in ((True, True), (1, True), ("yes", True), (None, False), (0, False)):
            self.assertIs(config.clean({"flow": {"mouse_follows_keyboard": value}})["flow"]["mouse_follows_keyboard"], kept)
        self.assertIs(config.clean({"flow": {}})["flow"]["mouse_follows_keyboard"], False)

    def test_the_switch_shows_with_a_keyboard_and_sets_the_setting(self):
        from mxoptions.ui import flow_page
        store = Store(copy.deepcopy(config.DEFAULTS), Engine())
        store.flush = lambda: None
        store._save_timer.timeout.disconnect()
        page = flow_page.FlowPage(store)
        self.addCleanup(page.deleteLater)
        page.show()
        self.assertTrue(page.follow_row.isHidden())
        self.assertFalse(page.follow.isChecked())
        store.update_state(dict(store.state, keyboard={"connected": False, "known": True, "name": "Synthetic keyboard"}))
        self.assertFalse(page.follow_row.isHidden())
        page.follow.toggle()
        self.assertIs(store.cfg["flow"]["mouse_follows_keyboard"], True)
        store.update_state(dict(store.state, keyboard=None))
        self.assertTrue(page.follow_row.isHidden())

    def test_the_keyboard_following_switch_shows_with_a_keyboard_and_sets_its_setting(self):
        from mxoptions.ui import flow_page
        store = Store(copy.deepcopy(config.DEFAULTS), Engine())
        store.flush = lambda: None
        store._save_timer.timeout.disconnect()
        page = flow_page.FlowPage(store)
        self.addCleanup(page.deleteLater)
        page.show()
        self.assertTrue(page.lead_row.isHidden())
        self.assertFalse(page.lead.isChecked())
        store.update_state(dict(store.state, keyboard={"connected": True, "known": True, "name": "Synthetic keyboard"}))
        self.assertFalse(page.lead_row.isHidden())
        self.assertEqual(page.lead_row.title.text(), "Keyboard follows the mouse")
        self.assertIn("Windows can receive the keyboard but cannot send it on", page.lead_row.info.text())
        page.lead.toggle()
        self.assertIs(store.cfg["flow"]["keyboard_follows_mouse"], True)
        self.assertIs(store.cfg["flow"]["mouse_follows_keyboard"], False)  # the other one stays as it was
        store.set("flow", "keyboard_follows_mouse", False)
        self.assertFalse(page.lead.isChecked())
        store.update_state(dict(store.state, keyboard=None))
        self.assertTrue(page.lead_row.isHidden())


class ThreeServices(Services):
    """The desk in the middle, a laptop on one side and a tower on the other. Not tried on three real computers."""

    def setUp(self):
        super().setUp()
        self.tower = self.computer()
        pair_them(self, self.desk, self.laptop)
        pair_them(self, self.desk, self.tower, peers_of_host=2)
        for computer, channel in ((self.laptop, 1), (self.tower, 2)):
            computer.mouse(True, channel)
            computer.mouse(False)
        self.desk.mouse(True, 0)
        self.assertTrue(wait(lambda: [self.desk.peer(0).get("channel"), self.desk.peer(1).get("channel")] == [1, 2]))
        time.sleep(0.4)
        APP.processEvents()

    def test_each_of_the_two_gets_a_side_of_its_own_and_takes_the_facing_one(self):
        self.assertEqual([self.desk.entry(0)["edge"], self.desk.entry(1)["edge"]], ["right", "left"])
        self.assertTrue(wait(lambda: self.laptop.entry()["edge"] == "left" and self.tower.entry()["edge"] == "right"))
        self.assertEqual({self.desk.entry(0)["id"], self.desk.entry(1)["id"]}, {self.laptop.cfg["id"], self.tower.cfg["id"]})

    def test_each_edge_leads_to_its_own_computer(self):
        self.desk.at_edge("right", 0.5)
        self.assertEqual(self.desk.engine.asked("flow_arm"), [(1, "right", 10.0)])
        self.desk.flow.on_engine(("left", 1))
        self.assertTrue(wait(lambda: self.laptop.engine.asked("expect_mouse")))
        self.assertEqual(self.tower.engine.asked("expect_mouse"), [])
        self.desk.at_edge("left", 0.5)
        self.assertEqual(self.desk.engine.asked("flow_arm")[-1], (2, "left", 10.0))
        self.desk.flow.on_engine(("left", 2))
        self.assertTrue(wait(lambda: self.tower.engine.asked("expect_mouse")))
        self.assertEqual(len(self.laptop.engine.asked("expect_mouse")), 1)

    def test_what_was_copied_goes_only_where_the_pointer_goes(self):
        self.desk.clipboard.content["text"] = "for the tower"
        self.desk.at_edge("left", 0.5)
        self.desk.flow.on_engine(("left", 2))
        self.assertTrue(wait(lambda: self.tower.clipboard.written))
        self.assertEqual(self.tower.clipboard.written[0]["text"], "for the tower")
        self.assertEqual(self.laptop.clipboard.written, [])
        self.desk.at_edge("right", 0.5)   # and the laptop gets it when the pointer goes there
        self.desk.flow.on_engine(("left", 1))
        self.assertTrue(wait(lambda: self.laptop.clipboard.written))

    def test_putting_one_where_the_other_stands_makes_them_change_places(self):
        self.desk.flow.place(self.desk.pair_of(1), "right")
        self.assertEqual([self.desk.entry(0)["edge"], self.desk.entry(1)["edge"]], ["left", "right"])
        self.assertTrue(wait(lambda: self.laptop.entry()["edge"] == "right" and self.tower.entry()["edge"] == "left"))

    def test_forgetting_one_keeps_the_other(self):
        self.desk.flow.unpair(self.desk.pair_of(0))
        self.assertEqual(len(self.desk.cfg["peers"]), 1)
        self.assertTrue(self.desk.cfg["enabled"])
        self.assertTrue(wait(lambda: self.desk.linked() and not self.laptop.linked()))

    def test_the_keyboard_going_round_three_computers_moves_the_mouse_once(self):
        for computer in (self.desk, self.laptop, self.tower):
            computer.store.set("flow", "mouse_follows_keyboard", True)
        laptop, tower = self.desk.pair_of(0), self.desk.pair_of(1)
        heard = lambda computer, pair: (computer.flow._heard.get(pair) or {}).get("here")  # noqa: E731
        keyboard(self.tower, LINK, True)
        self.assertTrue(wait(lambda: heard(self.desk, tower)))
        keyboard(self.tower, LINK, False, ago=0.0)  # to the desk, where the mouse is already
        keyboard(self.desk, LINK, True)
        self.assertTrue(wait(lambda: heard(self.tower, self.tower.pair_of()) and not heard(self.desk, tower)))
        keyboard(self.desk, LINK, False, ago=0.0)  # and on to the laptop
        keyboard(self.laptop, LINK, True)
        self.assertTrue(wait(lambda: self.desk.engine.asked("flow_leave")))
        stale = {"name": "laptop", "channel": 1, "present": False, "edge": "left", "edge_at": 0.0,
                 "keyboard": {"token": flow_service.keyboard_token(self.desk.entry(0)["key"], LINK), "here": True, "seq": 0}}
        self.desk.flow._on_link("status", stale, laptop)  # late, and from before
        self.desk.flow.on_engine(("left", 1))
        self.assertTrue(wait(lambda: self.laptop.engine.asked("expect_mouse")))
        self.desk.mouse(False)
        self.laptop.mouse(True, 1)
        time.sleep(0.5)
        APP.processEvents()
        self.assertEqual(self.desk.engine.asked("flow_leave"), [(1, False)])
        self.assertEqual(self.tower.engine.asked("flow_leave"), [])  # its keyboard's going was had: the mouse was not there
        self.assertEqual(self.laptop.engine.asked("flow_leave"), [])
        self.assertEqual(self.tower.engine.asked("expect_mouse"), [])

    def test_the_keyboard_follows_the_mouse_to_the_computer_it_went_to(self):
        self.desk.store.set("flow", "keyboard_follows_mouse", True)
        for computer, channel in ((self.laptop, 1), (self.tower, 2)):  # the keyboard was at each, on its channel there
            kb_here(computer, LINK, True, channel=channel)
            kb_here(computer, LINK, False, ago=60.0)
        kb_here(self.desk, LINK, True, channel=0)
        laptop, tower = self.desk.pair_of(0), self.desk.pair_of(1)
        self.assertTrue(wait(lambda: laptop in self.desk.flow._kb_hosts and tower in self.desk.flow._kb_hosts))
        self.desk.at_edge("left", 0.5)
        self.desk.flow.on_engine(("left", 2))
        self.desk.at_edge("right", 0.5)
        self.desk.flow.on_engine(("left", 1))  # sent on before the keyboard went: the first one is called off
        follows = self.desk.engine.asked("keyboard_follow")
        self.assertEqual([call[:2] for call in follows], [(2, LINK), (1, LINK)])
        self.assertEqual([call[0] for call in self.desk.engine.asked("keyboard_follow_cancel")], [follows[0][2]])
        self.assertTrue(follows[0][2].is_set())
        self.assertFalse(follows[1][2].is_set())
        for computer in (self.laptop, self.tower):
            self.assertEqual(computer.engine.asked("keyboard_follow"), [])

    def third_pairing(self, paired=lambda: None):
        """A fourth computer pairs with the desk; ``paired`` runs once the fourth has it. Returns the fourth."""
        fourth = self.computer()
        fourth.flow.watch_offers(True)
        self.desk.flow.start_pairing()
        self.assertTrue(wait(lambda: fourth.flow.status()["offers"]))
        offer = fourth.flow.status()["offers"][0]
        fourth.flow.pair(offer["address"], offer["port"], self.desk.flow.status()["pairing"]["code"])
        self.assertTrue(wait(lambda: fourth.linked() or len(fourth.cfg["peers"]) == 1))
        paired()
        self.assertTrue(wait(lambda: self.desk.entry(1)["id"] == fourth.cfg["id"]))  # the desk may take it in later
        self.assertEqual(len(self.desk.cfg["peers"]), flow.MAX_PEERS)  # the desk keeps two: the oldest made room
        self.assertEqual(self.desk.entry(1)["id"], fourth.cfg["id"])
        return fourth

    def test_a_third_pairing_is_not_taken(self):
        self.third_pairing()

    def test_a_third_pairing_is_not_taken_when_the_desk_takes_it_in_last(self):
        late, take_in = [], self.desk.flow._paired
        self.desk.flow._paired = late.append  # the desk's side of the pairing, held until the fourth has its own

        def then():
            self.assertTrue(wait(lambda: late))
            self.assertNotEqual(self.desk.entry(1)["id"], late[0]["id"])  # not taken in yet
            take_in(late.pop())
        fourth = self.third_pairing(then)
        self.assertEqual(late, [])
        self.assertTrue(fourth.cfg["peers"])


if __name__ == "__main__":
    unittest.main()
