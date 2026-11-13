"""Settings a keyboard keeps itself, as far as MX Options reads and changes them: how each is asked for, what an
answer must look like to count, and how a new value is sent. Each function here is one request of a task
(keyboard.KeyboardSide), made on the keyboard's thread with ``dev`` first; none of them decides anything.

Layouts are restated as facts from two public descriptions that agree: Solaar 1.1.20
(lib/logitech_receiver/settings_templates.py, its Fn swap settings and the way it finds the current host) and
OpenLogi v0.9.0 (crates/openlogi-hidpp/src/feature/fn_inversion.rs, crates/openlogi-device/src/write/fn_lock.rs;
for the keys turned off, feature/disable_keys.rs and write/disable_keys.rs; for the operating system layout,
feature/multi_platform.rs, feature/dual_platform.rs and write/os_layout.rs). No code of either is used. Only answers that fit the layout exactly count; anything else is BadReply, and
nothing is sent on the strength of it."""

from . import device

FN = "fn"
DISABLE = "disable"
PLATFORM = "platform"


class Unsupported(Exception):
    """The keyboard does not offer this, or not in a way the layouts here cover; nothing is sent."""


class BadReply(Exception):
    """An answer that does not fit the layout."""


def current_host(dev, hosts_current):
    """The Easy-Switch channel this computer is on, as HOSTS_INFO function 0 says in byte 3 of its answer (both
    sources), and only when that agrees with the channel CHANGE_HOST gave when the keyboard was found
    (``hosts_current``) and is one the keyboard has. Unsupported otherwise."""
    if not dev.has(device.HOSTS_INFO):
        raise Unsupported("no host information")
    reply = dev.call(device.HOSTS_INFO, 0)
    if len(reply) < 4 or reply[3] >= dev.host_count or reply[3] != hosts_current:
        raise Unsupported("the current channel is not certain")
    return reply[3]


def _state(byte):
    """Fn inversion state byte: 0 the bare top-row key gives F1-F12, 1 it gives its other function (both sources).
    True for the first; BadReply for anything but 0 or 1."""
    if byte not in (0, 1):
        raise BadReply("Fn state is not 0 or 1")
    return byte == 0


class FnCodec:
    """Fn lock: whether the top row gives F1-F12 without Fn (value True) or its other functions. Three features
    carry it; the first the keyboard lists is used.

    0x40A3 (one setting per Easy-Switch channel): function 0 with the channel answers that channel (byte 0), the
    state (byte 1) and the keyboard's default state (byte 2); function 1 with the channel and a state sets it.
    0x40A2: function 0 answers the state (byte 0) and the default (byte 1); function 1 with a state sets it.
    0x40A0: function 0 answers the state (byte 0; only 0 or 1 is taken, as the sources read it differently
    above that); function 1 with a state sets it. The channel is always named, never 0xFF ("this one"), which
    some firmware does not take for a change."""

    name = FN
    features = (device.FN_INVERSION_HOSTS, device.FN_INVERSION_DEFAULT, device.FN_INVERSION)

    @staticmethod
    def _feature(dev):
        return next((feature for feature in FnCodec.features if dev.has(feature)), None)

    @staticmethod
    def supported(dev):
        feature = FnCodec._feature(dev)
        return feature is not None and (feature != device.FN_INVERSION_HOSTS or dev.has(device.HOSTS_INFO))

    @staticmethod
    def needs_host(dev):
        return FnCodec._feature(dev) == device.FN_INVERSION_HOSTS

    @staticmethod
    def valid(value):
        return type(value) is bool

    @staticmethod
    def read(dev, host):
        """{"value": bool, "default": bool | None}. BadReply for an answer that does not fit."""
        feature = FnCodec._feature(dev)
        if feature == device.FN_INVERSION_HOSTS:
            reply = dev.call(feature, 0, host)
            if len(reply) < 3 or reply[0] != host:
                raise BadReply("not the channel asked about")
            state, default = reply[1], reply[2]
        else:
            reply = dev.call(feature, 0)
            if len(reply) < 1:
                raise BadReply("empty")
            state = reply[0]
            default = reply[1] if feature == device.FN_INVERSION_DEFAULT and len(reply) > 1 else None
        return {"value": _state(state), "default": default == 0 if default in (0, 1) else None}

    @staticmethod
    def write(dev, host, value):
        """Send the setter alone; what it did is for a fresh read to say."""
        feature = FnCodec._feature(dev)
        state = 0 if value else 1
        if feature == device.FN_INVERSION_HOSTS:
            dev.call(feature, 1, host, state)
        else:
            dev.call(feature, 1, state)


