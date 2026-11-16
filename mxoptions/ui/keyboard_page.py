"""The keyboard's two pages: what its keys do, and the keyboard itself (battery, Easy-Switch, firmware)."""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QComboBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, actions, config, device, firmware, keyboard
from ..i18n import _
from . import icons, keyboard_drawing, mouse_view, theme
from .action_picker import ActionPicker
from .pages import FirmwareQuery, ScrollPage, _header
from .widgets import BatteryIcon, Card, Row, Segmented, SliderRow, Switch, hint_label, ui_scale

# What a key may not do: what only makes sense on the mouse (catalogue ids, see config.KEYBOARD_ACTIONS)
KEYBOARD_EXCLUDE = ("gestures", "wheel_mode", "dpi_cycle", "dpi_shift", "flow", "spotlight")
# Headings of the action list for a key, where the catalogue's speak of the mouse (actions.CATEGORIES)
KEYBOARD_TITLES = {"mouse": "General"}
# Names for the keys whose control ids are known (Solaar's list of them): every other key goes by its id.
KEY_LABELS = {
    0x00D1: "Easy-Switch 1", 0x00D2: "Easy-Switch 2", 0x00D3: "Easy-Switch 3",
    0x0034: "Fn", 0x00CD: "Fn lock", 0x0101: "Fn lock key", 0x00DE: "F-Lock",
    0x00E2: "Backlight down", 0x00E3: "Backlight up",
    0x00C7: "Screen brightness down", 0x00C8: "Screen brightness up", 0x00E0: "Task view", 0x00E1: "Action center",
    0x00E4: "Previous track", 0x00E5: "Play/Pause", 0x0141: "Play/Pause", 0x00E6: "Next track", 0x00E7: "Mute",
    0x00E8: "Volume down", 0x00E9: "Volume up", 0x0003: "Mute", 0x0005: "Next track", 0x0006: "Previous track",
    0x0103: "Dictation", 0x0108: "Emoji", 0x010A: "Screenshot", 0x00BF: "Screenshot", 0x004C: "Print Screen",
    0x011C: "Mute microphone", 0x011D: "Do not disturb", 0x000A: "Calculator", 0x006E: "Show desktop",
    0x006F: "Lock screen", 0x00C2: "Lock screen", 0x00CC: "Switch app", 0x00CA: "View open apps",
    0x00CB: "View all apps", 0x00D4: "Search", 0x00DD: "Language switch", 0x0121: "Language switch",
    0x01B4: "AI prompt", 0x01A9: "Cut", 0x01AA: "Copy", 0x01AB: "Paste",
    0x010B: "Grave accent", 0x010C: "Tab", 0x010D: "Caps Lock", 0x010E: "Left Shift", 0x010F: "Left Ctrl",
    0x0110: "Left Start/Option", 0x0111: "Left Alt/Command", 0x0112: "Right Alt/Command", 0x0113: "Right Start/Option",
    0x0114: "Right Ctrl", 0x0115: "Right Shift", 0x0116: "Insert", 0x0117: "Delete", 0x0118: "Home", 0x0119: "End",
    0x011A: "Page Up", 0x011B: "Page Down", 0x011E: "Backslash",
}
# The order of the keys on the Keys page: as they sit along the top row of an MX keyboard; others after, by id
KEY_ORDER = (0x00D1, 0x00D2, 0x00D3, 0x00C7, 0x00C8, 0x00E0, 0x00E1, 0x00E2, 0x00E3, 0x0103, 0x0108, 0x010A, 0x00BF,
             0x011C, 0x00E4, 0x00E5, 0x0141, 0x00E6, 0x00E7, 0x00E8, 0x00E9, 0x000A, 0x0034)
# The icon on a key's cap (freedesktop names), drawn where the icon theme has it
KEY_ICONS = {
    0x00D1: "computer", 0x00D2: "computer", 0x00D3: "computer",
    0x00C7: "brightness-low", 0x00C8: "brightness-high", 0x00E0: "preferences-system-windows",
    0x00E1: "preferences-desktop-notification", 0x00E2: "keyboard-brightness", 0x00E3: "keyboard-brightness",
    0x00E4: "media-skip-backward", 0x00E5: "media-playback-start", 0x0141: "media-playback-start",
    0x00E6: "media-skip-forward", 0x00E7: "audio-volume-muted", 0x00E8: "audio-volume-low", 0x00E9: "audio-volume-high",
    0x0003: "audio-volume-muted", 0x0005: "media-skip-forward", 0x0006: "media-skip-backward",
    0x0103: "audio-input-microphone", 0x0108: "face-smile", 0x010A: "camera-photo", 0x00BF: "camera-photo",
    0x004C: "camera-photo", 0x011C: "microphone-sensitivity-muted", 0x011D: "notifications-disabled",
    0x000A: "accessories-calculator", 0x006E: "user-desktop", 0x006F: "system-lock-screen", 0x00C2: "system-lock-screen",
    0x00CC: "preferences-system-windows", 0x00D4: "system-search", 0x01A9: "edit-cut", 0x01AA: "edit-copy",
    0x01AB: "edit-paste",
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
    "backlight": ("Backlight on", "The keyboard has a backlight with settings of its own, set on the keyboard as its guide "
                  "describes. MX Options does not change them yet."),
    "platform": ("Operating system layout", "The keyboard can be set up for the layout of another operating system, on "
                 "the keyboard itself as its guide describes. MX Options does not change this yet."),
    "disable": ("Disable keys", "The keyboard can turn some of its keys off. MX Options does not do this yet."),
    "locks": ("Lock keys", "The keyboard reports when Caps Lock, Num Lock or Scroll Lock turns on or off. MX Options can "
              "show a notice then; it never saves or logs their state."),
}
# How a change of a setting went, when not as asked (keyboard.KeyboardSide._change)
OUTCOMES = {
    "unknown": "The keyboard did not say how this is set.",
    "unsupported": "The keyboard does not let MX Options change this.",
    "not-applied": "The keyboard did not take the change.",
    "restored": "The keyboard did not take the change as asked; the earlier setting was put back.",
    "unconfirmed": "The keyboard did not confirm the change. Shown as the keyboard reports it.",
}
FN_HINT = ("On: the top row gives F1–F12, and Fn with a key gives its other function. Off: the other way round. "
           "The keyboard's own Fn lock shortcut changes it too.")
