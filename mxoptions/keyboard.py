"""The keyboard: found next to the mouse (on the same receiver, or on a connection of its own), its
programmable keys turned into actions, its battery and Easy-Switch channels shown.

What is known of the keyboard is kept, and decided on, on the engine's thread, like the mouse. Every request
to the keyboard is made on a thread of its own, though, in the background: it gives way to every other
request (hidpp.ProbeTransport), so that a slow or sleeping keyboard never holds up the mouse. Looking for
the keyboard is done there in one go; the rest (its keys diverted or given back, Easy-Switch) is a task the
engine's thread runs one request at a time, going on with it when the answer comes back (KeyboardSide._step).

A key is only ever diverted with the temporary setting, never the persistent one the keyboard keeps, and
only keys this program diverted itself are given back. Which ones those are is written down before each
one is diverted, in a small file of this computer's (JOURNAL), so that a start after a crash knows them
again. What the keyboard says is what counts; the file only says what was meant. Where it cannot be told
(the keyboard does not say which unit it is, the file cannot be read, other software for the device is
running) no key is touched at all."""

import copy
import hashlib
import json
import logging
import os
import queue
import re
import struct
import threading
import time

from . import config, device, hidpp, paths
from .actions import Executor, here
from .hidpp import HidppCancelled, HidppClosed, HidppError, HidppTimeout

log = logging.getLogger(__name__)

# Not measured on a real keyboard yet: knobs to set from what a receiver shared with the mouse shows.
PROBE_TIMEOUT_SHARED = 0.25  # s for an answer, on a transport that carries the mouse too
PROBE_TIMEOUT_OWN = 1.0      # s for an answer, on one of the keyboard's own
PROBE_BUDGET = 20.0          # s one look may take in all (loading a keyboard is a couple of hundred requests)
ASLEEP_RETRY = 60.0          # s before slots that did not answer, or were not reached, are asked again
NODE_INTERVAL = 10.0         # s between two looks for new receivers and Bluetooth devices (no HID++ traffic)
RECOVERY_RETRY = 30.0        # s between two attempts at keys whose state could not be settled
REQUEST_BUDGET = 5.0         # s one request of a task may wait for its turn and its answer, in all
SHUTDOWN_BUDGET = 3.0        # s the keys may take to be given back on exit
SWITCH_GRACE = 5.0           # s after an Easy-Switch request before keys are taken again if the keyboard stayed
LOW_BATTERY = 15             # percent: where the battery in the sidebar turns red
MAX_CONTROLS = 128
MAX_JOURNAL_KEYBOARDS = 16
JOURNAL = os.path.join(paths.DATA, "keyboard-diverted.json")
IDENTITY = re.compile(r"[0-9a-f]{16}\Z")


def flow_link(identity):
    """What Flow knows this keyboard by (see flow_service.py), or None for one that does not say which unit it is.
    Made from the identity, never the identity itself; it never leaves this program as it is."""
    return hashlib.sha256(b"mxoptions keyboard flow\0" + identity.encode("ascii")).hexdigest()[:16] if identity else None


class JournalUnavailable(Exception):
    """The journal is there but cannot be read, or is not in the one form this version knows."""


def read_journal(path=None):
    """The keys this program diverted, by keyboard: {identity: [key ids]}, {} if there is no journal.
    Raises JournalUnavailable for anything else: the file is then left exactly as it is."""
    path = path or JOURNAL
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise JournalUnavailable(str(e)) from None
    keyboards = data.get("keyboards") if isinstance(data, dict) else None
    if (not isinstance(keyboards, dict) or set(data) != {"version", "keyboards"} or type(data["version"]) is not int
            or data["version"] != 1 or len(keyboards) > MAX_JOURNAL_KEYBOARDS):
        raise JournalUnavailable("not a journal this version knows")
    for identity, ids in keyboards.items():
        if (not IDENTITY.match(identity) or not isinstance(ids, list) or not ids or len(ids) > config.MAX_KEYBOARD_KEYS
                or not all(isinstance(key, str) and config.KEY_ID.match(key) for key in ids) or ids != sorted(set(ids))):
            raise JournalUnavailable("not a journal this version knows")
    return {identity: list(ids) for identity, ids in keyboards.items()}