# DISABLE_KEYS (0x4521) bits, one key each, as both sources name them; the order is the one they are shown in
DISABLE_KEYS = (("caps_lock", 0x01), ("num_lock", 0x02), ("scroll_lock", 0x04), ("insert", 0x08), ("super", 0x10))
# MULTI_PLATFORM (0x4531) descriptor bits, one operating system each (both sources); the order names are given in
OS_BITS = (("windows", 0x0100), ("macos", 0x2000), ("linux", 0x0400), ("chromeos", 0x0800), ("android", 0x1000),
           ("ios", 0x4000), ("webos", 0x8000), ("windows_embedded", 0x0200), ("tizen", 0x0001))
MAX_DESCRIPTORS = 16  # a keyboard saying it has more is not believed
DUAL_CHOICES = ({"index": 0, "os": ["ios", "macos"]}, {"index": 1, "os": ["android", "windows"]})  # DUAL_PLATFORM byte 0


class DisableCodec:
    """Keys turned off: a key whose bit is set does nothing. Function 0 answers which keys can be (byte 0), function 1
    which are (byte 0), function 2 with a whole set of bits replaces it (both sources). The value is that set; a
    change asked for names keys only ({name: off}), and is laid over what a fresh read says, so a bit nobody asked
    about, named here or not, goes back as it was read. A set with a bit the keyboard says cannot be is not believed
    (both sources would clear it): nothing is sent then."""

    name = DISABLE
    features = (device.DISABLE_KEYS,)
    _BITS = dict(DISABLE_KEYS)

    @staticmethod
    def supported(dev):
        return dev.has(device.DISABLE_KEYS)

    @staticmethod
    def needs_host(dev):
        return False

    @staticmethod
    def valid(value):
        return (type(value) is dict and 1 <= len(value) <= len(DISABLE_KEYS)
                and all(type(key) is str and key in DisableCodec._BITS and type(on) is bool for key, on in value.items()))

    @staticmethod
    def merge(old, new):
        """One change asked for over another not sent yet: both keys go, the later word on a key counts."""
        return {**(old or {}), **new}

    @staticmethod
    def read(dev, host):
        """{"value": the bits set, "caps": the bits that can be}. BadReply for an answer that does not fit."""
        caps = dev.call(device.DISABLE_KEYS, 0)
        if len(caps) < 1:
            raise BadReply("empty")
        current = dev.call(device.DISABLE_KEYS, 1)
        if len(current) < 1:
            raise BadReply("empty")
        if current[0] & ~caps[0]:
            raise BadReply("a key turned off that cannot be")
        return {"value": current[0], "caps": caps[0]}

    @staticmethod
    def target(before, request):
        """The whole set to send: ``before`` with the keys asked about changed. Unsupported for a key it lacks."""
        mask = before["value"]
        for key, off in request.items():
            bit = DisableCodec._BITS[key]
            if not bit & before["caps"]:
                raise Unsupported("a key it cannot turn off")
            mask = mask | bit if off else mask & ~bit
        return mask

    @staticmethod
    def write(dev, host, mask):
        dev.call(device.DISABLE_KEYS, 2, mask)

    @staticmethod
    def public(result):
        """What is shown: the named keys turned off, and those that can be. No bits."""
        return {"value": [key for key, bit in DISABLE_KEYS if result["value"] & bit],
                "keys": [key for key, bit in DISABLE_KEYS if result["caps"] & bit]}


