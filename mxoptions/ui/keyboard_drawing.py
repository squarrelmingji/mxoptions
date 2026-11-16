"""A keyboard drawn from above, as the Keys page shows it: the keys a key's action can be set for marked where they
sit, like the buttons on the picture of the mouse.

Two generic layouts, built from rectangles in key units: "full" (with navigation keys and a number pad) and
"compact" (a Mini: no number pad, navigation keys down the right side). A key is drawn where it is only when that
is known: from the F-key the keyboard says it is on (getCidInfo), or from a control id that names one key every
layout has in the same place (CID_SLOTS). Any other key has no place on the picture."""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen

from . import theme

LAYOUTS = {"full": 22.5, "compact": 16.5}  # layout -> width of the board, in key units
ROWS = 6.5  # height of the board in key units: the F-row, a gap, five rows
# Control ids of keys every layout has in the same place (Solaar's names for them) -> slot
CID_SLOTS = {
    0x010B: "grave", 0x010C: "tab", 0x010D: "caps", 0x010E: "lshift", 0x010F: "lctrl", 0x0110: "lwin",
    0x0111: "lalt", 0x0112: "ralt", 0x0113: "rwin", 0x0114: "rctrl", 0x0115: "rshift", 0x0116: "insert",
    0x0117: "delete", 0x0118: "home", 0x0119: "end", 0x011A: "pgup", 0x011B: "pgdn", 0x011E: "backslash",
}
# What a slot's key says on it (keycap legends: the same in every language)
LEGENDS = {"esc": "Esc", "tab": "Tab", "caps": "Caps", "lshift": "Shift", "rshift": "Shift", "lctrl": "Ctrl",
           "rctrl": "Ctrl", "lalt": "Alt", "ralt": "Alt", "insert": "Ins", "delete": "Del", "home": "Home",
           "end": "End", "pgup": "PgUp", "pgdn": "PgDn", "backspace": "⌫", "enter": "⏎", "grave": "`",
           "backslash": "\\", **{f"f{n}": f"F{n}" for n in range(1, 13)}}
MAIN_TOP = 1.5  # key units: where the rows under the F-row begin


def _row(y, start, widths):
    """Keys side by side from ``start``: each (slot, width in units)."""
    keys, x = [], start
    for slot, width in widths:
        keys.append((slot, QRectF(x, y, width, 1)))
        x += width
    return keys


def _main(bottom):
    """The main block, 15 units wide: five rows under the F-row; ``bottom`` the slots of the bottom row."""
    y = [MAIN_TOP + i for i in range(5)]
    return (_row(y[0], 0, [("grave", 1)] + [("", 1)] * 12 + [("backspace", 2)])
            + _row(y[1], 0, [("tab", 1.5)] + [("", 1)] * 12 + [("backslash", 1.5)])
            + _row(y[2], 0, [("caps", 1.75)] + [("", 1)] * 11 + [("enter", 2.25)])
            + _row(y[3], 0, [("lshift", 2.25)] + [("", 1)] * 10 + [("rshift", bottom[0])])
            + _row(y[4], 0, bottom[1]))


