"""The app's own update check on the Qt side: one at a time, off the GUI thread, on a daily timer."""

import logging
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .. import __version__, updates

log = logging.getLogger(__name__)
FIRST_DELAY = 60.0  # s after start before the first automatic check, when one is due


def status(release, error=""):
    """{"state", "version", "page", "error"} for the card and the tray, from the last release found (or None) and the
    last error ("none": GitHub has no release). Always against the version running now."""
    if error and release is None:
        return {"state": "none" if error == "none" else "failed", "version": "", "page": "", "error": error}
    if release is None:
        return {"state": "unknown", "version": "", "page": "", "error": ""}
    newer = updates.is_newer(release["version"], __version__)
    return {"state": "available" if newer else "current", "version": release["version"], "page": release["page"],
            "error": error}


class Checker(QObject):
    """Asks GitHub for the latest release: on demand (check_now) and, while the setting is on, once a day."""

    result = Signal(object)    # status(): after each check, and "checking" when one starts
    _done = Signal(object, str)

    def __init__(self, store, parent=None, clock=time.time):
        super().__init__(parent)
        self.store = store
        self._clock = clock
        self._running = False
        self.state = updates.load_state(clock())
        self._error = ""
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self._due)
        self._done.connect(self._finished)
        store.changed.connect(self._schedule)
        self._schedule()

    def last(self):
        if self._running:
            return {"state": "checking", "version": "", "page": "", "error": ""}
        return status(self.state["release"], self._error)

    def _schedule(self):
        if not self.store.cfg["app"].get("update_check", True):
            self._timer.stop()
            return
        checked = self.state["checked_at"]
        wait = FIRST_DELAY if checked is None else max(FIRST_DELAY, checked + updates.PERIOD - self._clock())
        if not self._timer.isActive() or self._timer.remainingTime() > wait * 1000:
            self._timer.start(int(wait * 1000))

    def _due(self):
        if self.store.cfg["app"].get("update_check", True):
            self.check_now()
        else:
            self._timer.stop()

    def check_now(self):
        if self._running:
            return
        self._running = True
        self.result.emit(self.last())

        def job():
            try:
                release, error = updates.fetch_latest(), ""
            except updates.NoRelease:
                release, error = None, "none"
            except (OSError, ValueError) as e:
                log.info("update check failed: %s", e)
                release, error = None, str(e) or type(e).__name__
            self._done.emit(release, error)  # queued: back on the GUI thread

        threading.Thread(target=job, name="update-check", daemon=True).start()

    def _finished(self, release, error):
        self._running = False
        self.state["checked_at"] = self._clock()
        if release is not None:
            self.state["release"] = release
        elif error == "none":
            self.state["release"] = None
        self._error = error if release is None else ""
        updates.save_state(self.state)
        self._timer.stop()
        self._schedule()
        self.result.emit(self.last())

    def mark_notified(self, version):
        """The tray said ``version`` is out: it is not said again."""
        self.state["notified"] = version
        updates.save_state(self.state)


def checker_for(store):
    """The store's checker, made on first use (the program makes it at start; a page alone in a test makes its own)."""
    if getattr(store, "updates", None) is None:
        store.updates = Checker(store, parent=store)
    return store.updates
