"""The keyboard as Logitech's own product picture, with the keys whose action can be set marked on it.

The pictures are Logitech's official top views of the MX Mechanical and the MX Mechanical Mini. They are Logitech's
property and are not shipped with this program: it fetches them from logitech.com when the user asks for them and
keeps them in the user's data directory, as it does for the mouse (mouse_photo). The key grid of each was measured on
it by hand. A key has the slot keyboard_drawing.slots() gives it; this module only says where that slot is on the
picture."""

import os

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen

from .. import paths
from . import keyboard_drawing, mouse_photo, theme

PHOTO_DIR = os.path.join(paths.DATA, "keyboard")
_BASE = ("https://resource.logitech.com/w_2400,c_limit,q_auto:best,f_png/d_transparent.gif/content/dam/logitech/en/"
         "products/keyboards/mx-mechanical/")
# keyboard name -> (file, where the picture comes from, its size, the part that holds the keyboard (left, top, width,
# height, in px of that size), where the grid starts in that part (px), one key unit (px), the drawing's layout)
PHOTOS = {
    "MX Mechanical": ("mx-mechanical.png", _BASE + "migration-assets-for-delorean-2025/gallery/"
                      "mx-mechanical-top-view-graphite-us.png", (2400, 1350), (256, 378, 1889, 574), (7.5, 53.2), 83.8,
                      "full"),
    "MX Mechanical Mini": ("mx-mechanical-mini.png", _BASE + "gallery/mx-mechanical-mini/"
                           "mx-mechanical-mini-mini-keyboard-top-view-graphite-us.png", (1600, 800),
                           (236, 150, 1133, 479), (11.4, 43.6), 69.4, "compact"),
}
# Where a key of the Mini is on its picture when not where the compact drawing has it (key units): the F-row has no
# gaps, Delete ends it, Home, End, PgUp and PgDn go down the right side in that order, Fn sits between Alt and Ctrl
_MINI_PLACES = {**{f"f{n}": (n, 0) for n in range(1, 13)}, "delete": (15, 0), "home": (15, 1), "end": (15, 2),
                "pgup": (15, 3), "pgdn": (15, 4), "rctrl": (12, 5)}

_images = {}  # name -> QImage
_scaled = {}  # (name, width, height in device pixels, lit) -> QImage


def photo_path(name):
    return os.path.join(PHOTO_DIR, PHOTOS[name][0])


def available(name):
    """Whether there is a picture of the keyboard called ``name`` and it has been fetched."""
    return name in PHOTOS and os.path.exists(photo_path(name))


def shown(name, picture):
    """Whether the picture of ``name`` is shown instead of the drawing: fetched, and ``picture`` (the setting
    keyboard.picture) is "photo"."""
    return picture == "photo" and available(name)


def offered(name):
    """Whether a picture of the keyboard called ``name`` can be fetched and has not been yet."""
    return name in PHOTOS and not available(name)


