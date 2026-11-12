"""The keyboard's two pages: what its keys do, and the keyboard itself (battery, Easy-Switch, firmware)."""

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from .. import APP_NAME, actions, config, device, firmware, keyboard
from ..i18n import _
from . import theme
from .action_picker import ActionPicker
from .pages import FirmwareQuery, ScrollPage, _header
from .widgets import Card, Row, hint_label

# What a key may not do: what only makes sense on the mouse (catalogue ids, see config.KEYBOARD_ACTIONS)
KEYBOARD_EXCLUDE = ("gestures", "ring", "wheel_mode", "dpi_cycle", "dpi_shift", "flow")
# Names for the keys whose control ids are known (Solaar's list of them): every other key goes by its id.
KEY_LABELS = {
    0x00D1: "Easy-Switch 1", 0x00D2: "Easy-Switch 2", 0x00D3: "Easy-Switch 3",
    0x0034: "Fn", 0x00CD: "Fn lock", 0x0101: "Fn lock key", 0x00DE: "F-Lock",
    0x00E2: "Backlight down", 0x00E3: "Backlight up",
}
# Why a key is left as it is (keyboard.KeyboardSide._keys)
KEY_REASONS = {
    "protected": ("Kept by the keyboard", "MX Options leaves the Easy-Switch keys and the keys of the Fn layer to the keyboard, "
                  "so that switching computers and the Fn keys always work."),
    "foreign": ("Changed by another program", "Another program has changed what this key sends, so MX Options leaves it alone."),
    "changed": ("Changed already", "This key was already changed when the keyboard was found, and MX Options cannot tell by whom, "
                "so it leaves the key alone."),
}
# Why no key is touched at all (state "reason")
BLOCK_REASONS = {
    "rivals": "Other software for Logitech devices is running, so the keys of the keyboard are left as they are.",
    "identity": "This keyboard does not say which one it is, so its keys are left as they are.",
    "journal": "The record of changed keys cannot be read: {}. Keys are left as they are.",
    "full": "The record of changed keys already lists 16 other keyboards, so the keys of this one are left as they are.",
}
# What went wrong last (state "failed")
FAILED = {
    "switch": "The keyboard was not moved to the other computer: a key MX Options changed could not be given back "
              "first. Its keys work as set here; try again in a moment.",
}
# The settings the keyboard keeps itself (device.KEYBOARD_SETTINGS): that it lists the feature says only that it has
# one, so each is said in general words, never which keys or values this keyboard uses for it.
SETTINGS = {
    "fn": ("Fn lock", "The keyboard can swap whether the top row gives F1–F12 or its other functions without Fn, "
           "as its guide describes. MX Options does not change this yet."),
    "backlight": ("Backlight", "The keyboard has a backlight with settings of its own, set on the keyboard as its guide "
                  "describes. MX Options does not change them yet."),
    "platform": ("Operating system layout", "The keyboard can be set up for the layout of another operating system, on "
                 "the keyboard itself as its guide describes. MX Options does not change this yet."),
    "disable": ("Disable keys", "The keyboard can turn some of its keys off. MX Options does not do this yet."),
    "locks": ("Lock keys", "The keyboard can report the state of its lock keys. MX Options does not show it yet."),
}
CAP_W, CAP_H, CAP_GAP, PAD = 152, 54, 8, 18
CHARGING = (device.BATTERY_CHARGING, device.BATTERY_ALMOST_FULL, device.BATTERY_SLOW_CHARGE)


def battery_text(battery):
    """A battery reading (percent, status) in words, as the sidebar shows it for the mouse and the keyboard."""
    percent, status = battery
    if status == device.BATTERY_FULL:
        return _("Fully charged")
    if status in CHARGING:
        return _("Charging · {}%").format(percent)
    return f"{percent}%"


def key_title(key):
    label = KEY_LABELS.get(key["cid"])
    return _(label) if label else _("Key {}").format(key["id"])


def block_text(reason):
    text = _(BLOCK_REASONS[reason])
    return text.format(keyboard.JOURNAL) if reason == "journal" else text


def notice_text(found):
    """What the banner of the keyboard's pages says, or "" for none."""
    if found.get("reason"):
        return block_text(found["reason"])
    return _(FAILED[found["failed"]]) if found.get("failed") in FAILED else ""


def key_value(key, values):
    """What a keycap says under the key's name: what it does, or why the keyboard keeps it."""
    if key["protected"]:
        return _(KEY_REASONS["protected"][0])
    if key["native_reason"] in KEY_REASONS:
        return _(KEY_REASONS[key["native_reason"]][0])
    return values.get(key["id"], "")


