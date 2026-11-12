"""HID devices on Linux: hidraw nodes (see hidpp.py, which talks through them).

A hidraw node carries every report of its interface, whatever its report ID, so one node serves
for all of HID++; a mouse's ordinary reports are on the same node (Bluetooth, cable) or on a sibling
node of the same receiver, and reading them only copies them: the desktop still gets them.

A keyboard on Bluetooth can also be asked, through the node already open, whether any of its keys is down
right now (Port.keys_held), for the keyboard following the mouse to another computer. Nothing else of what
it answers is kept: the answer is one yes or no."""

import fcntl
import glob
import logging
import os
import select

log = logging.getLogger(__name__)

LOGITECH_VID = 0x046D
# Asking a keyboard which of its keys are down. Each ask is one read of a report over Bluetooth, which
# bluetoothd records for the life of the keyboard's input device and replays at every reconnect (BlueZ
# src/shared/uhid.c): so there is a limit to how often a node is asked.
KEY_REPORTS_MAX = 4      # most key reports read per check; a keyboard with more is not asked at all
IOCTLS_PER_PORT = 32     # most reads per open node, failed ones too; in memory only, a new Port starts again
_HIDPP_REPORTS = {0x10: 7, 0x11: 20}  # report id -> bytes with the id: the HID++ short and long reports (hidpp.py)
_HIDPP_PAGES = (0xFF00, 0xFF43)       # the vendor pages HID++ nodes are found by (find_hidpp_nodes)
_UNSET = object()


def HIDIOCGINPUT(length):  # noqa: N802 - the name of the request in linux/hidraw.h
    """The ioctl request that reads the input report byte 0 names into a buffer of ``length`` bytes."""
    return (3 << 30) | (length << 16) | (ord("H") << 8) | 0x0A


def _system_usages(usages):
    """Generic Desktop usages that are keys: System Control up to the System Display ones (0x80-0xB7)."""
    return bool(usages) and all(0x80 <= low <= high <= 0xB7 for low, high in usages)