def write_journal(entries, path=None):
    """Replace the journal with ``entries`` ({identity: [key ids]}), readable by the user alone. Raises OSError."""
    path = path or JOURNAL
    data = {"version": 1, "keyboards": {identity: sorted(set(ids)) for identity, ids in sorted(entries.items()) if ids}}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def effective_keys(settings, app):
    """What the keys do while ``app`` is in front (None: no app known), ``settings`` being the "keyboard" part of the
    settings: its profile over the keys for all apps. A key left to the keyboard there is not in it."""
    keys = dict(settings["keys"])
    if app is not None:
        keys.update(next((profile["keys"] for profile in settings.get("profiles", ()) if profile["app"] == app), {}))
    return {name: action for name, action in keys.items() if action != config.NATIVE}


def low_battery(armed, battery):
    """Whether to say now that the battery is low: once when it runs down to LOW_BATTERY percent, and again only
    after it was charged in between. ``armed``: it may be said. Returns (armed, say it now)."""
    if not battery:
        return armed, False
    percent, status = battery
    if status != device.BATTERY_DISCHARGING or percent > LOW_BATTERY:
        return True, False
    return False, armed


def _set_reporting(dev, cid, divert):
    """Divert a key, or give it back: setCidReporting with the (temporary) divert bit and its "valid" bit alone,
    never the persistent ones. True if the keyboard echoed it back."""
    reply = dev.call(device.REPROG_CONTROLS_V4, 3, *struct.pack("!HBH", cid, 0x02 | (0x01 if divert else 0), 0))
    return struct.unpack("!H", reply[:2])[0] == cid and bool(reply[2] & 0x01) == divert


def _reporting_of(cids):
    """A task (see _run_here): what the keyboard says of these keys, {cid: "native" | "diverted" | "foreign"}.
    "foreign": diverted persistently, raw, or sent as another key, which is never this program's doing. A key
    it could not say anything about is left out (unknown); the first one it does not answer in time ends the
    asking."""
    seen = {}
    for cid in cids:
        try:
            flags, remap = yield (device.Mouse.get_cid_reporting, cid)
        except (HidppCancelled, HidppClosed):
            raise
        except HidppTimeout:
            break
        except (HidppError, IndexError, struct.error):
            continue
        if remap not in (0, cid) or flags & (device.REPORT_PERSISTENT | device.REPORT_RAW_XY):
            seen[cid] = "foreign"
        else:
            seen[cid] = "diverted" if flags & device.REPORT_DIVERTED else "native"
    return seen


# A task is a generator: each request it needs it yields as (function, *arguments), function(device, *arguments)
# being made on the device, and gets back what that returns, or has what it raises raised where it yielded.

def _run_here(task, call):
    """Run ``task`` to its end on this thread, each request made at once with ``call(request)``; its result."""
    reply = error = None
    while True:
        try:
            request = task.send(reply) if error is None else task.throw(error)
        except StopIteration as done:
            return done.value
        reply = error = None
        try:
            reply = call(request)
        except Exception as e:
            error = e


def _call(dev, request):
    """Make ``request`` (see _run_here) on ``dev``. One that gave way for too long counts as not answered."""
    try:
        return request[0](dev, *request[1:])
    except HidppCancelled as e:
        raise HidppTimeout(str(e)) from None


def _view(dev, transport, timeout, cancel, budget):
    """``dev`` making its requests in the background, see hidpp.ProbeTransport."""
    view = copy.copy(dev)  # its own transport; what it knows of itself is shared, and only read
    view.transport = hidpp.ProbeTransport(transport, timeout, cancel, time.monotonic() + budget)
    return view


def _status(dev):
    """(battery, (current host, hosts)) of a keyboard, as far as it says. HidppCancelled and HidppClosed are passed on."""
    battery, hosts = None, (0, [])
    try:
        battery = dev.get_battery()
        if dev.host_count:
            hosts = dev.get_hosts()
    except (HidppCancelled, HidppClosed):
        raise
    except HidppError as e:
        log.debug("keyboard status read failed: %s", e)
    return battery, hosts


