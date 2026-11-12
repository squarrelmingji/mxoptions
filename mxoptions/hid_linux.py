"""HID devices on Linux: hidraw nodes (see hidpp.py, which talks through them).

A hidraw node carries every report of its interface, whatever its report ID, so one node serves
for all of HID++; a mouse's ordinary reports are on the same node (Bluetooth, cable) or on a sibling
node of the same receiver, and reading them only copies them: the desktop still gets them."""

import glob
import os
import select
import sys

LOGITECH_VID = 0x046D
HIDRAW_SYS = "/sys/class/hidraw"
MAX_DESCRIPTOR = 4096  # HID_MAX_DESCRIPTOR_SIZE: the kernel refuses anything longer
MAX_USAGES = 12288  # HID_MAX_USAGES


def _identity(sys_path):
    """(bus, vendor, product, uevent) of a hidraw sysfs directory. Raises OSError or ValueError."""
    with open(os.path.join(sys_path, "device", "uevent"), encoding="utf-8", errors="replace") as f:
        uevent = dict(line.strip().split("=", 1) for line in f.read(4096).splitlines() if "=" in line)
    bus, vid, pid = (int(x, 16) for x in uevent.get("HID_ID", "0:0:0").split(":"))
    return bus, vid, pid, uevent


def _descriptor(sys_path):
    """At most MAX_DESCRIPTOR + 1 bytes, so that a longer one shows. Raises OSError."""
    with open(os.path.join(sys_path, "device", "report_descriptor"), "rb") as f:
        return f.read(MAX_DESCRIPTOR + 1)


def is_hidpp_mouse_ble(desc):
    """Return True only if desc is a well-formed report descriptor of a HID++ mouse.

    Every item must be one that the HID 1.11 specification defines, complete within desc, and
    within the limits that the kernel enforces (hid-core.c): report IDs 1 to 255, Report Size up
    to 256, Report Count and the usages of one main item up to 12288, Push at most 4 deep.
    Collections and Push/Pop must balance. There must be a Generic Desktop Mouse application
    collection and report ID 0x11 declared under usage page 0xFF43, and no application collection
    may be a Generic Desktop Keyboard or Keypad. Usages are resolved as the kernel resolves them
    (complete_usage, hid_concatenate_last_usage_page, open_collection), and every main item
    clears them, as hid_parser_main does.

    This is used where BlueZ has not yet given the vendor. It is what the device says about
    itself, not proof of what it is."""
    if not desc or len(desc) > MAX_DESCRIPTOR:
        return False
    page, stack, depth, usages, count, usage_min = 0, [], 0, [], 0, 0
    mouse = hidpp = keyboard = False
    i, n = 0, len(desc)
    while i < n:
        b = desc[i]
        size = (0, 1, 2, 4)[b & 3]
        if i + 1 + size > n:
            return False
        value = int.from_bytes(desc[i + 1:i + 1 + size], "little")
        kind, tag = b & 0x0C, b & 0xFC
        i += 1 + size
        if kind == 0x04:  # global items
            if tag == 0x04:  # Usage Page
                page = value
            elif tag == 0x74:  # Report Size
                if value > 256:
                    return False
            elif tag == 0x84:  # Report ID
                if not 0 < value < 256:
                    return False
                hidpp = hidpp or (value == 0x11 and page == 0xFF43)
            elif tag == 0x94:  # Report Count
                if value > MAX_USAGES:
                    return False
            elif tag == 0xA4:  # Push
                if len(stack) == 4:
                    return False
                stack.append(page)
            elif tag == 0xB4:  # Pop
                if not stack:
                    return False
                page = stack.pop()
            elif tag > 0xB4:  # not defined
                return False
        elif kind == 0x08:  # local items
            if tag in (0x08, 0x28):  # Usage; Usage Maximum adds Usage Minimum to Maximum
                added = 1
                if tag == 0x28:
                    if usage_min > value:
                        return False
                    added, value = value - usage_min + 1, usage_min
                count += added
                if count > MAX_USAGES:
                    return False
                # A range is kept as its first usage: all of its usages share one page and size.
                usages.append([value if size > 2 else (value & 0xFFFF) | (page & 0xFFFF) << 16, size])
            elif tag == 0x18:  # Usage Minimum
                usage_min = value
            elif tag == 0x68 or tag >= 0xA8:  # not defined; Delimiter, never seen on these mice
                return False
        elif tag in (0x80, 0x90, 0xA0, 0xB0, 0xC0):  # main items
            for u in reversed(usages):
                if u[1] > 2:
                    continue
                if u[0] >> 16 == page:
                    break
                u[0] = (u[0] & 0xFFFF) | (page & 0xFFFF) << 16
            if tag == 0xA0:
                depth += 1
                if value & 0xFF == 0x01:  # Application
                    usage = usages[0][0] if usages else 0
                    mouse = mouse or usage == 0x00010002
                    keyboard = keyboard or usage in (0x00010006, 0x00010007)
            elif tag == 0xC0:
                if depth == 0:
                    return False
                depth -= 1
            usages, count, usage_min = [], 0, 0
        else:  # a main item tag that is not defined, the reserved item type, or a long item (0xFE)
            return False
    return depth == 0 and not stack and mouse and hidpp and not keyboard


def zero_id_hidpp(sys_path):
    """Return True for a Bluetooth hidraw node whose vendor and product are still 0 (BlueZ created
    it before reading the PnP ID) and whose report descriptor passes is_hidpp_mouse_ble. This only
    reads sysfs."""
    try:
        bus, vid, pid, _ = _identity(sys_path)
        return bus == 0x0005 and vid == 0 and pid == 0 and is_hidpp_mouse_ble(_descriptor(sys_path))
    except (OSError, ValueError):
        return False


def find_hidpp_nodes():
    """Return [(devnode, is_receiver)] for Logitech hidraw nodes that speak HID++ long reports."""
    nodes = []
    for sys_path in sorted(glob.glob(os.path.join(HIDRAW_SYS, "hidraw*"))):
        try:
            bus, vid, pid, uevent = _identity(sys_path)
            if vid != LOGITECH_VID and not (bus == 0x0005 and vid == 0 and pid == 0):
                continue
            desc = _descriptor(sys_path)
        except (OSError, ValueError):
            continue
        if vid != LOGITECH_VID:  # Bluetooth, before BlueZ has read the vendor: only the strict check
            if not is_hidpp_mouse_ble(desc):
                continue
        else:
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


if __name__ == "__main__":
    # The udev helper of 70-mxoptions.rules is a root-owned copy of this file, run as
    # "python3 -I -B <copy> /sys/<devpath of the hidraw node>". It reads sysfs and writes nothing.
    # Exit status 0 grants access; 1 does not.
    try:
        ok = len(sys.argv) == 2 and zero_id_hidpp(sys.argv[1])
    except Exception:
        ok = False
    sys.exit(0 if ok else 1)
