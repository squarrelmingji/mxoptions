"""Finding HID++ nodes in sysfs, and the udev helper for Bluetooth mice that BlueZ announces before
it has read their vendor (bus 0005, vendor and product 0000). Needs no Qt and no device."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mxoptions import hid_linux  # noqa: E402

HELPER = hid_linux.__file__

# Captured on an MX Master 4 over Bluetooth (BlueZ 5.88) and on a keyboard of the same family.
REAL_MOUSE = bytes.fromhex(
    "05010902a10185020901a10095077501150025010509190129078102950981030501a49502750c1600f826ff07"
    "3600f846ff07550d6513093009318106b4950175081580257f09388106050c0a38028106c0c00643ff0a0202a1"
    "01851195137508150026ff000902810009029100c0050e0901a101853c0910a102050a1903290b16031026ff2f"
    "95097510b102c0050e0911a102050a1903290b469001660110550d15002690017510b102c0853d050e09214500"
    "650055001501250b95017504910209231500250475039102092425059102092546e803660110550d26e803750a"
    "91020928366009461815166009261815750d910275079103c0")
REAL_KEYBOARD = bytes.fromhex(
    "05010906a1018501050719e029e7150025017501950881029505050819012905910295017503910195067508"
    "150026a400050719002aa4008100c0050c0901a101850375109502150126ff0219012aff028160c005010913a1"
    "01850b950175011500250109e18106750f8103c00643ff0a0202a101851175089513150026ff000902810009"
    "029100c0")

MOUSE = bytes.fromhex(
    "05010902a10185020901a1000509190129101500250195107501810205011601f826ff07750c950209300931"
    "81061581257f750895010938 8106c0c0".replace(" ", ""))
HIDPP = bytes.fromhex("0643ff0a0202a101851175089513150026ff000902810009029100c0")
KBD = bytes.fromhex("05010906a1018501050719e029e71500250175019508 8102c0".replace(" ", ""))
KEYPAD = KBD.replace(b"\x09\x06", b"\x09\x07", 1)
GOOD = MOUSE + HIDPP
# A Keyboard or Keypad application collection after an unknown main item (tag 0xD0) that would
# hide it if only the known main items cleared the usages.
KBD_AFTER_D0 = bytes.fromhex("05010902d00906a101c0")
KEYPAD_AFTER_D0 = bytes.fromhex("05010902d00907a101c0")

ZERO_ID = "0005:00000000:00000000"


def uevent(hid_id, name="MX Master 4 B"):
    return "DRIVER=hid-generic\nHID_ID=%s\nHID_NAME=%s\nHID_PHYS=00:11:22:33:44:55\n" \
           "HID_UNIQ=00:11:22:33:44:55\nMODALIAS=hid:b0005g0001v00000000p00000000\n" % (hid_id, name)


class Descriptor(unittest.TestCase):
    def ok(self, desc):
        self.assertTrue(hid_linux.is_hidpp_mouse_ble(desc), desc.hex())

    def no(self, desc):
        self.assertFalse(hid_linux.is_hidpp_mouse_ble(desc), desc.hex())

    def test_real_captures(self):
        self.assertEqual(len(REAL_MOUSE), 250)
        self.assertEqual(len(REAL_KEYBOARD), 137)
        self.ok(REAL_MOUSE)       # mouse, HID++ and haptics collections; unknown pages are fine
        self.no(REAL_KEYBOARD)    # keyboard, consumer, system and HID++, no mouse
        self.no(REAL_KEYBOARD + REAL_MOUSE)
        self.no(REAL_MOUSE + REAL_KEYBOARD)

    def test_synthetic_mouse_either_order(self):
        self.ok(MOUSE + HIDPP)
        self.ok(HIDPP + MOUSE)

    def test_push_pop_restores_the_page(self):
        self.ok(bytes.fromhex("0501a4") + HIDPP + bytes.fromhex("b4") + MOUSE)

    def test_extended_usages(self):
        self.ok(bytes.fromhex("050c0b02000100") + MOUSE[4:] + HIDPP)        # 0x00010002 under Consumer
        self.no(bytes.fromhex("05010b02000c00") + MOUSE[4:] + HIDPP)        # 0x000C0002: not a mouse

    def test_items_the_specification_does_not_define(self):
        # Long items (the kernel refuses them), main tags other than Input, Output, Feature and
        # (End) Collection, global tags after Pop, local tag 0x68 and tags after Delimiter, and
        # the reserved item type.
        for item in ("fe0200aabb", "f0", "f4", "f8", "fc", "00", "d0", "e0", "70", "c4", "e4",
                     "68", "b8", "e8", "0c", "1d00", "ec"):
            self.no(MOUSE + bytes.fromhex(item) + HIDPP)
            self.no(REAL_MOUSE + bytes.fromhex(item))
        # Local items that are defined (designators, strings) are fine, and so are pages the
        # parser does not know (the real mouse has Haptics and Ordinal).
        self.ok(MOUSE + bytes.fromhex("390149015901790189019901") + HIDPP)
        self.ok(bytes.fromhex("050e0901a101c0050a0901a101c0") + GOOD)

    def test_every_main_item_clears_the_usages(self):
        for kbd in (KBD_AFTER_D0, KEYPAD_AFTER_D0):
            self.no(kbd + GOOD)
            self.no(GOOD + kbd)
            self.no(REAL_MOUSE + kbd)
        # Input, Output, Feature, Collection and End Collection between the Mouse usage and the
        # Keyboard or Keypad usage: the application collection is a keyboard to the kernel.
        for main in ("8100", "9100", "b100", "a100c0"):
            for usage in ("06", "07"):
                self.no(bytes.fromhex("05010902" + main + "09" + usage + "a101c0") + GOOD)

    def test_report_ids(self):
        for tail in ("8500", "860001", "86ffff", "8700010000", "87110000ff"):
            self.no(REAL_MOUSE + bytes.fromhex(tail))
            self.no(MOUSE + bytes.fromhex(tail) + HIDPP)
        self.ok(REAL_MOUSE + bytes.fromhex("85ff"))
        self.ok(MOUSE + HIDPP.replace(b"\x85\x11", b"\x87\x11\x00\x00\x00"))   # 32-bit Report ID

    def test_kernel_limits(self):
        self.ok(REAL_MOUSE + bytes.fromhex("760001"))                 # Report Size 256
        self.no(REAL_MOUSE + bytes.fromhex("760101"))                 # 257
        self.ok(REAL_MOUSE + bytes.fromhex("960030"))                 # Report Count 12288
        self.no(REAL_MOUSE + bytes.fromhex("960130"))                 # 12289
        self.ok(bytes.fromhex("0501" + "a4" * 4 + "b4" * 4) + GOOD)   # Push 4 deep
        self.no(bytes.fromhex("0501" + "a4" * 5 + "b4" * 5) + GOOD)   # 5 deep
        self.ok(bytes.fromhex("050119002aff2fa100c0") + GOOD)         # 12288 usages
        self.no(bytes.fromhex("050119002a0030a100c0") + GOOD)         # 12289
        self.no(bytes.fromhex("050119002aff2f0901a100c0") + GOOD)     # 12288 + 1
        self.ok(bytes.fromhex("050119002aff2fa100c00901a100c0") + GOOD)   # limit is per main item

    def test_usage_minimum_above_maximum(self):
        # The kernel adds usages from the minimum on; refuse it rather than guess.
        self.no(bytes.fromhex("050119062902a101c0") + GOOD)
        self.no(bytes.fromhex("050119062902") + GOOD)

    def test_keyboard_or_keypad_anywhere(self):
        for extra in (KBD, KEYPAD):
            self.no(extra + MOUSE + HIDPP)
            self.no(MOUSE + HIDPP + extra)
            self.no(MOUSE + extra + HIDPP)
        self.no(KBD.replace(b"\x09\x06", b"\x0b\x06\x00\x01\x00", 1) + MOUSE + HIDPP)  # 32-bit usage
        self.no(KBD)
        self.no(KBD + HIDPP)

    def test_keyboard_hidden_the_way_the_kernel_reads_it(self):
        # The kernel keeps the page of the first usage when a later one already has the current page.
        self.no(bytes.fromhex("0501090605 0c0901a101c0".replace(" ", "")) + GOOD)
        # Usage Minimum..Maximum makes usages too.
        self.no(bytes.fromhex("050119062906a101c0") + GOOD)
        self.no(bytes.fromhex("050119072907a101c0") + GOOD)
        # A usage page given after the usage is the one the kernel applies.
        self.no(bytes.fromhex("0906050 1a101c0".replace(" ", "")) + GOOD)

    def test_delimiter_is_refused(self):
        self.no(bytes.fromhex("a901a900") + GOOD)          # open and close, one byte each
        self.no(GOOD + bytes.fromhex("a8"))

    def test_truncated(self):
        for tail in ("26ff", "fe", "fe0500aa"):
            self.no(GOOD + bytes.fromhex(tail))

    def test_unbalanced(self):
        self.no(GOOD + b"\xc0")
        self.no(b"\xc0" + GOOD)
        self.no(b"\xc0" + GOOD + b"\xa1\x00")     # End Collection first, balanced by count only
        self.no(GOOD[:-1])
        self.no(b"\xb4" + GOOD)
        self.no(GOOD + b"\xa4")
        # report ID 0x11 after Pop is under the restored page 0000, not FF43
        self.no(bytes.fromhex("a40643ffb48511") + MOUSE)

    def test_garbage(self):
        self.no(b"")
        self.no(b"\xff" * 4096)
        self.no(b"\x05")

    def test_signature(self):
        self.no(MOUSE)
        self.no(MOUSE + HIDPP.replace(b"\x06\x43\xff", b"\x06\x00\xff"))     # USB vendor page
        self.no(MOUSE + bytes.fromhex("05012706 43ff008511"))                 # FF43 only as data
        self.no(MOUSE + HIDPP.replace(b"\x85\x11", b"\x85\x10"))

    def test_size_limit(self):
        padded = REAL_MOUSE + b"\x05\x01" * ((4096 - len(REAL_MOUSE)) // 2)
        self.assertEqual(len(padded), 4096)
        self.ok(padded)
        self.no(padded[:-2] + b"\x06\x01\x00")       # every item valid, but longer than the kernel allows
        self.no(REAL_MOUSE + b"\x05\x01" * 50000)


class Sysfs(unittest.TestCase):
    """A stand-in for /sys/class/hidraw."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        old = hid_linux.HIDRAW_SYS
        hid_linux.HIDRAW_SYS = self.root
        self.addCleanup(setattr, hid_linux, "HIDRAW_SYS", old)

    def node(self, name, hid_id, desc, hid_name="MX Master 4 B", raw_uevent=None):
        device = os.path.join(self.root, name, "device")
        os.makedirs(device)
        if raw_uevent is not None or hid_id is not None:
            with open(os.path.join(device, "uevent"), "w") as f:
                f.write(raw_uevent if raw_uevent is not None else uevent(hid_id, hid_name))
        if desc is not None:
            with open(os.path.join(device, "report_descriptor"), "wb") as f:
                f.write(desc)
        return os.path.join(self.root, name)

    def helper(self, *args, script=HELPER):
        return subprocess.run([sys.executable, "-I", "-B", script, *args], capture_output=True).returncode

    def accepted(self, path):
        verdict = hid_linux.zero_id_hidpp(path)
        self.assertEqual(self.helper(path), 0 if verdict else 1)
        return verdict

    def test_zero_id_mouse(self):
        a = self.node("hidraw3", ZERO_ID, REAL_MOUSE)
        b = self.node("hidraw4", ZERO_ID, GOOD)
        self.node("hidraw5", ZERO_ID, HIDPP + MOUSE)
        self.assertTrue(self.accepted(a))
        self.assertTrue(self.accepted(b))
        self.assertEqual(hid_linux.find_hidpp_nodes(),
                         [("/dev/hidraw3", False), ("/dev/hidraw4", False), ("/dev/hidraw5", False)])

    def test_046d_unchanged(self):
        self.node("hidraw0", "0005:0000046D:0000B048", REAL_MOUSE)
        self.node("hidraw1", "0003:0000046D:0000C548", bytes.fromhex("0600ff0901a1018511") + b"\xc0",
                  hid_name="Logitech USB Receiver")
        self.node("hidraw2", "0005:0000046D:0000B366", REAL_KEYBOARD, hid_name="MX MCHNCL")
        self.assertEqual(hid_linux.find_hidpp_nodes(),
                         [("/dev/hidraw0", False), ("/dev/hidraw1", True), ("/dev/hidraw2", False)])
        for n in ("hidraw0", "hidraw1", "hidraw2"):
            self.assertFalse(self.accepted(os.path.join(self.root, n)))

    def test_zero_id_refused(self):
        for i, desc in enumerate((REAL_KEYBOARD, KBD + GOOD, GOOD + KEYPAD, GOOD[:-1], b"",
                                  REAL_MOUSE + b"\x00" * 5000, REAL_MOUSE + b"\x05\x01" * 2000,
                                  KBD_AFTER_D0 + GOOD, KEYPAD_AFTER_D0 + GOOD, GOOD + KBD_AFTER_D0,
                                  REAL_MOUSE + bytes.fromhex("8500"), REAL_MOUSE + bytes.fromhex("860001"),
                                  MOUSE + bytes.fromhex("fe0200aabb") + HIDPP)):
            self.assertFalse(self.accepted(self.node("hidraw%d" % i, ZERO_ID, desc)), i)
        self.assertEqual(hid_linux.find_hidpp_nodes(), [])

    def test_descriptor_read_is_bounded(self):
        path = self.node("hidraw0", ZERO_ID, REAL_MOUSE + b"\x00" * 100000)
        self.assertEqual(len(hid_linux._descriptor(path)), hid_linux.MAX_DESCRIPTOR + 1)

    def test_wrong_ids(self):
        for i, hid_id in enumerate(("0003:00000000:00000000", "0005:00000000:0000B048",
                                    "0005:0000045E:00000000", "0005:045E:0000:0000", "garbage")):
            self.assertFalse(self.accepted(self.node("hidraw%d" % i, hid_id, REAL_MOUSE)), hid_id)
        self.node("hidraw9", None, REAL_MOUSE, raw_uevent="HID_NAME=MX Master 4 B\n")  # no HID_ID
        self.assertFalse(self.accepted(os.path.join(self.root, "hidraw9")))
        self.assertEqual(hid_linux.find_hidpp_nodes(), [])

    def test_missing_files(self):
        a = self.node("hidraw0", ZERO_ID, None)
        b = self.node("hidraw1", None, REAL_MOUSE)
        self.assertFalse(self.accepted(a))
        self.assertFalse(self.accepted(b))
        self.assertFalse(self.accepted(os.path.join(self.root, "nothing")))
        self.assertEqual(hid_linux.find_hidpp_nodes(), [])

    def test_helper_arguments(self):
        a = self.node("hidraw0", ZERO_ID, REAL_MOUSE)
        self.assertEqual(self.helper(a), 0)
        self.assertEqual(self.helper(), 1)
        self.assertEqual(self.helper(a, a), 1)

    def test_installed_copy_stands_alone_and_writes_nothing(self):
        a = self.node("hidraw0", ZERO_ID, REAL_MOUSE)
        with tempfile.TemporaryDirectory() as udev:
            copy = os.path.join(udev, "mxoptions-hidpp-check.py")
            with open(HELPER, "rb") as src, open(copy, "wb") as dst:
                dst.write(src.read())
            before = sorted(os.walk(self.root)), sorted(os.listdir(udev))
            self.assertEqual(self.helper(a, script=copy), 0)
            self.assertEqual(self.helper(os.path.join(self.root, "nothing"), script=copy), 1)
            self.assertEqual((sorted(os.walk(self.root)), sorted(os.listdir(udev))), before)


if __name__ == "__main__":
    unittest.main()