class KeyboardSide:
    """The keyboard part of the engine. Everything but _probe_loop is called on the engine's thread."""

    def __init__(self, engine):
        self.engine = engine
        # One keyboard at a time: a list of these once a second one has to work alongside.
        self.device = None      # a device.Mouse: that class speaks HID++ 2.0 to any device
        self.transport = None
        self.online = False
        self.executor = None
        self._identity = None
        self._journal = {}
        self._reporting = {}    # cid -> what the keyboard said of it last (see _reporting_of); missing: unknown
        self._active = set()    # keys diverted by this program now
        self._block = None      # why no key is touched: "rivals" | "identity" | "journal" | "full"
        self._said = None       # the block last written to the log
        self._stalled = False   # the keyboard stopped answering during this round of changes
        self._pressed = set()
        self._held = {}         # cid -> action being held
        self._started = {}      # cid -> the last few macro actions a press of it started (see _remap)
        self._front = None      # the app in front, whose profile counts (see effective_keys)
        self._battery = None
        self._hosts = (0, [])
        self._firmware = None   # its firmware version, once read (see _read_firmware)
        self._firmware_due = False  # it is still to be read, once the keys leave time for it
        self._seen = None       # (name, keys, settings) of the keyboard last connected, while it is away
        self._slots = {}        # transport -> {devidx: what is there, see device.probe_slot; "pending" while looked at}
        self._generation = 0    # results of looks started before the last change of this number are dropped
        self._jobs = queue.Queue()  # for the keyboard's thread: looks, and the requests of tasks
        self._task = None       # the task under way (see _step), or None
        self._token = 0         # answers to requests of tasks before the last change of this number are dropped
        self._asked = None      # the request of the task on its way now
        self._called_off = threading.Event()  # set once the task under way is dropped: what it has not sent yet never goes
        self._wish = None       # what to do once the task under way is over: None | "configure" | ("host", n)
        self._failed = None     # "switch": the keyboard could not be moved to another computer last time
        self._leaving = 0.0     # time.monotonic until which the keyboard is on its way to another computer
        self._sent_at = None    # time.monotonic of the last Easy-Switch request this program made, while it may be going
        self.link = None        # flow_link() of the keyboard last found, kept after it is gone (engine thread only)
        self.last_drop = None   # (time.monotonic, sent away by this program) of its going, while it is away
        self._cancel = threading.Event()
        self._thread = None
        self._next_nodes = 0.0
        self._next_look = 0.0
        self._next_recovery = float("inf")

    # -- the engine's side ---------------------------------------------------

    def start(self):
        engine = self.engine
        self.executor = Executor(engine.vinput, self.device_action, lambda name, phase: engine._emit("hook", (name, phase)),
                                 owner="keyboard", haptic=engine.play_haptic)
        self._thread = threading.Thread(target=self._probe_loop, name="keyboard", daemon=True)
        self._thread.start()

    def owns(self, transport, devidx):
        return self.device is not None and transport is self.transport and devidx == self.device.devidx

    def skip(self, transport):
        """Slots of ``transport`` the mouse need not be looked for on: the keyboard is there, or being looked at."""
        states = self._slots.get(transport, {})
        return tuple(devidx for devidx in transport.slots() if states.get(devidx) == "pending" or self.owns(transport, devidx))

    def offer(self, dev):
        """A device the engine loaded while looking for the mouse: taken if it is a keyboard and none is held."""
        states = self._slots.setdefault(dev.transport, {})
        if dev.kind != device.KIND_KEYBOARD:
            states[dev.devidx] = "other"
            return False
        if self.device is not None or states.get(dev.devidx) == "pending":
            return False
        self._queue(dev.transport, [dev.devidx], {dev.devidx: dev})
        return True

    def after_scan(self, transport, mouse_devidx, loaded):
        """The mouse was found: take what was loaded on the way and look at every slot that was not."""
        self._slots.setdefault(transport, {})[mouse_devidx] = "mouse"
        for dev in loaded:
            self.offer(dev)
        self._next_look = time.monotonic() + ASLEEP_RETRY
        self._look()

    def found(self, generation, transport, slots, prepared, states):
        """A look at ``slots`` of ``transport`` is over. ``states``: what each slot it got to holds; ``prepared``:
        None, or the keyboard found (device, identity, {cid: reporting}, (battery, hosts)), already read off the
        engine's thread."""
        if generation != self._generation or transport.closed or transport is not self.engine._transports.get(transport.path):
            return
        mine = self._slots.setdefault(transport, {})
        for devidx in slots:
            if devidx in states:
                mine[devidx] = states[devidx]
            elif mine.get(devidx) == "pending":
                del mine[devidx]  # not reached: asked again later
        if prepared is None:
            return
        dev, identity, reporting, (battery, hosts) = prepared
        mouse = self.engine.mouse
        if self.device is not None or (mouse is not None and mouse.transport is transport and mouse.devidx == dev.devidx):
            mine.pop(dev.devidx, None)  # looked at again once this keyboard is gone
            return
        self.device, self.transport, self.online = dev, transport, True
        self._identity = identity
        self._reporting = dict(reporting)
        self._active.clear()
        self._pressed.clear()
        self._held.clear()
        self._started.clear()
        self._battery, self._hosts, self._failed, self._leaving = battery, hosts, None, 0.0
        self._firmware, self._firmware_due = None, True
        self._sent_at, self.link, self.last_drop = None, flow_link(identity), None
        log.info("found %s on %s slot %s", dev.name, transport.path, dev.devidx)
        self._tell_flow()
        self._invalidate()  # one keyboard at a time: looks still to come no longer count, and go before its requests
        self.configure()

    def on_link(self, transport, devidx, linked):
        """The receiver says a device came or went on ``devidx``, which is not the keyboard's or the mouse's."""
        states = self._slots.get(transport)
        if states is None:
            return
        if states.pop(devidx, None) == "pending":
            self._invalidate()
        if linked and self.device is None and self.engine.mouse is not None:
            self._queue(transport, [devidx])
        self._look()

    def on_notification(self, sub_id, address, data):
        """A notification from the keyboard (see owns)."""
        dev = self.device
        if sub_id == 0x41:  # gone, or back without having been seen to go: loaded again either way
            self._again(not (data[0] & 0x40))
            return
        feature = dev.index_to_feature.get(sub_id)
        event = address >> 4
        if feature == device.REPROG_CONTROLS_V4 and event == 0:
            self._on_keys({cid for cid in struct.unpack("!HHHH", data[:8]) if cid})
        elif feature == device.UNIFIED_BATTERY and event == 0:
            self._battery = (data[0], data[2])
            self.engine._publish()
        elif feature == device.BATTERY_STATUS and event == 0:
            self._battery = device.battery_status(data)
            self.engine._publish()
        elif feature == device.WIRELESS_DEVICE_STATUS and event == 0 and data[1] == 1:
            self._again(True)  # power-cycled: loaded again, and what its keys report read afresh

    def transport_gone(self, transport):
        if self._slots.pop(transport, None) is not None:
            self._invalidate()
        if transport is self.transport:
            self._drop()
        self._look()

    def config_changed(self, old_cfg, new_cfg):
        self._remap(effective_keys(old_cfg["keyboard"], self._front), effective_keys(new_cfg["keyboard"], self._front))

    def front_changed(self, app):
        """Another app is in front: ``app`` (see config.app_id), or None when that cannot be told."""
        app = app if config.app_id(app) else None
        if app == self._front:
            return
        old, self._front = self._effective(), app
        log.debug("the app in front changed")  # which one never goes to the log
        self._remap(old, self._effective())

    def _effective(self):
        return effective_keys(self.engine.cfg["keyboard"], self._front)

    def _remap(self, old, new):
        """The keys do ``new`` instead of ``old`` (see effective_keys). A key whose action changed lets go of what it
        held, and the macros it started stop; every other key, and what it started, carries on."""
        changed = {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
        if not changed:
            return
        for cid, action in list(self._held.items()):  # a key held down lets go of what it did: its key-up does nothing
            if config.key_id(cid) in changed:
                del self._held[cid]
                self.executor.release(action)
        for cid in [cid for cid in self._started if config.key_id(cid) in changed]:
            for action in self._started.pop(cid):
                self.executor.cancel(action)
        if self._task is not None and self._task.__name__ == "_configure":  # brings the keys in line with the old
            wish = self._wish or "configure"                                # settings: called off, and started over
            self._stop_task()
            self._wish = wish
            self._next_task()
        else:
            self.configure()

    def due(self, now):
        """How long until tick() has something to do."""
        moments = [self._next_recovery] if self.device is not None and self.online and self._task is None else []
        if self.device is None and self.engine.mouse is not None:
            moments += [self._next_nodes, self._next_look]
        return min(moments, default=float("inf")) - now

    def tick(self, now):
        if self.device is None and self.engine.mouse is not None:
            if now >= self._next_nodes:
                self._next_nodes = now + NODE_INTERVAL
                self._new_nodes()
            if now >= self._next_look:
                self._next_look = now + ASLEEP_RETRY
                self._look()
        if self.device is not None and self.online and self._task is None and now >= self._next_recovery:
            self._leaving = 0.0  # still here: see _switch
            self.configure()

    def shutdown(self):
        """Hand back the keys this program diverted, and stop looking (the engine closes the transports after)."""
        self._cancel.set()
        self._generation += 1
        self._jobs.put(None)
        self._stop_task()
        if self.executor:
            self.executor.reset()
        if self.device is not None and self.online:  # here, on this thread, within SHUTDOWN_BUDGET
            dev = _view(self.device, self.transport, self._timeout(self.transport), threading.Event(), SHUTDOWN_BUDGET)
            try:
                _run_here(self._give_back(), lambda request: _call(dev, request))
            except HidppError as e:
                log.debug("keyboard restore on exit failed: %s", e)
        if self.executor:
            self.executor.close()
        if self._thread is not None:
            self._thread.join(timeout=PROBE_TIMEOUT_OWN + 0.5)

    def state(self):
        """What the window shows of the keyboard, or None when none was ever seen. Never says which unit it is."""
        dev = self.device
        if dev is not None:
            self._seen = (dev.name, self._keys(), device.keyboard_settings(dev))
        if self._seen is None:
            return None
        connected = dev is not None and self.online
        return {"connected": connected, "known": True, "name": self._seen[0], "battery": self._battery if connected else None,
                "current_host": self._hosts[0], "hosts": copy.deepcopy(self._hosts[1]) if connected else [],
                "keys": copy.deepcopy(self._seen[1]), "reason": self._block if dev is not None else None,
                "failed": self._failed if connected else None, "firmware": self._firmware if connected else None,
                "settings": list(self._seen[2])}

    # -- actions -------------------------------------------------------------

    def device_action(self, action, phase):
        """Easy-Switch from a key moves the keyboard; nothing else a key does is the keyboard's business."""
        dev = self.device
        if action.get("type") != "host" or phase == "release" or dev is None or not self.online:
            return
        host = action.get("host", 0)
        if not 0 <= host < dev.host_count or host == self._hosts[0]:
            return
        self._want(("host", host))

    def _switch(self, host):
        """A task: move the keyboard to ``host``, only once every key this program diverted is given back (it
        comes back as new, and its keys are diverted again then). Where that cannot be done, or the keyboard
        does not go, it stays here with its keys as the settings say."""
        self._sent_at = None
        if not (yield from self._give_back()):
            log.warning("keyboard not moved to channel %s: a key could not be given back first", host + 1)
            self._failed = "switch"
            yield from self._configure()
            return
        self.executor.reset()
        self._sent_at = time.monotonic()  # it may be gone before the answer is back: then it went because of this
        try:
            yield (device.Mouse.change_host, host)
        except HidppClosed:
            raise
        except HidppError as e:
            log.warning("keyboard host switch failed: %s", e)
            self._sent_at = time.monotonic() if isinstance(e, HidppTimeout) else None
            self._failed = "switch"
            yield from self._configure()
            return
        self._failed, self._wish = None, None
        self._sent_at = time.monotonic()
        self._leaving = self._next_recovery = self._sent_at + SWITCH_GRACE  # still here then: it did not go,
        self.engine._publish()                                                 # and its keys are taken again

    def _on_keys(self, pressed):
        keys = self._effective()
        for cid in sorted(pressed - self._pressed):
            action = keys.get(config.key_id(cid)) if cid in self._active else None
            log.info("key %s down", config.key_id(cid))
            if action and config.keyboard_action(action) and action.get("type") != "none":
                self._held[cid] = action
                if action.get("type") == "macro":  # the same object each time, until the settings change
                    started = self._started.setdefault(cid, [])
                    if not any(one is action for one in started):
                        started[:] = started[-4:] + [action]  # no more can be queued or playing (MacroRunner.run)
                self.executor.press(action)
        for cid in sorted(self._pressed - pressed):
            action = self._held.pop(cid, None)
            if action:
                log.info("key %s up", config.key_id(cid))
                self.executor.release(action)
        self._pressed = pressed

    # -- the keys ------------------------------------------------------------

    def configure(self):
        """Have the keys brought in line with the settings (see _configure): now, or once the task under way is over."""
        self._want("configure")

    # -- tasks -------------------------------------------------------------------

    def _want(self, wish):
        """Have a task run (see self._wish), now if none is under way. A switch to another computer goes before
        bringing the keys in line, which it does itself where the keyboard stays."""
        if self.device is None or not self.online:
            return
        if wish == "configure" and (self._wish is not None or time.monotonic() < self._leaving):
            return  # leaving: taken up at self._next_recovery, should it stay
        if self._task is not None and self._task.__name__ == "_read_firmware":  # never holds up the keys: called
            self._stop_task()                                                   # off, and read again after them
            self._firmware_due = True
        self._wish = wish
        if self._task is None:
            self._next_task()

    def _next_task(self):
        wish, self._wish = self._wish, None
        if self.device is None or not self.online:
            return
        if wish is not None:
            self._next_recovery, self._leaving = float("inf"), 0.0
            self._task = self._configure() if wish == "configure" else self._switch(wish[1])
        elif self._firmware_due and time.monotonic() >= self._leaving:  # not while it may be on its way elsewhere
            self._firmware_due = False
            self._task = self._read_firmware()
        else:
            return
        self._token += 1
        self._called_off = threading.Event()
        self._step(None, None)

    def _step(self, reply, error):
        """Take the task under way on to its next request, sent to the keyboard's thread, or to its end."""
        try:
            request = self._task.send(reply) if error is None else self._task.throw(error)
        except StopIteration:
            self._task = self._asked = None
            self._next_task()
            return
        except Exception as e:
            self._task = self._asked = None
            if not isinstance(e, HidppClosed):  # closed: the transport's going is on its way, and handled then
                log.exception("keyboard task failed")
            return
        self._asked = request
        self._jobs.put(("ask", self._token, self.device, self.transport, self._timeout(self.transport), request,
                        self._called_off))

    def answered(self, token, reply, error):
        """The keyboard's thread made the request a task asked for (see _step)."""
        if token == self._token and self._task is not None:
            self._asked = None
            self._step(reply, error)

    def _stop_task(self):
        """Drop the task under way, and what was wished for after it. A request of it not sent yet never is
        (see _probe_loop); one sent already may have reached the keyboard, so what it did is asked afresh."""
        self._called_off.set()
        if self._task is not None:
            if self._asked is not None and self._asked[0] is _set_reporting:
                self._reporting.pop(self._asked[1], None)  # whether it went through is for the keyboard to say
            self._task.close()
        self._task = self._asked = self._wish = None
        self._token += 1

    def _timeout(self, transport):
        mouse = self.engine.mouse
        return PROBE_TIMEOUT_SHARED if mouse is not None and mouse.transport is transport else PROBE_TIMEOUT_OWN

    def _owned(self):
        """The keys the journal says this program diverted on this keyboard."""
        return {int(key, 16) for key in self._journal.get(self._identity, ())}

    def _configure(self):
        """A task: bring the keys in line with the settings, as far as is safe (see the description of this
        module); keys not known yet are asked first. Returns True when nothing is left to settle."""
        self._stalled = False
        self._block = self._blocked()
        unknown = sorted((self._owned() | self._wanted()) - set(self._reporting))
        self._reporting.update((yield from _reporting_of(unknown)))
        settled = self._block is None and (yield from self._settle(self._wanted()))
        retry = not settled and self._block not in ("identity", "full")
        self._next_recovery = time.monotonic() + RECOVERY_RETRY if retry else float("inf")
        self.engine._publish()
        return settled

    def _wanted(self):
        """The keys the settings give an action that the keyboard lets this program have."""
        dev, wanted = self.device, set()
        for name, action in self._effective().items():
            if not config.KEY_ID.match(name) or not config.keyboard_action(action):
                continue
            cid, does = int(name, 16), here(action).get("type")
            control = dev.controls.get(cid)
            if (control is not None and control.divertable and cid not in device.CID_PROTECTED
                    and not (does in ("keys", "button") and self.engine.vinput is None)):
                wanted.add(cid)
        return wanted

    def _blocked(self):
        """Why no key may be touched now, or None. Reads the journal afresh."""
        reason = why = None
        if self.engine._rivals:
            reason, why = "rivals", "other software for the device is running"
        elif self._identity is None:
            reason, why = "identity", "the keyboard does not say which one it is"
        else:
            try:
                self._journal = read_journal()
            except JournalUnavailable as e:
                reason, why = "journal", f"{JOURNAL} cannot be read ({e})"
            else:
                if self._identity not in self._journal and len(self._journal) >= MAX_JOURNAL_KEYBOARDS:
                    reason, why = "full", f"{JOURNAL} already lists {MAX_JOURNAL_KEYBOARDS} other keyboards"
        if reason != self._said:
            self._said = reason
            if why:
                log.warning("keyboard keys left as they are: %s", why)
        return reason

    def _record(self, owned):
        """Write the journal with ``owned`` as this keyboard's keys. False if that could not be done: what is
        known of the file stays what it holds."""
        entries = dict(self._journal)
        entries[self._identity] = sorted(config.key_id(cid) for cid in owned)
        if not entries[self._identity]:
            del entries[self._identity]
        if entries == self._journal:
            return True
        if len(owned) > config.MAX_KEYBOARD_KEYS:
            return False
        try:
            write_journal(entries)
        except OSError as e:
            log.warning("could not write %s: %s", JOURNAL, e)
            return False
        self._journal = entries
        return True

    def _write(self, cid, divert):
        """A task: divert ``cid`` or give it back; returns what the keyboard says of it afterwards (None: unknown)."""
        if self._stalled:
            return None
        try:
            if (yield (_set_reporting, cid, divert)):
                self._reporting[cid] = "diverted" if divert else "native"
                return self._reporting[cid]
        except HidppClosed:
            raise
        except HidppTimeout:
            self._stalled = True
        except (HidppError, IndexError, struct.error) as e:
            log.debug("key %s: %s", config.key_id(cid), e)
        self._reporting.pop(cid, None)  # whether it went through is for the keyboard to say
        if not self._stalled:
            self._reporting.update((yield from _reporting_of([cid])))
            self._stalled = cid not in self._reporting
        return self._reporting.get(cid)

    def _settle(self, wanted):
        """A task: divert the ``wanted`` keys and give back every other one this program diverted, as the keyboard
        and the journal allow. Returns True when every key is settled."""
        owned = self._owned()
        settled = True
        for cid in sorted(owned | wanted | self._active):
            state = self._reporting.get(cid)
            if cid in owned:
                if state is None:  # unknown: nothing done until the keyboard says
                    settled = False
                    continue
                if state == "diverted":
                    if cid in wanted:
                        self._active.add(cid)  # left on by an earlier run, or now: it stays
                        continue
                    state = yield from self._write(cid, False)
                    if state in ("diverted", None):
                        settled = False
                        continue
                owned.discard(cid)  # native (back, or the keyboard was reset), or someone else's now
                self._active.discard(cid)
            if cid in wanted and state == "native":
                if not self._record(owned | {cid}):  # written down first, or not diverted at all
                    settled = False
                    continue
                owned.add(cid)
                state = yield from self._write(cid, True)
                if state == "diverted":
                    self._active.add(cid)
                elif state is None:
                    settled = False
                else:
                    owned.discard(cid)
            elif cid not in owned:
                self._active.discard(cid)  # not in the journal: not this program's to keep
                if cid in wanted and state is None:
                    settled = False
        return self._record(owned) and settled

    def _give_back(self):
        """A task: give back every key this program diverted. True when the keyboard says none is left diverted
        and the journal says so too."""
        self._stalled = False
        if self._blocked() is None:
            unknown = sorted(self._owned() - set(self._reporting))
            self._reporting.update((yield from _reporting_of(unknown)))
            yield from self._settle(set())
        return not self._active and not self._owned()

    def _read_firmware(self):
        """A task: the keyboard's firmware version, read once after it is found, when nothing else is to be done.
        It is only shown: where it is not read in time, or a reply is not one, it stays unknown."""
        try:
            self._firmware = yield (device.Mouse.get_firmware,)
        except HidppClosed:
            raise
        except HidppError as e:  # out of time: asked again only when the keyboard is found again
            log.debug("keyboard firmware not read: %s", e)
        self.engine._publish()

    def _keys(self):
        keys = []
        for cid, control in sorted(self.device.controls.items()):
            protected = cid in device.CID_PROTECTED
            if not (control.divertable or protected):
                continue
            state = self._reporting.get(cid)
            if protected:
                reason = "protected"
            elif cid in self._active or state == "native":
                reason = None
            else:  # someone else's, or diverted by a run of this program it cannot tell, or not known yet
                reason = {"foreign": "foreign", "diverted": "changed"}.get(state, "pending")
            keys.append({"id": config.key_id(cid), "cid": cid, "tid": control.tid, "protected": protected,
                         "active": cid in self._active, "native_reason": reason})
            if len(keys) >= MAX_CONTROLS:
                break
        return keys

    # -- the device coming and going -------------------------------------------

    def _drop(self):
        """The keyboard is gone. What it had diverted stays in the journal: settled when it is back."""
        if self.device is None:
            return
        now = time.monotonic()
        sent = self._sent_at is not None and (
            now - self._sent_at < SWITCH_GRACE or self._task is not None and self._task.__name__ == "_switch")
        self._stop_task()
        if self.executor:
            self.executor.reset()
        self._slots.get(self.transport, {}).pop(self.device.devidx, None)
        self._seen = (self.device.name, self._keys(), device.keyboard_settings(self.device))
        self.last_drop = (now, sent) if self.link else None
        self._sent_at = None
        self.device, self.transport, self.online = None, None, False
        self._identity = None
        self._active.clear()
        self._pressed.clear()
        self._held.clear()
        self._started.clear()
        self._battery = self._firmware = None
        self._firmware_due = False
        self._block = self._failed = None
        self._next_recovery = float("inf")
        self.engine._publish()
        self._tell_flow()

    def _tell_flow(self):
        """Flow's side of the keyboard coming and going (flow_service.FlowService.on_engine, "keyboard"). An event
        of the engine's that stays in this program: the link is not in the state, which the window and others see."""
        self.engine._emit("flow", ("keyboard", {"link": self.link, "here": self.device is not None and self.online,
                                                "drop": self.last_drop}))

    def _again(self, linked):
        transport, devidx = self.transport, self.device.devidx
        self._drop()
        if linked:
            self._queue(transport, [devidx])

    # -- looking for the keyboard ------------------------------------------------

    def _invalidate(self):
        """Looks under way no longer count: the slots they are at are unknown again."""
        self._generation += 1
        for states in self._slots.values():
            for devidx in [devidx for devidx, what in states.items() if what == "pending"]:
                del states[devidx]

    def _queue(self, transport, slots, loaded=None):
        states = self._slots.setdefault(transport, {})
        for devidx in slots:
            states[devidx] = "pending"
        mouse = self.engine.mouse
        timeout = PROBE_TIMEOUT_SHARED if mouse is not None and mouse.transport is transport else PROBE_TIMEOUT_OWN
        self._jobs.put(("look", self._generation, transport, list(slots), loaded or {}, timeout))

    def _look(self):
        """Ask every slot nothing is known of yet, or that did not answer last time."""
        if self.device is not None or self.engine.mouse is None:
            return
        for transport in list(self.engine._transports.values()):
            states = self._slots.setdefault(transport, {})
            wanted = [devidx for devidx in transport.slots() if states.get(devidx) in (None, "asleep")]
            if wanted and not transport.closed:
                self._queue(transport, wanted)

    def _new_nodes(self):
        """Receivers and Bluetooth devices that came since the mouse was found."""
        for path, is_receiver in hidpp.find_hidpp_nodes():
            if path not in self.engine._transports:
                transport = self.engine._transport(path, is_receiver)
                if transport is not None:
                    self._queue(transport, transport.slots())

    def _probe_loop(self):
        while True:
            job = self._jobs.get()
            if job is None or self._cancel.is_set():
                return
            if job[0] == "ask":  # a request of a task (see KeyboardSide._step)
                _ask, token, dev, transport, timeout, request, called_off = job
                if called_off.is_set():
                    continue  # its task was dropped (keyboard gone or replaced, settings changed, exit): never sent
                reply = error = None
                try:  # called off while waiting for its turn, or just before it goes: not sent either
                    reply = _call(_view(dev, transport, timeout, called_off, REQUEST_BUDGET), request)
                except Exception as e:
                    error = e
                self.engine._queue.put(("kb_answer", token, reply, error))
                continue
            _look, generation, transport, slots, loaded, timeout = job
            if generation != self._generation:
                continue
            view = hidpp.ProbeTransport(transport, timeout, self._cancel, time.monotonic() + PROBE_BUDGET)
            states, prepared = {}, None
            try:
                for devidx in slots:
                    if generation != self._generation:  # no longer counts: the next job may
                        break
                    dev = loaded.get(devidx)
                    if dev is None:
                        what, dev = device.probe_slot(view, devidx)
                    else:
                        what = "keyboard"
                    if what == "keyboard":
                        dev.transport = view
                        cids = [cid for cid, control in sorted(dev.controls.items()) if control.divertable][:MAX_CONTROLS]
                        prepared = (dev, dev.identity(), _run_here(_reporting_of(cids), lambda request: request[0](dev, *request[1:])),
                                    _status(dev))
                        dev.transport = transport
                        states[devidx] = what
                        break
                    states[devidx] = what
            except (HidppCancelled, HidppClosed) as e:
                log.debug("look at %s ended: %s", transport.path, e)
                prepared = None
            except Exception:
                log.exception("looking for the keyboard failed")
                prepared = None
            self.engine._queue.put(("kb_found", generation, transport, slots, prepared, states))