class PlatformCodec:
    """The operating system the keyboard lays its keys out for, on the Easy-Switch channel this computer is on. The
    value is a platform index; each platform covers the operating systems of its descriptors.

    0x4531: function 0 answers flags (byte 0, bit 1: software may set the platform), the number of platforms
    (byte 2), of descriptors (byte 3; one source says byte 2, the other and the kernel patch byte 3, which is used),
    of channels (byte 4), the channel asked about (byte 5) and its platform (byte 6). Function 1 with a descriptor
    index answers its platform (byte 0), the index again (byte 1) and its operating systems (bytes 2-3, big-endian).
    Function 3 with a channel and a platform sets it; the channel is named, never 0xFF. 0x4530, only where 0x4531 is
    not listed: function 0 answers 0 (iOS, macOS) or 1 (Android, Windows); never set here, as the sources disagree
    on the function that does. Anything not as laid out here leaves the platform unknown, and never set."""

    name = PLATFORM
    features = (device.MULTI_PLATFORM, device.DUAL_PLATFORM)

    @staticmethod
    def _feature(dev):
        return next((feature for feature in PlatformCodec.features if dev.has(feature)), None)

    @staticmethod
    def supported(dev):
        return PlatformCodec._feature(dev) is not None

    @staticmethod
    def needs_host(dev):
        """The channel is asked for where it can be: without HOSTS_INFO the platform is only shown."""
        return PlatformCodec._feature(dev) == device.MULTI_PLATFORM and dev.has(device.HOSTS_INFO)

    @staticmethod
    def valid(value):
        return type(value) is int and 0 <= value <= 0xFE

    @staticmethod
    def read(dev, host):
        """{"value": platform index, "choices": [{"index", "os": [names]}], "writable": bool}. BadReply for an
        answer that does not fit, Unsupported for a number of descriptors not believed."""
        if PlatformCodec._feature(dev) == device.DUAL_PLATFORM:
            reply = dev.call(device.DUAL_PLATFORM, 0)
            if len(reply) < 1 or reply[0] not in (0, 1):
                raise BadReply("not 0 or 1")
            return {"value": reply[0], "choices": [dict(choice, os=list(choice["os"])) for choice in DUAL_CHOICES],
                    "writable": False}
        info = dev.call(device.MULTI_PLATFORM, 0)
        if len(info) < 7:
            raise BadReply("short")
        platforms, count, hosts, slot, current = info[2], info[3], info[4], info[5], info[6]
        if not 1 <= count <= MAX_DESCRIPTORS:
            raise Unsupported("descriptors not believed")
        if host is not None and (slot != host or slot >= hosts):
            raise BadReply("not the channel asked about")
        covers = {}
        for index in range(count):
            row = dev.call(device.MULTI_PLATFORM, 1, index)
            if len(row) < 8 or row[1] != index or row[0] >= platforms:
                raise BadReply("not the descriptor asked for")
            mask = row[2] << 8 | row[3]
            if not mask:
                raise BadReply("a descriptor for no system")
            covers[row[0]] = covers.get(row[0], 0) | mask
        if current not in covers:
            raise BadReply("no platform it has")
        return {"value": current, "writable": host is not None and bool(info[0] & 0x02),
                "choices": [{"index": index, "os": [name for name, bit in OS_BITS if covers[index] & bit]}
                            for index in sorted(covers)]}

    @staticmethod
    def target(before, request):
        """``request`` itself, where the keyboard lets it be set and has it. Unsupported otherwise."""
        if not before.get("writable") or request not in {choice["index"] for choice in before["choices"]}:
            raise Unsupported("not a platform it lets be set")
        return request

    @staticmethod
    def write(dev, host, index):
        if PlatformCodec._feature(dev) != device.MULTI_PLATFORM or host is None:
            raise Unsupported("not set here")
        dev.call(device.MULTI_PLATFORM, 3, host, index)

    @staticmethod
    def public(result):
        return {"value": result["value"], "choices": [dict(choice, os=list(choice["os"])) for choice in result["choices"]]}


CODECS = {FN: FnCodec, DISABLE: DisableCodec, PLATFORM: PlatformCodec}