def key_reports(desc):
    """The input reports of a keyboard's descriptor that carry its keys: ((report id, 0 when it has none;
    bytes of the report with its id; mask of the key bits after the id), ...) in the order they are
    described. None for anything not exactly understood, or with any input that is neither a key nor
    HID++: such a keyboard is not asked at all, as a key there could be down unseen.

    A key field is on the Keyboard (0x07) or Consumer (0x0C) page, or Generic Desktop 0x80-0xB7 (system
    keys). Each of its bits is 0 while no key is down: a one-bit field 0 to 1, or a list of keys down whose
    value 0 means "none" (HID 1.11, 6.2.2.5: a value out of the logical range is no control; usage 0 of
    pages 0x07 and 0x0C is reserved, HID Usage Tables 1.6, sections 10 and 15). A HID++ report (vendor page,
    id and length of hidpp.py's own) is left out: it carries this program's own requests and answers, and the
    keys diverted to it are followed there."""
    page = logical_min = logical_max = size = count = 0
    report, numbered, depth = None, False, 0
    usages, low, fields = [], None, []   # usages: [(first, last)] of the main item to come, in order
    i = 0
    while i < len(desc):
        head = desc[i]
        if head == 0xFE:
            return None  # long item
        length = (0, 1, 2, 4)[head & 3]
        kind, tag = (head >> 2) & 3, head >> 4
        if i + 1 + length > len(desc):
            return None
        data = desc[i + 1:i + 1 + length]
        value = int.from_bytes(data, "little")
        i += 1 + length
        if kind == 1:  # global
            if tag == 0:
                page = value
            elif tag == 1:
                logical_min = int.from_bytes(data, "little", signed=True)
            elif tag == 2:
                logical_max = value
            elif tag in (3, 4, 5, 6):  # physical range, unit exponent, unit
                pass
            elif tag == 7:
                size = value
            elif tag == 8:
                if not 1 <= value <= 255:
                    return None
                report, numbered = value, True
            elif tag == 9:
                count = value
            else:
                return None  # push, pop, reserved
        elif kind == 2:  # local
            if tag in (0, 1, 2):
                if length == 4:
                    return None  # a usage with its page in it
                if tag == 0:
                    usages.append((value, value))
                elif tag == 1:
                    if low is not None:
                        return None
                    low = value
                else:
                    if low is None or low > value:
                        return None
                    usages.append((low, value))
                    low = None
            elif tag not in (3, 4, 5, 7, 8, 9):  # designators and strings mean nothing here; a delimiter does
                return None
        elif kind == 0:  # main
            if low is not None:
                return None
            if tag == 8:  # input
                if not (1 <= size <= 32 and 1 <= count <= 255):
                    return None
                fields.append((report, size, count, value, page, logical_min, logical_max, usages))
            elif tag == 10:  # collection
                depth += 1
                if depth > 16:
                    return None
            elif tag == 12:  # end collection
                if depth == 0:
                    return None
                depth -= 1
            elif tag not in (9, 11):  # output and feature: not what is read
                return None
            usages = []
        else:
            return None  # reserved item type
    if depth or (numbered and any(field[0] is None for field in fields)):
        return None
    reports = {}  # report id -> what its input fields come to
    for rid, size, count, flags, page, logical_min, logical_max, usages in fields:
        found = reports.setdefault(rid or 0, {"bits": 0, "mask": 0, "keys": False, "hidpp": False, "keyboard": False})
        start, width = found["bits"], size * count
        found["bits"] += width
        if flags & 0x01:
            continue  # constant: padding
        if page in _HIDPP_PAGES:
            found["hidpp"] = True
            continue
        if page in (0x07, 0x0C) or (page == 0x01 and _system_usages(usages)):
            if flags & 0x02:  # variable: one bit a key, 1 while down
                if size != 1 or logical_min != 0 or logical_max != 1:
                    return None
            elif not (1 <= size <= 16 and (logical_min >= 1 or (
                    logical_min == 0 and page in (0x07, 0x0C) and usages and usages[0][0] == 0))):
                return None  # a list of the keys down where 0 could name a key, or a value below 0
            found["mask"] |= ((1 << width) - 1) << start
            found["keys"] = True
            found["keyboard"] = found["keyboard"] or page == 0x07
            continue
        return None  # a pointer, buttons, a battery, another vendor's report: not a keyboard to ask
    out = []
    for rid, found in reports.items():
        if found["hidpp"]:
            if found["keys"] or found["bits"] % 8 or _HIDPP_REPORTS.get(rid) != found["bits"] // 8 + 1:
                return None
            continue
        if not found["keys"]:
            continue  # padding only
        total = found["bits"] // 8 + (1 if numbered else 0)
        if found["bits"] % 8 or total > 64:
            return None
        out.append((rid, total, found["mask"], found["keyboard"]))
    if not any(entry[3] for entry in out) or len(out) > KEY_REPORTS_MAX:
        return None
    return tuple(entry[:3] for entry in out)


def _keys_layout(path):
    """key_reports() of a node, if it is a Bluetooth LE device that BlueZ runs through uhid: the one way
    whose answers to HIDIOCGINPUT are known to be the report exactly as described, with its id first when
    it has one (Linux drivers/hid/uhid.c and hidraw.c, BlueZ src/shared/hog.c). None otherwise."""
    device = os.path.join("/sys/class/hidraw", os.path.basename(path), "device")
    try:
        with open(os.path.join(device, "uevent")) as f:
            uevent = dict(line.strip().split("=", 1) for line in f if "=" in line)
        bus = int(uevent.get("HID_ID", "").split(":")[0], 16)
        real = os.path.realpath(device)
    except (OSError, ValueError):
        return None
    if bus != 0x0005 or "/devices/virtual/misc/uhid/" not in real + "/":
        return None
    return key_reports(read_descriptor(path))


def find_hidpp_nodes():
    """Return [(devnode, is_receiver)] for Logitech hidraw nodes that speak HID++ long reports."""
    nodes = []
    for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(sys_path, "device", "uevent")) as f:
                uevent = dict(line.strip().split("=", 1) for line in f if "=" in line)
            bus, vid, pid = (int(x, 16) for x in uevent.get("HID_ID", "0:0:0").split(":"))
            if vid != LOGITECH_VID:
                continue
            with open(os.path.join(sys_path, "device", "report_descriptor"), "rb") as f:
                desc = f.read()
        except (OSError, ValueError):
            continue
        # Vendor usage page (FF00 on USB, FF43 on Bluetooth) carrying report ID 0x11.
        vendor_page = b"\x06\x00\xff" in desc or b"\x06\x43\xff" in desc
        if not vendor_page or b"\x85\x11" not in desc:
            continue
        is_receiver = bus == 0x0003 and "receiver" in uevent.get("HID_NAME", "").lower()
        nodes.append((os.path.join("/dev", os.path.basename(sys_path)), is_receiver))
    return nodes