def board(layout):
    """Every key of ``layout`` as (slot or "", rect in key units)."""
    if layout == "compact":
        f_row = [("esc", QRectF(0, 0, 1, 1))] + [
            (f"f{n}", QRectF(1.25 + (n - 1) + 0.25 * ((n - 1) // 4), 0, 1, 1)) for n in range(1, 13)]
        f_row.append(("delete", QRectF(15.5, 0, 1, 1)))
        keys = f_row + _main((1.75, [("lctrl", 1.25), ("lwin", 1.25), ("lalt", 1.25), ("", 6.25), ("ralt", 1),
                                     ("rctrl", 1)]))
        keys += [(slot, QRectF(15.5, MAIN_TOP + row, 1, 1)) for row, slot in enumerate(("home", "pgup", "pgdn", "end"))]
        keys += [("", QRectF(14.5, MAIN_TOP + 3, 1, 1))]  # up
        keys += [("", QRectF(13.5 + i, MAIN_TOP + 4, 1, 1)) for i in range(3)]  # left, down, right
        return keys
    f_row = [("esc", QRectF(0, 0, 1, 1))] + [
        (f"f{n}", QRectF(2 + (n - 1) + 0.5 * ((n - 1) // 4), 0, 1, 1)) for n in range(1, 13)]
    f_row += [("", QRectF(15.25 + i, 0, 1, 1)) for i in range(3)]  # print screen, scroll lock, pause
    f_row += [("", QRectF(18.5 + i, 0, 1, 1)) for i in range(4)]   # the keys over the number pad
    keys = f_row + _main((2.75, [("lctrl", 1.25), ("lwin", 1.25), ("lalt", 1.25), ("", 6.25), ("ralt", 1.25),
                                 ("rwin", 1.25), ("", 1.25), ("rctrl", 1.25)]))
    for row, slots in enumerate((("insert", "home", "pgup"), ("delete", "end", "pgdn"))):
        keys += [(slot, QRectF(15.25 + i, MAIN_TOP + row, 1, 1)) for i, slot in enumerate(slots)]
    keys += [("", QRectF(16.25, MAIN_TOP + 3, 1, 1))]  # up
    keys += [("", QRectF(15.25 + i, MAIN_TOP + 4, 1, 1)) for i in range(3)]  # left, down, right
    pad = 18.5  # the number pad
    keys += [("", QRectF(pad + i, MAIN_TOP, 1, 1)) for i in range(4)]
    keys += [("", QRectF(pad + i, MAIN_TOP + row, 1, 1)) for row in (1, 2, 3) for i in range(3)]
    keys += [("", QRectF(pad + 3, MAIN_TOP + 1, 1, 2)), ("", QRectF(pad + 3, MAIN_TOP + 3, 1, 2)),
             ("", QRectF(pad, MAIN_TOP + 4, 2, 1)), ("", QRectF(pad + 2, MAIN_TOP + 4, 1, 1))]
    return keys


def layout_for(name):
    """The layout drawn for a keyboard called ``name``: compact for a Mini, else full."""
    return "compact" if name and "Mini" in name else "full"


def slots(keys, layout):
    """Key id -> the slot it is drawn in, for the keys of ``keys`` whose place is known. Keys are taken in the
    order of their control ids, so neither the order of ``keys`` nor anything else about them changes it: a slot two
    keys claim stays with the lower id. Keys without a known place are left out."""
    there = {slot for slot, _rect in board(layout) if slot}
    taken, result = set(), {}
    for key in sorted(keys, key=lambda key: key["cid"]):
        pos = key.get("pos") or 0
        slot = f"f{pos}" if 1 <= pos <= 12 else CID_SLOTS.get(key["cid"])
        if slot in there and slot not in taken:
            taken.add(slot)
            result[key["id"]] = slot
    return result


def fit(area, layout):
    """The board's rect inside ``area``, centred, at the largest whole size that fits (at most 34 px a key), and the
    size of one key unit in px."""
    width = LAYOUTS[layout] + 0.8  # with the case's rim on both sides
    unit = min(area.width() / width, area.height() / (ROWS + 0.8), 34.0)
    rect = QRectF(0, 0, LAYOUTS[layout] * unit, ROWS * unit)
    rect.moveCenter(area.center())
    return rect, unit


def _cap(rect, unit, place):
    """A key's cap in px: ``place`` in key units, inside board ``rect``, with a gap all round."""
    gap = unit * 0.08
    return QRectF(rect.left() + place.left() * unit + gap, rect.top() + place.top() * unit + gap,
                  place.width() * unit - 2 * gap, place.height() * unit - 2 * gap)


def anchor_of(cap, slot):
    """Where a line to a key ends: the top edge of a key on the F-row (the lines come from above), the middle of any
    other."""
    return QPointF(cap.center().x(), cap.top()) if slot.startswith("f") or slot == "esc" else cap.center()


def paint(painter, area, layout, slot_of, keys, selected=None, hover=None, changed=frozenset(), kept=frozenset()):
    """Draw the keyboard inside ``area``; ``slot_of`` as slots() gives it, ``keys`` key id -> key; ``changed`` the
    ids of keys set to something of their own, ``kept`` of those left as they are. Returns key id -> the point its
    line ends at."""
    rect, unit = fit(area, layout)
    of_slot = {slot: key_id for key_id, slot in slot_of.items()}
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rim = unit * 0.4
    painter.setPen(QPen(QColor(theme.MODEL_EDGE), 1.2))
    painter.setBrush(QColor(theme.MODEL_BASE))
    painter.drawRoundedRect(rect.adjusted(-rim, -rim, rim, rim), unit * 0.45, unit * 0.45)
    font = QFont(painter.font())
    font.setPixelSize(max(7, int(unit * 0.27)))
    painter.setFont(font)
    anchors = {}
    for slot, place in board(layout):
        cap = _cap(rect, unit, place)
        key_id = of_slot.get(slot) if slot else None
        key = keys.get(key_id) if key_id else None
        locked = key_id in kept
        if key_id == selected:
            pen, fill = QPen(QColor(theme.ACCENT_INK), 1.6), QColor(theme.ACCENT)
        elif key_id is not None and key_id == hover:
            pen, fill = QPen(QColor(theme.ACCENT_INK), 1.6), QColor(theme.CARD_HOVER)
        elif key is not None and not locked:
            pen, fill = QPen(QColor(theme.mix(theme.MODEL_EDGE, theme.TEXT, 0.35)), 1.1), QColor(theme.CARD)
        else:
            pen, fill = QPen(QColor(theme.MODEL_EDGE), 0.8), QColor(theme.MODEL_DECAL)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawRoundedRect(cap, unit * 0.14, unit * 0.14)
        legend = LEGENDS.get(slot, "")
        if legend and unit >= 20:
            text = QColor(theme.ACCENT_TEXT if key_id == selected else theme.TEXT if key and not locked else theme.MUTED)
            if not (key and not locked) and key_id != selected:
                text.setAlpha(150)
            painter.setPen(text)
            painter.drawText(cap.adjusted(unit * 0.12, unit * 0.06, -unit * 0.08, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, legend)
        if key is None:
            continue
        anchors[key_id] = anchor_of(cap, slot)
        dot = QPointF(cap.center().x(), cap.bottom() - cap.height() * 0.28)
        painter.setPen(Qt.PenStyle.NoPen)
        if locked:  # a padlock: the keyboard keeps this key
            painter.setBrush(QColor(theme.MUTED))
            size = unit * 0.2
            body = QRectF(dot.x() - size / 2, dot.y() - size * 0.3, size, size * 0.7)
            painter.drawRoundedRect(body, 1, 1)
            painter.setPen(QPen(QColor(theme.MUTED), max(1.0, unit * 0.04)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(QRectF(body.left() + size * 0.15, body.top() - size * 0.45, size * 0.7, size * 0.9), 0,
                            180 * 16)
            continue
        # the hotspot: a key whose action can be set here (accent while it is set to something of its own)
        color = QColor(theme.ACCENT_TEXT if key_id == selected else theme.ACCENT if key_id in changed else theme.TEXT)
        painter.setBrush(color)
        radius = max(2.0, unit * 0.09)
        painter.drawEllipse(dot, radius, radius)
    painter.restore()
    return anchors


def key_at(area, layout, slot_of, pos):
    """The key id drawn at ``pos``, or None."""
    rect, unit = fit(area, layout)
    of_slot = {slot: key_id for key_id, slot in slot_of.items()}
    for slot, place in board(layout):
        if slot in of_slot and _cap(rect, unit, place).contains(pos):
            return of_slot[slot]
    return None