class KeyCap(QAbstractButton):
    """One key of KeyStrip: a button, so that it can be reached with Tab and the arrow keys, picked with Space,
    and is told apart by screen readers like any other button."""

    def __init__(self, key, parent):
        super().__init__(parent)
        self.setCheckable(True)
        self.setAutoExclusive(True)  # the arrow keys move between the keys, and pick the one they move to
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(CAP_W, CAP_H)
        self.key, self.value = key, ""

    def show_key(self, key, value):
        self.key, self.value = key, value
        self.setAccessibleName(key_title(key))
        self.setAccessibleDescription(value)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        key, rect = self.key, QRectF(0, 0, CAP_W, CAP_H)
        chosen = self.isChecked()
        kept = key["protected"] or key["native_reason"] in KEY_REASONS
        painter.setPen(QPen(QColor(theme.ACCENT_INK), 2) if chosen else QPen(QColor(theme.BORDER), 1))
        painter.setBrush(QColor(theme.CARD_HOVER if chosen else theme.CARD))
        painter.drawRoundedRect(rect.adjusted(0, 0, 0, -3), 9, 9)
        if self.hasFocus():  # where the keyboard is: a second outline inside the first
            painter.setPen(QPen(QColor(theme.ACCENT), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -6), 7, 7)
        painter.setPen(QPen(QColor(theme.MODEL_EDGE), 1))  # the keycap's skirt
        painter.drawLine(rect.left() + 8, rect.bottom() - 1, rect.right() - 8, rect.bottom() - 1)
        text = rect.adjusted(12, 7, -12, -9)
        small = QFontMetrics(painter.font())
        painter.setPen(QColor(theme.MUTED if not kept else theme.DISABLED))
        painter.drawText(text, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                         small.elidedText(key_title(key), Qt.TextElideMode.ElideRight, int(text.width())))
        font = painter.font()
        font.setBold(not kept)
        painter.setFont(font)
        painter.setPen(QColor(theme.DISABLED if kept else theme.TEXT))
        painter.drawText(text, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                         QFontMetrics(font).elidedText(self.value, Qt.TextElideMode.ElideRight, int(text.width())))
        if key["active"]:  # the program has this key now
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawEllipse(QRectF(rect.right() - 13, rect.top() + 8, 5, 5))


