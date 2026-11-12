"""Settings a keyboard keeps itself, as far as MX Options reads and changes them: how each is asked for, what an
answer must look like to count, and how a new value is sent. Each function here is one request of a task
(keyboard.KeyboardSide), made on the keyboard's thread with ``dev`` first; none of them decides anything.

Layouts are restated as facts from two public descriptions that agree: Solaar 1.1.20
(lib/logitech_receiver/settings_templates.py, its Fn swap settings and the way it finds the current host) and
OpenLogi v0.9.0 (crates/openlogi-hidpp/src/feature/fn_inversion.rs, crates/openlogi-device/src/write/fn_lock.rs).
No code of either is used. Only answers that fit the layout exactly count; anything else is BadReply, and
nothing is sent on the strength of it."""

from . import device

FN = "fn"


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


CODECS = {FN: FnCodec}