# The keys the keyboard can turn off (kbsettings.DISABLE_KEYS): the title of each one's row, and its keycap name
DISABLE_TITLES = (("caps_lock", "Caps Lock", "Caps Lock"), ("num_lock", "Num Lock", "Num Lock"),
                  ("scroll_lock", "Scroll Lock", "Scroll Lock"), ("insert", "Insert", "Insert"),
                  ("super", "Windows/Super key", "Windows/Super"))
DISABLE_NOTE = "Turn off keys you press by mistake. A key turned off does nothing; turn its switch off to enable it again."
DISABLE_HINT = ("The keyboard does this itself. Whether it keeps it after it is switched off is not known yet; "
                "MX Options shows what the keyboard reports.")
# Operating systems a layout is for (kbsettings.OS_BITS): their own names, the same in every language
OS_NAMES = {"windows": "Windows", "macos": "macOS", "linux": "Linux", "chromeos": "ChromeOS", "android": "Android",
            "ios": "iOS", "webos": "webOS", "windows_embedded": "Windows Embedded", "tizen": "Tizen"}
PLATFORM_HINT = ("The layout the keyboard uses for the operating system on this Easy-Switch channel; each channel keeps "
                 "its own. The keyboard's own shortcut for it, as its guide describes, changes it too.")
BACKLIGHT_HINT = ("The keyboard keeps these settings itself; its own backlight keys change them too. MX Options does not "
                  "store them.")
# What the Backlight row says while nothing else does (KeyboardPage._show_backlight), the first that applies
BACKLIGHT_NOTES = {
    "read-only": "MX Options does not change the backlight of this keyboard; its own keys do.",
    "keys": "Brightness is set with the keyboard's keys just now. Choose Automatic or Fixed to set it here.",
    "keys-only": "Brightness is set with the keyboard's keys.",
    "mode-unknown": "The keyboard uses a brightness setting MX Options does not know; it is left as it is.",
    "untimed": "Brightness and timing are set with the keyboard's keys; newer keyboard firmware may let MX Options set "
               "them too.",
}
LEVEL_HINT = "Steps 0 to {top}, as the keyboard counts them. In Automatic, the light around the keyboard sets the brightness."
POWER_SAVE_HINT = "Shown as the keyboard reports it. MX Options does not change this yet."
# The backlight's timers (kbsettings.TIMERS): the title of each one's row, its name for assistive technology, its hint
TIMER_ROWS = (
    ("hands_out", "Off after, hands away", "Backlight off after, hands away",
     "How long the backlight stays on after you type, once your hands move away."),
    ("hands_in", "Off after, hands near", "Backlight off after, hands near",
     "How long the backlight stays on after you type, while your hands stay near the keyboard."),
    ("powered", "Off after, plugged in", "Backlight off after, plugged in",
     "How long the backlight stays on after you type, while the keyboard is charging over USB."),
)
TIMER_STOPS = (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200)  # s
# px: the callouts of the keys on the F-row (the size of the mouse's), the other keys, those left as they are
CALLOUT_W, CALLOUT_H = 150, mouse_view.CHIP_H
CAP_MIN_W, CAP_H = 136, 46
KEPT_MIN_W, KEPT_H = 120, 36
CAP_GAP, GROUP_H, BOARD_GAP = 9, 28, 18  # px: between caps, a section's caption, around the keyboard
CHARGING = (device.BATTERY_CHARGING, device.BATTERY_ALMOST_FULL, device.BATTERY_SLOW_CHARGE)


def layout_labels(choices):
    """What the layouts a keyboard offers are called: the systems each is for, numbered where that is not enough."""
    labels = [", ".join(OS_NAMES.get(name, name) for name in choice["os"]) or _("Layout {}").format(choice["index"] + 1)
              for choice in choices]
    return [label + " · " + _("Layout {}").format(choice["index"] + 1) if labels.count(label) > 1 else label
            for label, choice in zip(labels, choices)]


def duration_text(seconds):
    """A time in whole seconds in words: hours, minutes and seconds, each left out only where it is 0."""
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return " ".join(unit.format(count) for count, unit in ((hours, _("{} h")), (minutes, _("{} min")), (seconds, _("{} s")))
                    if count)


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


def key_kept(key):
    """Whether the keyboard keeps ``key`` as it is: it cannot be given an action here."""
    return bool(key["protected"]) or key["native_reason"] in KEY_REASONS