def find_mouse_report_node(hidpp_path):
    """The receiver exposes ordinary mouse reports on a sibling hidraw node; return its path or None."""
    def usb_device(name):
        return os.path.realpath(os.path.join("/sys/class/hidraw", name, "device", "..", ".."))

    own = os.path.basename(hidpp_path)
    try:
        parent = usb_device(own)
        for sys_path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
            name = os.path.basename(sys_path)
            if name == own or usb_device(name) != parent:
                continue
            with open(os.path.join(sys_path, "device", "report_descriptor"), "rb") as f:
                desc = f.read()
            # Generic Desktop / Mouse collection that uses report ID 2 (the receivers' fixed layout)
            if desc.startswith(b"\x05\x01\x09\x02") and b"\x85\x02" in desc[:12]:
                return os.path.join("/dev", name)
    except OSError:
        pass
    return None


def read_descriptor(node):
    """The HID report descriptor of a hidraw node (b"" if it cannot be read)."""
    try:
        with open(os.path.join("/sys/class/hidraw", os.path.basename(node), "device", "report_descriptor"), "rb") as f:
            return f.read()
    except OSError:
        return b""


def short_name(path):
    return os.path.basename(path)


class Port:
    """One open node. ``read()`` waits for the next report and gives b"" once the port is closed or the
    device is gone; ``write()`` raises OSError if the report cannot be sent."""

    def __init__(self, path, write=True):
        self._fd = os.open(path, (os.O_RDWR if write else os.O_RDONLY) | os.O_NONBLOCK)
        self._wake_r, self._wake_w = os.pipe()
        self._closed = False
        self._path = path
        self._layout = _UNSET  # keys_layout(), once looked up
        self._ioctls = 0       # HIDIOCGINPUT asked on this node so far (see IOCTLS_PER_PORT)
        self._capped = False   # ... and the limit was reached, which the log was told

    def keys_layout(self):
        """The key reports to read for keys_held(), or None when this node is not one to ask (see _keys_layout)."""
        if self._layout is _UNSET:
            self._layout = _keys_layout(self._path)
        return self._layout

    def keys_held(self, stop):
        """Whether a key of the keyboard is down now: True, False, or None when that cannot be told. Reads each of
        its key reports once, through this node (never another): stops with None as soon as ``stop()`` says so,
        before the next read. False only when every one was read in full and has no key down. Blocks for as long as
        the reads take, up to 5 s each in the kernel: never call it from a thread that must not wait. What is read
        is wiped at once; only the answer leaves."""
        reports = self.keys_layout()
        if reports is None:
            return None
        if self._ioctls + len(reports) > IOCTLS_PER_PORT:
            if not self._capped:
                self._capped = True
                log.info("%s: asked which keys are down %d times already: not asked again until it is opened anew",
                         short_name(self._path), self._ioctls)
            return None
        for rid, total, mask in reports:
            if stop() or self._closed:
                return None
            buf = bytearray(total + 1)  # one byte more than the report: a longer answer shows
            buf[0] = rid
            self._ioctls += 1  # counted before: a read that fails was still asked of the keyboard
            try:
                try:
                    got = fcntl.ioctl(self._fd, HIDIOCGINPUT(len(buf)), buf, True)
                except OSError:
                    return None
                if got != total or (rid and buf[0] != rid):
                    return None
                with memoryview(buf) as view:
                    down = int.from_bytes(view[1:total] if rid else view[:total], "little") & mask
                if down:
                    return True
            finally:
                buf[:] = bytes(len(buf))
        return False

    def read(self):
        while not self._closed:
            try:
                readable, _, _ = select.select([self._fd, self._wake_r], [], [])
                if self._closed:
                    break
                if self._fd in readable:
                    return os.read(self._fd, 64)
            except (OSError, ValueError):
                break
        return b""

    def write(self, packet):
        os.write(self._fd, packet)

    def wake(self):
        """Make a waiting read() return (called before close(), from another thread)."""
        self._closed = True
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def close(self):
        self._closed = True
        for fd in (self._fd, self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