class KeyStrip(QWidget):
    """The keys that can be given an action, drawn as the keycaps of a keyboard: what each does under its
    name, the one being edited outlined, the ones the keyboard keeps greyed out."""

    selected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keys = []
        self._values = {}
        self._selected = None
        self.caps = {}  # key id -> KeyCap, in the order of the keys
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_keys(self, keys):
        ids = [key["id"] for key in keys]
        if ids != list(self.caps):  # other keys: new caps, in the new order (the Tab order follows)
            focused = next((key_id for key_id, cap in self.caps.items() if cap.hasFocus()), None)
            for cap in self.caps.values():
                cap.deleteLater()
            self.caps = {}
            for key in keys:
                cap = self.caps[key["id"]] = KeyCap(key, self)
                cap.clicked.connect(lambda _checked=False, key_id=key["id"]: self.select(key_id))
                cap.show()
            if focused in self.caps:
                self.caps[focused].setFocus()
        self._keys = list(keys)
        for key in keys:
            cap = self.caps[key["id"]]
            cap.show_key(key, key_value(key, self._values))
            cap.setChecked(key["id"] == self._selected)
        self._place()
        self.updateGeometry()
        self.update()

    def set_value(self, key_id, text):
        self._values[key_id] = text
        cap = self.caps.get(key_id)
        if cap is not None:
            cap.show_key(cap.key, key_value(cap.key, self._values))

    def select(self, key_id):
        self._selected = key_id
        cap = self.caps.get(key_id)
        if cap is not None:
            cap.setChecked(True)
        self.selected.emit(key_id)

    def _columns(self, width):
        return max(1, int((width - 2 * PAD + CAP_GAP) // (CAP_W + CAP_GAP)))

    def _height(self, width):
        rows = -(-len(self._keys) // self._columns(width)) if self._keys else 0
        return 2 * PAD + 14 + rows * (CAP_H + CAP_GAP)

    def _place(self):
        columns = self._columns(self.width())
        for index, cap in enumerate(self.caps.values()):
            row, column = divmod(index, columns)
            cap.move(PAD + column * (CAP_W + CAP_GAP), PAD + 14 + row * (CAP_H + CAP_GAP))

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._height(width)

    def sizeHint(self):
        return QSize(4 * (CAP_W + CAP_GAP) + 2 * PAD, self._height(4 * (CAP_W + CAP_GAP) + 2 * PAD))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setMinimumHeight(self._height(self.width()))
        self._place()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        body = QRectF(0.5, 0.5, width - 1, self._height(width) - 1)
        # the case: a slab with a raised rim along its top edge, as on a low-profile keyboard
        painter.setPen(QPen(QColor(theme.MODEL_EDGE), 1.2))
        painter.setBrush(QColor(theme.MODEL_BASE))
        painter.drawRoundedRect(body, 16, 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.MODEL_DECAL))
        painter.drawRoundedRect(QRectF(body.left() + PAD, body.top() + 7, body.width() - 2 * PAD, 4), 2, 2)


class KeysPage(QWidget):
    """Laid out like the Buttons page: the keys on the left, the list of actions on the right."""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._key = None
        self._keys = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QVBoxLayout()
        left.setContentsMargins(34, 28, 24, 20)
        left.setSpacing(12)
        left.addLayout(_header(_("Keys"), _(
            "Pick a key to choose what it does. A key left at Default does what it always does; the keys you change "
            "go back to normal when MX Options quits.")))
        self.notice = QLabel()
        self.notice.setObjectName("banner")
        self.notice.setWordWrap(True)
        self.notice.hide()
        left.addWidget(self.notice)
        # Whose keys are shown: every app's, or those of one app while it is in front (an app profile)
        line = QHBoxLayout()
        line.setSpacing(8)
        label = QLabel(_("Keys for"))
        self.apps = QComboBox()
        self.apps.setAccessibleName(_("Keys for"))
        label.setBuddy(self.apps)
        self.add_app = QPushButton(_("Add app"))
        self.remove_app = QPushButton(_("Remove"))
        self.remove_app.setAccessibleName(_("Remove the keys of this app"))
        for button in (self.add_app, self.remove_app):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        line.addWidget(label)
        line.addWidget(self.apps, 1)
        line.addWidget(self.add_app)
        line.addWidget(self.remove_app)
        left.addLayout(line)
        self.apps_hint = hint_label("")
        left.addWidget(self.apps_hint)
        self._app = None  # the app whose keys are shown, None: every app's
        self.strip = KeyStrip()
        self.strip.setAccessibleName(_("Keys"))
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(self.strip)
        inner.addStretch(1)
        area.setWidget(holder)
        left.addWidget(area, 1)
        self.empty = hint_label(_("Turn the keyboard on to see its keys here. Its receiver can be the one of the mouse."))
        left.addWidget(self.empty)
        layout.addLayout(left, 1)

        self.picker = ActionPicker(allow_button_actions=True, exclude=KEYBOARD_EXCLUDE)
        layout.addWidget(self.picker)

        self.strip.selected.connect(self._select)
        self.picker.changed.connect(self._changed)
        self.apps.currentIndexChanged.connect(self._app_chosen)
        self.add_app.clicked.connect(self._add_app)
        self.remove_app.clicked.connect(self._remove_app)
        store.changed.connect(self._refresh)
        store.changed.connect(self._show_apps)
        store.state_changed.connect(self._state)
        if store.front is not None:
            store.front.changed.connect(self._show_apps)
        self._show_apps()
        self._state(store.state)

    def showEvent(self, event):
        super().showEvent(event)
        if self.store.front is not None:
            self.store.front.want("page", True)  # to know which app to offer to add

    def hideEvent(self, event):
        super().hideEvent(event)
        if self.store.front is not None:
            self.store.front.want("page", False)

    def _show_apps(self):
        """The list of apps with keys of their own, and what can be done with it."""
        front = self.store.front
        supported = front is None or front.supported
        profiles = self.store.cfg["keyboard"]["profiles"] if supported else []
        if self._app is not None and self.store.profile(self._app) is None or not supported:
            self._app = None
        entries = [(_("All apps"), None)] + [(profile["name"] or profile["app"], profile["app"]) for profile in profiles]
        if [(self.apps.itemText(i), self.apps.itemData(i)) for i in range(self.apps.count())] != entries:
            self.apps.blockSignals(True)
            self.apps.clear()
            for text, app in entries:
                self.apps.addItem(text, app)
            self.apps.blockSignals(False)
        self.apps.setCurrentIndex(next(i for i, (_text, app) in enumerate(entries) if app == self._app))
        self.apps.setEnabled(supported)
        last = front.last_other if front is not None else None
        self.add_app.setEnabled(bool(supported and last and self.store.profile(last) is None
                                     and len(profiles) < config.MAX_PROFILES))
        self.remove_app.setEnabled(self._app is not None)
        if not supported:
            text = _("This desktop does not tell other programs which app is in front, so keys of their own for one "
                     "app cannot work here. The keys do what is set for all apps.")
        elif self._app is not None:
            text = _("While {} is in front. \"As in all apps\": the key does what it does in every app. \"Default\": the "
                     "key does what it always does, here, even where it is changed for all apps.").format(self._app)
        elif last and self.store.profile(last) is None:
            text = _("To give an app keys of its own, add it: {} was in front last.").format(last)
        else:
            text = _("To give an app keys of its own, switch to it, then come back here and add it.")
        self.apps_hint.setText(text)

    def _app_chosen(self, index):
        app = self.apps.itemData(index)
        if app != self._app:
            self._app = app
            self._show_apps()
            self._refresh()
            if self._key is not None:
                self._select(self._key["id"])

    def _add_app(self):
        front = self.store.front
        app = front.last_other if front is not None else None
        if app and self.store.add_profile(app):
            self._app = app
            self._show_apps()
            self._refresh()
            if self._key is not None:
                self._select(self._key["id"])

    def _remove_app(self):
        profile = self.store.profile(self._app)
        if profile is None:
            return
        answer = QMessageBox.question(self, APP_NAME, _(
            "Remove the keys of {}? In that app the keys then do what they do in every app.").format(profile["name"] or profile["app"]))
        if answer == QMessageBox.StandardButton.Yes:
            self.store.remove_profile(profile["app"])

    def _select(self, key_id):
        self._key = next((key for key in self._keys if key["id"] == key_id), None)
        if self._key is None:
            return
        reason = "protected" if self._key["protected"] else self._key["native_reason"]
        if reason in KEY_REASONS:
            value, text = KEY_REASONS[reason]
            self.picker.show_fixed(key_title(self._key), _(value), _(text))
        else:
            hint = _("The keyboard has not said yet how this key reports; the action is applied once it does.") \
                if reason == "pending" else ""
            profile = self.store.profile(self._app)
            if profile is None:
                self.picker.edit(key_title(self._key), self.store.cfg["keyboard"]["keys"].get(key_id, actions.DEFAULT), hint)
            else:
                self.picker.edit(key_title(self._key), profile["keys"].get(key_id), hint, inherit=True)

    def _changed(self, action):
        if self._key is None:
            return
        if self.store.profile(self._app) is not None:
            self.store.set_profile_key(self._app, self._key["id"], action)
        elif action is not None:
            self.store.set_key(self._key["id"], action)

    def _refresh(self):
        keys = keyboard.effective_keys(self.store.cfg["keyboard"], self._app)
        for key in self._keys:
            self.strip.set_value(key["id"], actions.describe(keys.get(key["id"], actions.DEFAULT)))

    def _state(self, state):
        found = state.get("keyboard") or {}
        keys = found.get("keys") or []
        notice = notice_text(found)
        self.notice.setVisible(bool(notice))
        self.notice.setText(notice)
        self.empty.setVisible(not keys)
        if keys != self._keys:
            self._keys = keys
            self.strip.set_keys(keys)
            self._refresh()
            chosen = self._key["id"] if self._key else None
            ids = [key["id"] for key in keys]
            if ids:
                self.strip.select(chosen if chosen in ids else next(
                    (key["id"] for key in keys if not key["protected"]), ids[0]))


class KeyboardPage(ScrollPage):
    def __init__(self, store, parent=None):
        super().__init__(_("Keyboard"), None, parent)
        self.store = store
        self.notice = QLabel()
        self.notice.setObjectName("banner")
        self.notice.setWordWrap(True)
        self.notice.hide()
        self.body.addWidget(self.notice)

        card = Card(_("Status"))
        self.battery = QLabel()
        self.battery.setObjectName("valueLabel")
        card.add(Row(_("Battery"), None, self.battery))
        self.body.addWidget(card)

        card = Card(_("Easy-Switch"), hint=_(
            "Press an Easy-Switch key to move the keyboard to the computer on that channel. Hold one for three seconds "
            "to pair the keyboard with another computer on it."))
        self.hosts = QWidget()
        self.hosts_layout = QHBoxLayout(self.hosts)
        self.hosts_layout.setContentsMargins(0, 0, 0, 0)
        self.hosts_layout.setSpacing(10)
        card.add_plain(self.hosts)
        self.hosts_card = card
        self._hosts_shown = None
        self.body.addWidget(card)

        card = Card(_("Keyboard settings"), hint=_(
            "What the keyboard says it has settings for. They are the keyboard's own: MX Options shows them, "
            "but does not change them yet."))
        self.settings_rows = {name: card.add(Row(_(title), None, None, note=_(text))) for name, (title, text) in SETTINGS.items()}
        self.settings_card = card
        self.body.addWidget(card)

        card = Card(_("Firmware"))
        self.firmware_row = card.add(Row(_("Firmware"), None, None, note=" "))
        self.firmware = FirmwareQuery(self)
        self.firmware.answered.connect(self._firmware_answered)
        self._firmware_entries, self._firmware_for = [], None
        self.body.addWidget(card)

        card = Card(_("Keys"))
        reset = QPushButton(_("Restore keyboard defaults"))
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.clicked.connect(self._reset)
        card.add(Row(_("Every key does what it always does"), None, reset))
        self.body.addWidget(card)
        self.finish()

        store.state_changed.connect(self._state)
        self._state(store.state)

    def _ask_firmware(self):
        found = self.store.state.get("keyboard") or {}
        if firmware.available() and found.get("connected") and found.get("name") and not self.firmware.running:
            self._firmware_for = found["name"]
            self.firmware.start(found["name"], False)
        self._show_firmware()

    def _firmware_answered(self, entries, _checked):
        if (self.store.state.get("keyboard") or {}).get("name") == self._firmware_for:
            self._firmware_entries = entries
        elif self.isVisible():  # an answer about the keyboard before: this one is asked about now
            self._ask_firmware()
        self._show_firmware()

    def _show_firmware(self):
        entries = self._firmware_entries
        found = self.store.state.get("keyboard") or {}
        version = found.get("firmware") if found.get("connected") else None
        title = _("Firmware {}").format(version) if version else _("Firmware")
        self.firmware_row.title.setText("   ·   ".join(f"{entry['name']}  {entry['version']}" for entry in entries) or title)
        if not firmware.available():
            text = _("Install fwupd, the system's firmware updater, to see the firmware versions and check for updates.")
        elif self.firmware.running:
            text = _("Asking fwupd…")
        elif not entries and version:
            text = _("Read from the keyboard. fwupd, the system's firmware updater, does not see it right now.")
        elif not entries:
            text = _("fwupd, the system's firmware updater, does not see the keyboard right now.")
        else:
            text = _("Updates are installed by fwupd, the system's firmware updater, from the firmware Logitech "
                     "publishes for Linux. MX Options does not flash anything itself.")
        self.firmware_row.note.setText(text)

    def showEvent(self, event):
        super().showEvent(event)
        if self._firmware_for != (self.store.state.get("keyboard") or {}).get("name"):
            self._ask_firmware()

    def _reset(self):
        answer = QMessageBox.question(self, APP_NAME, _(
            "Give every key of the keyboard back its own function, in every app? The keys set for single apps go too."))
        if answer == QMessageBox.StandardButton.Yes:
            self.store.restore_keyboard()

    def _state(self, state):
        found = state.get("keyboard") or {}
        connected = bool(found.get("connected"))
        notice = notice_text(found)
        self.notice.setVisible(bool(notice))
        self.notice.setText(notice)
        settings = found.get("settings") or []
        for name, row in self.settings_rows.items():
            self.settings_card.set_row_visible(row, name in settings)
        self.settings_card.setVisible(any(name in SETTINGS for name in settings))
        battery = found.get("battery") if connected else None
        self.battery.setText(battery_text(battery) if battery else _("Connected") if connected else _("Not connected"))
        hosts = found.get("hosts") or []
        self.hosts_card.setVisible(bool(hosts))
        signature = (found.get("current_host"), tuple((h["paired"], h["name"]) for h in hosts))
        if signature != self._hosts_shown:
            self._hosts_shown = signature
            while self.hosts_layout.count():
                item = self.hosts_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            for index, host in enumerate(hosts):
                name = host["name"] or (_("Paired") if host["paired"] else _("Empty"))
                chip = QPushButton(f"{index + 1}  ·  {name}")
                chip.setMinimumHeight(40)
                chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                if index == found.get("current_host"):
                    chip.setObjectName("primary")
                self.hosts_layout.addWidget(chip, 1)
        if found.get("name") != self._firmware_for:
            self._firmware_entries = []  # fwupd's answer about another keyboard
        if self.isVisible() and connected and self._firmware_for != found.get("name"):
            self._ask_firmware()
        self._show_firmware()