def download():
    """Fetch the pictures from logitech.com, cut the keyboard out of them and store them. Raises OSError (which covers
    network errors) or ValueError if what comes back is not the picture expected."""
    os.makedirs(PHOTO_DIR, exist_ok=True)
    for name, (_file, url, size, crop, _origin, _unit, _layout) in PHOTOS.items():
        data = mouse_photo.fetch(url)
        image = QImage.fromData(data)
        # another size of the same picture is fine; another picture (another shape) would put the keys elsewhere
        if (image.isNull() or not image.hasAlphaChannel() or image.width() < size[0] // 2
                or abs(image.width() / image.height() - size[0] / size[1]) > 0.01):
            raise ValueError(f"unexpected picture for {name}")
        scale = image.width() / size[0]
        part = QRect(*(round(value * scale) for value in crop))
        picture = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied).copy(part).scaled(
            crop[2], crop[3], Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        path = photo_path(name)
        if not picture.save(path + ".tmp", "PNG"):
            raise OSError(f"could not write {path}")
        os.replace(path + ".tmp", path)
    _images.clear()
    _scaled.clear()


def Downloader(parent=None):
    """Runs download() off the GUI thread; ``finished`` carries an error message, empty on success."""
    return mouse_photo.Downloader(parent, download, "keyboard")


def grid(name):
    """Slot -> its place on the picture of ``name``, in key units from where the grid starts."""
    layout = PHOTOS[name][6]
    places = {}
    for slot, rect, _legend in keyboard_drawing.board(layout):
        if not slot:
            continue
        rect = QRectF(rect)
        if rect.top() >= keyboard_drawing.MAIN_TOP:  # no gap under the F-row on the real keyboards
            rect.translate(0, 1 - keyboard_drawing.MAIN_TOP)
        if layout == "full" and rect.left() >= 18:  # the number pad sits a little closer to the navigation keys
            rect.translate(-0.15, 0)
        if layout == "compact" and slot in _MINI_PLACES:
            rect.moveTopLeft(QRectF(*_MINI_PLACES[slot], 1, 1).topLeft())
        places[slot] = rect
    return places


def fit(width, top, name):
    """The picture's rect: as wide as ``width`` (at most keyboard_drawing.MAX_UNIT px a key), centred, from ``top``."""
    _file, _url, _size, crop, _origin, unit, _layout = PHOTOS[name]
    scale = min(width / crop[2], keyboard_drawing.MAX_UNIT / unit)
    return QRectF((width - crop[2] * scale) / 2, top, crop[2] * scale, crop[3] * scale)


def _cap(rect, name, place):
    _file, _url, _size, crop, origin, unit, _layout = PHOTOS[name]
    scale = rect.width() / crop[2]
    step, gap = unit * scale, unit * scale * 0.05
    return QRectF(rect.left() + (origin[0] + place.left() * unit) * scale + gap,
                  rect.top() + (origin[1] + place.top() * unit) * scale + gap,
                  place.width() * step - 2 * gap, place.height() * step - 2 * gap)


def key_rects(rect, name, slot_of):
    """Key id -> the rect of its cap in px, on the picture of ``name`` drawn in ``rect``."""
    places = grid(name)
    return {key_id: _cap(rect, name, places[slot]) for key_id, slot in slot_of.items() if slot in places}


def _picture(name, size, ratio, lit=None):
    """The picture at ``size`` (px, times ``ratio`` device pixels); ``lit`` as paint() takes it."""
    key = (name, max(1, int(round(size.width() * ratio))), max(1, int(round(size.height() * ratio))), lit)
    if key not in _scaled:
        if name not in _images:
            _images[name] = QImage(photo_path(name)).convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        if len(_scaled) > 8:
            _scaled.clear()
        image = _images[name].scaled(QSize(key[1], key[2]), Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        if lit is not None:
            painter = QPainter(image)
            if lit:  # the light legends brighter, the dark caps hardly: the backlight's glow
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
                painter.setOpacity(0.15)
                painter.drawImage(0, 0, image.copy())
            else:  # darker, the keyboard's shape kept
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
                painter.fillRect(image.rect(), QColor(0, 0, 0, 120))
            painter.end()
        _scaled[key] = image
    return _scaled[key]


def paint(painter, rect, name, slot_of, selected=None, hover=None, changed=frozenset(), kept=frozenset(), lit=None):
    """Draw the picture of ``name`` in ``rect`` and mark on it the keys of ``slot_of``, as keyboard_drawing.paint()
    does on its drawing; ``lit`` True or False: its backlight on (legends glow) or off (all of it darker), None: not
    said. Returns key id -> the rect of its cap."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.drawImage(rect, _picture(name, rect.size(), painter.device().devicePixelRatioF(), lit))
    accent = QColor(theme.ACCENT_INK if theme.DARK else theme.ACCENT)  # light enough on the dark grey keyboard
    rects = key_rects(rect, name, slot_of)
    for key_id, cap in rects.items():
        radius = cap.height() * 0.12
        outline = cap.adjusted(-1, -1, 1, 1)
        if key_id == selected:
            fill = QColor(accent)
            fill.setAlpha(90)
            halo = QColor(accent)
            halo.setAlpha(70)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(halo, 5.0))
            painter.drawRoundedRect(outline, radius, radius)
            painter.setBrush(fill)
            painter.setPen(QPen(accent, 2.0))
        elif key_id in kept:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(255, 255, 255, 120), 1.0, Qt.PenStyle.DashLine))
        else:  # a key whose action can be set: outlined, as in Options+
            painter.setBrush(QColor(255, 255, 255, 40) if key_id == hover else Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(255, 255, 255, 255 if key_id == hover else 190), 2.0 if key_id == hover else 1.4))
        painter.drawRoundedRect(outline, radius, radius)
        unit = cap.height()
        if key_id in kept:  # a padlock: the keyboard keeps this key
            size = unit * 0.2
            body = QRectF(cap.right() - size - unit * 0.1, cap.bottom() - size * 0.8 - unit * 0.1, size, size * 0.7)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 170))
            painter.drawRoundedRect(body, 1, 1)
            painter.setPen(QPen(QColor(255, 255, 255, 170), max(1.0, unit * 0.035)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(QRectF(body.left() + size * 0.15, body.top() - size * 0.45, size * 0.7, size * 0.9), 0, 180 * 16)
        elif key_id in changed:  # set to something of its own: a dot of the accent
            dot = max(2.5, unit * 0.08)
            painter.setPen(QPen(QColor(0, 0, 0, 150), 1.0))
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawEllipse(QRectF(cap.right() - unit * 0.1 - 2 * dot, cap.top() + unit * 0.1, 2 * dot, 2 * dot))
    painter.restore()
    return rects