def key_sort(keys):
    """``keys`` in the order the Keys page shows them: those that can be changed first, then those the keyboard keeps;
    each in the order of KEY_ORDER, the others after it by control id."""
    def place(key):
        cid = key["cid"]
        return key_kept(key), KEY_ORDER.index(cid) if cid in KEY_ORDER else len(KEY_ORDER), cid
    return sorted(keys, key=place)


def first_key(keys, chosen=None):
    """The key to pick on the Keys page: ``chosen`` while it is there, else the first that can be changed, else the
    first; None without keys."""
    ids = [key["id"] for key in keys]
    if chosen in ids:
        return chosen
    ordered = key_sort(keys)
    return next((key["id"] for key in ordered if not key_kept(key)), ordered[0]["id"] if ordered else None)


def confirm_restore(parent, store):
    """Ask, then give every key of the keyboard back its own function, in every app."""
    answer = QMessageBox.question(parent, APP_NAME, _(
        "Give every key of the keyboard back its own function, in every app? The keys set for single apps go too."))
    if answer == QMessageBox.StandardButton.Yes:
        store.restore_keyboard()


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
    """One key's callout on KeyStrip, drawn like the callouts of the mouse's buttons: the key's name over what it does.
    A button, so that it can be reached with Tab and the arrow keys, picked with Space, and is told apart by screen
    readers like any other button."""

    hovered = Signal(bool)
    focused = Signal()

    def __init__(self, key, parent):
        super().__init__(parent)
        self.setCheckable(True)
        self.setAutoExclusive(True)  # the arrow keys move between the keys, and pick the one they move to
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.key, self.value, self.changed, self._inside = key, "", False, False

    def show_key(self, key, value, changed=False):
        self.key, self.value, self.changed = key, value, changed
        self.setAccessibleName(key_title(key))
        self.setAccessibleDescription(value)
        self.update()

    def enterEvent(self, event):
        super().enterEvent(event)
        self._inside = True
        self.hovered.emit(True)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._inside = False
        self.hovered.emit(False)
        self.update()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        # The keys are one stop for Tab, as radio buttons are: coming in with Tab, the focus goes to the key picked
        reason = event.reason()
        if reason in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason) and not self.isChecked():
            picked = next((cap for cap in self.parentWidget().caps.values() if cap.isChecked()), None)
            if picked is not None and picked is not self:
                picked.setFocus(reason)
                return
        self.focused.emit()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        key, rect = self.key, QRectF(self.rect()).adjusted(1, 1, -1, -1)
        kept, chosen = key_kept(key), self.isChecked()
        if chosen:
            pen = QPen(QColor(theme.ACCENT_INK), 2)
        else:
            pen = QPen(QColor(theme.ACCENT_DIM if self._inside else theme.BORDER), 1)
        painter.setPen(pen)
        painter.setBrush(QColor(theme.CARD_HOVER if chosen or self._inside else theme.BG if kept else theme.CARD))
        painter.drawRoundedRect(rect, 12, 12)
        if self.hasFocus():  # where the keyboard is: a second outline inside the first
            painter.setPen(QPen(QColor(theme.ACCENT), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), 9, 9)
        if kept:
            self._paint_kept(painter, rect)
        else:
            self._paint_key(painter, rect)
        if key["active"]:  # the program has this key now
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawEllipse(QRectF(rect.right() - 12, rect.top() + 7, 5, 5))

    def _paint_key(self, painter, rect):
        """A key that can be changed: its name, small, over what it does, bold (the accent once set to something of
        its own), with the key's icon where the icon theme has one."""
        text = rect.adjusted(14, 6, -14, -6)
        name = KEY_ICONS.get(self.key["cid"], "")
        title = QRectF(text)
        if icons.has(name):
            icons.icon(name).paint(painter, int(text.left()), int(text.top() + 1), 14, 14)
            title.setLeft(text.left() + 20)
        font = QFont(painter.font())
        small = QFont(font)
        small.setPointSizeF(font.pointSizeF() * 0.86)
        painter.setFont(small)
        painter.setPen(QColor(theme.MUTED))
        painter.drawText(title, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                         QFontMetrics(small).elidedText(key_title(self.key), Qt.TextElideMode.ElideRight,
                                                        int(title.width())))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(theme.ACCENT_INK if self.changed else theme.TEXT))
        painter.drawText(text, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                         QFontMetrics(font).elidedText(self.value, Qt.TextElideMode.ElideRight, int(text.width())))

    def _paint_kept(self, painter, rect):
        """A key the keyboard keeps: a padlock and its name."""
        middle = rect.center().y()
        lock = QRectF(rect.left() + 14, middle - 2, 10, 8)  # a padlock: its body, and the shackle over it
        painter.setPen(QPen(QColor(theme.MUTED), 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawArc(QRectF(lock.left() + 2, lock.top() - 6, 6, 9), 0, 180 * 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.MUTED))
        painter.drawRoundedRect(lock, 2, 2)
        text = rect.adjusted(32, 0, -12, 0)
        painter.setPen(QColor(theme.MUTED))
        painter.drawText(text, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         QFontMetrics(painter.font()).elidedText(key_title(self.key), Qt.TextElideMode.ElideRight,
                                                                  int(text.width())))


class KeyStrip(QWidget):
    """The keyboard, laid out like the Buttons page of the mouse: a callout for each key on the F-row above a picture
    of the keyboard, the line from the one being edited to its key; under the picture the other keys that can be
    changed, then those left as they are. A key is marked on the picture only where its place is known
    (keyboard_drawing.slots); a click there picks it as its callout does."""

    selected = Signal(str)
    focused = Signal(QWidget)  # a key's callout got the keyboard focus: the page scrolls it into view

    def __init__(self, parent=None):
        super().__init__(parent)
        self._keys = []
        self._values = {}  # key id -> (what it does, in words; whether that is not its own function)
        self._selected = self._hover = None
        self._name = None
        self._layout_name = keyboard_drawing.layout_for(None)
        self._slot_of = {}
        self._anchors = {}  # key id -> where its line ends, as last drawn
        self.caps = {}  # key id -> KeyCap, in the order of the sections (the Tab order follows)
        self.other_label = QLabel(_("Other keys").upper(), self)
        self.kept_label = QLabel(_("Left as they are").upper(), self)  # by the keyboard or another program
        for label in (self.other_label, self.kept_label):
            label.setObjectName("keyGroup")
            label.hide()
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # -- keys ---------------------------------------------------------------

    def set_keys(self, keys, name=None):
        self._name = name
        self._layout_name = keyboard_drawing.layout_for(name)
        self._slot_of = keyboard_drawing.slots(keys, self._layout_name)
        self._keys = list(keys)
        ordered = [key for section in self._sections() for key in section]
        ids = [key["id"] for key in ordered]
        if ids != list(self.caps):  # other keys or another order: new caps, in the new order (the Tab order follows)
            focused = next((key_id for key_id, cap in self.caps.items() if cap.hasFocus()), None)
            for cap in self.caps.values():
                cap.deleteLater()
            self.caps = {}
            for key in ordered:
                cap = self.caps[key["id"]] = KeyCap(key, self)
                cap.clicked.connect(lambda _checked=False, key_id=key["id"]: self.select(key_id))
                cap.hovered.connect(lambda inside, key_id=key["id"]: self._set_hover(key_id if inside else None))
                cap.focused.connect(lambda cap=cap: self.focused.emit(cap))
                cap.show()
            if focused in self.caps:
                self.caps[focused].setFocus()
        for key in ordered:
            self._show(self.caps[key["id"]], key)
            self.caps[key["id"]].setChecked(key["id"] == self._selected)
        self._place()
        self.updateGeometry()
        self.update()

    def _sections(self):
        """The keys of each section, top to bottom: callouts (keys on the F-row, left to right), the other keys that
        can be changed, those left as they are."""
        ordered = key_sort(self._keys)
        x = {slot: place.left() for slot, place in keyboard_drawing.board(self._layout_name) if slot}
        on_row = lambda key: self._slot_of.get(key["id"], "").startswith("f")  # noqa: E731
        callouts = sorted((key for key in ordered if on_row(key) and not key_kept(key)),
                          key=lambda key: x[self._slot_of[key["id"]]])
        others = [key for key in ordered if not key_kept(key) and not on_row(key)]
        return callouts, others, [key for key in ordered if key_kept(key)]

    def _show(self, cap, key):
        value, changed = self._values.get(key["id"], ("", False))
        cap.show_key(key, key_value(key, {key["id"]: value}), changed and not key_kept(key))

    def set_value(self, key_id, text, changed=False):
        self._values[key_id] = (text, changed)
        cap = self.caps.get(key_id)
        if cap is not None:
            self._show(cap, cap.key)
            self.update()  # the key's mark on the picture

    def select(self, key_id):
        self._selected = key_id
        cap = self.caps.get(key_id)
        if cap is not None:
            cap.setChecked(True)
        self.update()
        self.selected.emit(key_id)

    def _set_hover(self, key_id):
        if key_id != self._hover:
            self._hover = key_id
            self.update()

    # -- geometry -----------------------------------------------------------

    @staticmethod
    def _grid(count, width, least):
        """Columns and the width of one cap, for ``count`` caps of at least ``least`` px across ``width``."""
        columns = max(1, min(count or 1, int((width + CAP_GAP) // (least + CAP_GAP))))
        return columns, max(least, (width - (columns - 1) * CAP_GAP) / columns)

    def _layout(self, width):
        """Where everything goes at ``width``: key id -> cap rect, the area of the picture, the caption of each lower
        section (label -> its top, or None), and the height all of it takes."""
        callouts, others, kept = self._sections()
        scale = ui_scale()
        places, captions, top = {}, {self.other_label: None, self.kept_label: None}, 0.0

        def grid(keys, least, height):
            nonlocal top
            columns, cap = self._grid(len(keys), width, least)
            for index, key in enumerate(keys):
                row, column = divmod(index, columns)
                places[key["id"]] = QRectF(column * (cap + CAP_GAP), top + row * (height + CAP_GAP), cap, height)
            top += -(-len(keys) // columns) * (height + CAP_GAP) - CAP_GAP

        if callouts:
            grid(callouts, CALLOUT_W * scale, CALLOUT_H)
            top += BOARD_GAP
        board_width = keyboard_drawing.LAYOUTS[self._layout_name] + 0.8
        unit = min(width / board_width, 34.0)
        board = QRectF(0, top, width, unit * (keyboard_drawing.ROWS + 0.8))
        top += board.height()
        for label, keys, least, height in ((self.other_label, others, CAP_MIN_W, CAP_H),
                                           (self.kept_label, kept, KEPT_MIN_W, KEPT_H)):
            if not keys:
                continue
            top += BOARD_GAP
            captions[label] = top
            top += GROUP_H
            grid(keys, least * scale, height)
        return places, board, captions, top + 2

    def _place(self):
        places, _board, captions, _height = self._layout(self.width())
        for key_id, rect in places.items():
            self.caps[key_id].setGeometry(rect.toRect())
        for label, top in captions.items():
            label.setVisible(top is not None)
            if top is not None:
                label.setGeometry(2, int(top), self.width() - 4, GROUP_H - 8)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return int(self._layout(width)[3])

    def sizeHint(self):
        width = 3 * (int(CALLOUT_W * ui_scale()) + CAP_GAP)
        return QSize(width, self.heightForWidth(width))

    def minimumSizeHint(self):
        return QSize(int(CALLOUT_W * ui_scale()), 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setMinimumHeight(self.heightForWidth(self.width()))
        self._place()

    # -- the picture --------------------------------------------------------

    def _board(self):
        return self._layout(self.width())[1]

    def mouseMoveEvent(self, event):
        key_id = keyboard_drawing.key_at(self._board(), self._layout_name, self._slot_of, event.position())
        self.setCursor(Qt.CursorShape.PointingHandCursor if key_id else Qt.CursorShape.ArrowCursor)
        self._set_hover(key_id)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._set_hover(None)

    def mousePressEvent(self, event):
        key_id = keyboard_drawing.key_at(self._board(), self._layout_name, self._slot_of, event.position())
        if key_id in self.caps and event.button() == Qt.MouseButton.LeftButton:
            self.select(key_id)
            self.caps[key_id].setFocus(Qt.FocusReason.MouseFocusReason)  # the keyboard goes on from this key

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        board = self._board()
        keys = {key["id"]: key for key in self._keys}
        changed = {key_id for key_id, (_text, on) in self._values.items() if on}
        kept = {key["id"] for key in self._keys if key_kept(key)}
        self._anchors = keyboard_drawing.paint(painter, board, self._layout_name, self._slot_of, keys, self._selected,
                                               self._hover, changed, kept)
        # the line from the callout of the key in question to its key, under the callouts as on the mouse's page
        for key_id in dict.fromkeys((self._hover, self._selected)):
            cap, end = self.caps.get(key_id), self._anchors.get(key_id)
            if cap is None or end is None:
                continue
            active = key_id == self._selected
            rect = QRectF(cap.geometry())
            above = rect.bottom() < board.top()
            start = QPointF(rect.center().x(), rect.bottom() if above else rect.top())
            elbow = QPointF(start.x(), board.top() - BOARD_GAP / 2 if above else board.bottom() + BOARD_GAP / 2)
            color = QColor(theme.ACCENT_INK if active else theme.MUTED)
            painter.setPen(QPen(color, 1.7 if active else 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath(start)
            path.lineTo(elbow)
            path.lineTo(QPointF(end.x(), elbow.y()))
            path.lineTo(end)
            painter.drawPath(path)
            painter.setPen(QPen(QColor(theme.BG), 1.2))
            painter.setBrush(color)
            radius = 4.2 if active else 3.2
            painter.drawEllipse(end, radius, radius)


class KeysPage(QWidget):
    """Laid out like the Buttons page: the keys on the left, the list of actions on the right."""

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._key = None
        self._keys = []
        self._name = None  # the keyboard's: which layout the picture has
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
        area = self.scroll = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(10)
        inner.addWidget(self.strip)
        self.restore = QPushButton(_("Restore keyboard defaults"))
        self.restore.setObjectName("flat")
        self.restore.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restore.clicked.connect(lambda: confirm_restore(self, self.store))
        inner.addWidget(self.restore, 0, Qt.AlignmentFlag.AlignRight)
        inner.addStretch(1)
        area.setWidget(holder)
        left.addWidget(area, 1)
        self.empty = hint_label(_("Turn the keyboard on to see its keys here. Its receiver can be the one of the mouse."))
        left.addWidget(self.empty)
        layout.addLayout(left, 1)

        self.picker = ActionPicker(allow_button_actions=True, exclude=KEYBOARD_EXCLUDE, titles=KEYBOARD_TITLES)
        layout.addWidget(self.picker)

        self.strip.selected.connect(self._select)
        self.strip.focused.connect(lambda cap: self.scroll.ensureWidgetVisible(cap, 0, 12))
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
            action = keys.get(key["id"], actions.DEFAULT)
            self.strip.set_value(key["id"], actions.describe(action), action != actions.DEFAULT)

    def _state(self, state):
        found = state.get("keyboard") or {}
        keys = found.get("keys") or []
        notice = notice_text(found)
        self.notice.setVisible(bool(notice))
        self.notice.setText(notice)
        self.empty.setVisible(not keys)
        self.restore.setVisible(bool(keys))
        if keys != self._keys or found.get("name") != self._name:
            self._keys, self._name = keys, found.get("name")
            self.strip.set_keys(keys, self._name)
            chain = [self.remove_app, *self.strip.caps.values(), self.restore]  # Tab: the app line, the keys, Restore
            for before, after in zip(chain, chain[1:]):
                QWidget.setTabOrder(before, after)
            self._refresh()
            chosen = first_key(keys, self._key["id"] if self._key else None)
            if chosen is not None:
                self.strip.select(chosen)


class ChannelTile(QFrame):
    """One Easy-Switch channel: its number, the computer on it, and whether that is this one. Only shown: the
    keyboard's own Easy-Switch keys move it between channels."""

    def __init__(self, index, host, current, parent=None):
        super().__init__(parent)
        self.setObjectName("channelCurrent" if current else "channel")
        self.setMinimumHeight(58)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        number = QLabel(str(index + 1))
        number.setObjectName("channelNumberCurrent" if current else "channelNumber")
        number.setFixedSize(26, 26)
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(number)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.name = QLabel(host["name"] or (_("Paired") if host["paired"] else _("Empty")))
        self.name.setObjectName("channelName" if host["name"] or current else "rowHint")
        text.addWidget(self.name)
        if current:
            caption = QLabel(_("This computer"))
            caption.setObjectName("rowHint")
            text.addWidget(caption)
        layout.addLayout(text, 1)
        self.setAccessibleName(_("Channel {}: {}").format(index + 1, self.name.text())
                               + (" · " + _("This computer") if current else ""))


class KeyboardPage(ScrollPage):
    def __init__(self, store, parent=None):
        super().__init__(_("Keyboard"), None, parent)
        self.store = store
        self.notice = QLabel()
        self.notice.setObjectName("banner")
        self.notice.setWordWrap(True)
        self.notice.hide()
        self.body.addWidget(self.notice)

        card = Card()  # which keyboard, and how it is: its name, battery and firmware at a glance
        self.name = QLabel()
        self.name.setObjectName("heroName")
        card.add_plain(self.name)
        status = QWidget()
        line = QHBoxLayout(status)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        self.battery_icon = BatteryIcon()
        line.addWidget(self.battery_icon)
        self.battery = QLabel()
        self.battery.setObjectName("deviceStatus")
        line.addWidget(self.battery, 1)
        card.add_plain(status)
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

        self.backlight_card = Card(_("Backlight"), hint=_(BACKLIGHT_HINT))
        self.body.addWidget(self.backlight_card)
        card = Card(_("Typing"), hint=_(
            "Settings the keyboard controls itself. Changes are made on the keyboard. Its own shortcuts may change "
            "them too."))
        self.fn_switch = Switch()
        self.fn_switch.setAccessibleName(_("Use F1–F12 without Fn"))
        self.fn_switch.toggled.connect(lambda on: self.store.engine.set_keyboard_setting("fn", on))
        self.platform_box = QComboBox()
        self.platform_box.setAccessibleName(_("Operating system layout"))
        self.platform_box.activated.connect(self._platform_chosen)
        self._choices = []  # the layouts in self.platform_box, as the keyboard offers them
        self.backlight_switch = Switch()
        self.backlight_switch.setAccessibleName(_("Backlight on"))
        self.backlight_switch.toggled.connect(lambda on: self._backlight_asks({"on": on}))
        self.lock_notices = Switch()  # a setting of this program's: the keyboard is never asked anything for it
        self.lock_notices.setAccessibleName(_("Show a notice when a lock key turns on or off"))
        self.lock_notices.toggled.connect(lambda on: self.store.set("keyboard", "lock_notices", on))
        controls = {"fn": self.fn_switch, "platform": self.platform_box, "backlight": self.backlight_switch,
                    "locks": self.lock_notices}
        self.settings_rows, self.disable_switches, self.disable_rows = {}, {}, {}
        for name, (title, text) in SETTINGS.items():
            home = self.backlight_card if name == "backlight" else card
            self.settings_rows[name] = home.add(Row(_(title), None, controls.get(name), note=_(text)))
            if name == "fn":  # its notice, a setting of this program's, under it
                notice = _("Show a notice when Fn lock changes on the keyboard")
                self.fn_notices = Switch()
                self.fn_notices.setAccessibleName(notice)
                self.fn_notices.toggled.connect(lambda on: self.store.set("keyboard", "fn_notices", on))
                self.fn_notices_row = card.add(Row(notice, None, self.fn_notices))
            if name == "disable":  # a row of its own for each key it can turn off, under it
                for key, label, cap in DISABLE_TITLES:
                    switch = self.disable_switches[key] = Switch()
                    switch.setAccessibleName(_("Turn off the {} key").format(cap))
                    switch.toggled.connect(lambda on, key=key: self.store.engine.set_keyboard_setting("disable", {key: on}))
                    self.disable_rows[key] = card.add(Row(_(label), None, switch))
            if name == "backlight":
                self._add_backlight(self.backlight_card)
        self.settings_rows["platform"].title.setBuddy(self.platform_box)
        self.settings_card = card
        self.body.addWidget(card)

        card = Card(_("Firmware & reset"))
        self.firmware_row = card.add(Row(_("Firmware"), None, None, note=" "))
        self.firmware = FirmwareQuery(self)
        self.firmware.answered.connect(self._firmware_answered)
        self._firmware_entries, self._firmware_for = [], None
        reset = QPushButton(_("Restore keyboard defaults"))
        reset.setCursor(Qt.CursorShape.PointingHandCursor)
        reset.clicked.connect(self._reset)
        card.add(Row(_("Every key does what it always does"), None, reset))
        self.body.addWidget(card)
        self.finish()

        store.state_changed.connect(self._state)
        store.changed.connect(self._show_notice_switches)
        self._state(store.state)

    def _show_notice_switches(self):
        """The notice switches as the settings have them; showing them changes nothing."""
        cfg = self.store.cfg["keyboard"]
        self.notices.set_checked_silently(cfg.get("notices") is True)
        self.fn_notices.set_checked_silently(cfg.get("fn_notices") is True)
        self.lock_notices.set_checked_silently(cfg.get("lock_notices") is True)

    def _add_backlight(self, card):
        """The rows under the Backlight row: brightness, its level, the timers, low battery, the notice."""
        self.backlight_rows = {}
        self.brightness = Segmented([("auto", _("Automatic")), ("manual", _("Fixed"))])
        self.brightness.setAccessibleName(_("Brightness"))
        for button, name in zip(self.brightness.findChildren(QPushButton), (_("Automatic brightness"), _("Fixed brightness"))):
            button.setAccessibleName(name)
        self.brightness.selected.connect(lambda mode: self._backlight_asks({"mode": mode}))
        self.backlight_rows["mode"] = card.add(Row(_("Brightness"), None, self.brightness))
        self.level = SliderRow(_("Brightness level"), 0, 7, 1, low_label=_("Dimmer"), high_label=_("Brighter"))
        self.level.slider.setAccessibleName(_("Backlight brightness level"))
        # the arrow and Page keys, or a click beside the handle, ask at once; a drag only once let go
        self.level.changed.connect(lambda value: None if self.level.slider.isSliderDown() else self._level_chosen(value))
        self.level.released.connect(self._level_chosen)
        self._levels = None  # the number of levels the slider has
        self.backlight_rows["level"] = card.add(self.level)
        self.timer_boxes = {}
        for key, title, name, hint in TIMER_ROWS:
            box = self.timer_boxes[key] = QComboBox()
            box.setAccessibleName(_(name))
            box.activated.connect(lambda position, key=key: self._timer_chosen(key, position))
            self.backlight_rows[key] = card.add(Row(_(title), _(hint), box))
            self.backlight_rows[key].title.setBuddy(box)
        self.power_save = Switch()
        self.power_save.setAccessibleName(_("Off at low battery"))
        self.power_save.setEnabled(False)  # shown only
        self.backlight_rows["power_save"] = card.add(Row(_("Off at low battery"), _(POWER_SAVE_HINT), self.power_save))
        title = _("Show a notice when the brightness changes on the keyboard")
        self.notices = Switch()
        self.notices.setAccessibleName(title)
        self.notices.toggled.connect(lambda on: self.store.set("keyboard", "notices", on))
        self.backlight_rows["notices"] = card.add(Row(title, None, self.notices))

    def _row_visible(self, row, shown):
        """Show or hide a row of the Backlight or the Typing card, in whichever it is."""
        card = self.backlight_card if row in self.backlight_card._rows else self.settings_card
        card.set_row_visible(row, shown)

    def _backlight(self):
        return ((self.store.state.get("keyboard") or {}).get("controls") or {}).get("backlight") or {}

    def _backlight_asks(self, change):
        self.store.engine.set_keyboard_setting("backlight", change)

    def _level_chosen(self, position):
        control = self._backlight()
        level = int(position)  # the slider says a float; the keyboard takes whole levels only
        if level != (control.get("value") or {}).get("level") or control.get("busy"):  # busy: the last one counts
            self._backlight_asks({"level": level})

    def _timer_chosen(self, key, position):
        seconds = self.timer_boxes[key].itemData(position)
        if seconds is not None and seconds != (self._backlight().get("value") or {}).get(key):
            self._backlight_asks({key: seconds})

    def _show_note(self, name, control, hint, otherwise=""):
        """The note and hint of setting ``name``'s row: its general words while MX Options cannot change it, else how
        its last change went, or ``otherwise``."""
        row = self.settings_rows[name]
        if control is None:
            row.set_hint("")
            row.note.setText(_(SETTINGS[name][1]))
            row.note.show()
            return
        row.set_hint(_(hint))
        outcome = OUTCOMES.get(control.get("outcome"))
        if outcome is None and control.get("value") is None:
            outcome = OUTCOMES["unknown"]
        text = _(outcome) if outcome else otherwise
        row.note.setText(text)
        row.note.setVisible(bool(text))

    @staticmethod
    def _changeable(control):
        return control.get("value") is not None and bool(control.get("writable")) and not control.get("busy")

    def _show_fn(self, control):
        """The Fn lock row: a switch while the keyboard lets it be changed, its general hint otherwise. What it shows
        is always what the keyboard said last; one asked for is shown as such until the keyboard answers."""
        self.fn_switch.setVisible(control is not None)
        self._show_note("fn", control, FN_HINT)
        if control is None:
            return
        if not control.get("busy"):
            self.fn_switch.set_checked_silently(bool(control.get("value")))
        self.fn_switch.setEnabled(self._changeable(control))

    def _show_disable(self, control, shown):
        """The keys the keyboard can turn off, a switch each (on: turned off), as for Fn lock."""
        self._show_note("disable", control, DISABLE_HINT, _(DISABLE_NOTE))
        keys = (control or {}).get("keys") or []
        off = (control or {}).get("value") or []
        for key, switch in self.disable_switches.items():
            self._row_visible(self.disable_rows[key], shown and key in keys)
            if control is None:
                continue
            if not control.get("busy"):
                switch.set_checked_silently(key in off)
            switch.setEnabled(self._changeable(control))

    def _show_backlight(self, control, shown, blocked):
        """The Backlight row and those under it, each where the keyboard has it, changeable as it lets it be. The slider
        stays where it is moved while the change is under way, and can still be moved; it asks for whole levels."""
        value, fields = (control or {}).get("value") or {}, (control or {}).get("fields") or []
        can = (control or {}).get("can") or [] if (control or {}).get("writable") else []
        mode, busy, levels = value.get("mode"), bool((control or {}).get("busy")), (control or {}).get("levels")
        note = ""
        if not value:
            pass
        elif not control.get("writable") and not blocked:
            note = "read-only"
        elif mode == "keys":
            note = "keys" if "mode" in fields else "keys-only"
        elif mode is None:
            note = "mode-unknown"
        elif not {"level", "hands_out", "hands_in", "powered"} & set(fields):
            note = "untimed"
        self.backlight_switch.setVisible(control is not None)
        self._show_note("backlight", control, BACKLIGHT_HINT, _(BACKLIGHT_NOTES[note]) if note else "")
        for key, row in self.backlight_rows.items():
            here = key in fields if key != "notices" else bool(value) and ("level" in fields or levels is not None)
            self._row_visible(row, shown and here)
        if control is None:
            return
        self.notices.set_checked_silently(self.store.cfg["keyboard"].get("notices") is True)
        if not busy:
            self.backlight_switch.set_checked_silently(bool(value.get("on")))
            self.brightness.set_value(mode)  # none chosen while set with its keys, or not known
        self.backlight_switch.setEnabled(self._changeable(control) and "on" in can)
        self.brightness.setEnabled(self._changeable(control) and "mode" in can)
        self.power_save.set_checked_silently(bool(value.get("power_save")))
        if levels and levels != self._levels:
            self._levels = levels
            self.level.set_range(0, levels - 1, 1)
            self.level.info.set_text(_(LEVEL_HINT).format(top=levels - 1))
        if not busy and not self.level.slider.isSliderDown() and value.get("level") is not None:
            self.level.set_value(value["level"])
        self.level.setEnabled(bool(control.get("writable")) and "level" in can and mode == "manual")
        for key, box in self.timer_boxes.items():
            seconds = value.get(key)
            if seconds is None:
                items = [(_("Not known"), None)]
            else:
                items = [(duration_text(stop), stop) for stop in sorted(set(TIMER_STOPS) | {seconds})]
            if [(box.itemText(i), box.itemData(i)) for i in range(box.count())] != items:
                box.blockSignals(True)
                box.clear()
                for text, stop in items:
                    box.addItem(text, stop)
                box.blockSignals(False)
            if not busy:
                box.blockSignals(True)
                box.setCurrentIndex(box.findData(seconds) if seconds is not None else 0)
                box.blockSignals(False)
            box.setEnabled(self._changeable(control) and key in can)

    def _show_platform(self, control, blocked):
        """The layout for this computer's Easy-Switch channel, picked from those the keyboard offers."""
        self.platform_box.setVisible(control is not None)
        read_only = control is not None and control.get("value") is not None and not control.get("writable") and not blocked
        self._show_note("platform", control, PLATFORM_HINT, _(OUTCOMES["unsupported"]) if read_only else "")
        if control is None:
            return
        choices = control.get("choices") or []
        if choices != self._choices:
            self._choices = choices
            self.platform_box.blockSignals(True)
            self.platform_box.clear()
            self.platform_box.addItems(layout_labels(choices))
            self.platform_box.blockSignals(False)
        if not control.get("busy"):
            index = next((i for i, choice in enumerate(choices) if choice["index"] == control.get("value")), -1)
            self.platform_box.blockSignals(True)
            self.platform_box.setCurrentIndex(index)
            self.platform_box.blockSignals(False)
        self.platform_box.setEnabled(self._changeable(control) and bool(choices))

    def _platform_chosen(self, position):
        control = ((self.store.state.get("keyboard") or {}).get("controls") or {}).get("platform") or {}
        if 0 <= position < len(self._choices) and self._choices[position]["index"] != control.get("value"):
            self.store.engine.set_keyboard_setting("platform", self._choices[position]["index"])

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
        confirm_restore(self, self.store)

    def _state(self, state):
        found = state.get("keyboard") or {}
        connected = bool(found.get("connected"))
        notice = notice_text(found)
        self.notice.setVisible(bool(notice))
        self.notice.setText(notice)
        settings = found.get("settings") or []
        for name, row in self.settings_rows.items():
            self._row_visible(row, name in settings)
        self.backlight_card.setVisible("backlight" in settings)
        self.settings_card.setVisible(any(name in SETTINGS and name != "backlight" for name in settings))
        controls = (found.get("controls") or {}) if connected else {}
        self._show_fn(controls.get("fn"))
        self._row_visible(self.fn_notices_row, "fn" in settings and controls.get("fn") is not None)
        self._show_notice_switches()
        self._show_disable(controls.get("disable"), "disable" in settings)
        self._show_backlight(controls.get("backlight"), "backlight" in settings, bool(found.get("reason")))
        self._show_platform(controls.get("platform"), bool(found.get("reason")))
        battery = found.get("battery") if connected else None
        self.name.setText(found.get("name") or _("Keyboard"))
        self.battery_icon.set_level(*(battery[0], battery[1] in CHARGING or battery[1] == device.BATTERY_FULL)
                                    if battery else (None, False))
        self.battery_icon.setVisible(bool(battery))
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
                self.hosts_layout.addWidget(ChannelTile(index, host, index == found.get("current_host")), 1)
        if found.get("name") != self._firmware_for:
            self._firmware_entries = []  # fwupd's answer about another keyboard
        if self.isVisible() and connected and self._firmware_for != found.get("name"):
            self._ask_firmware()
        self._show_firmware()
