#!/usr/bin/env python3
"""A display-only text box for Game Mode, drawn top-left (takes no input).

Started by main.py with the system python (GTK 3 via PyGObject). Reads JSON lines on stdin:
    {"title": "...", "lines": ["...", ...], "color": "#ffff77"}   show / replace the content
    {"hide": true}                                                  hide it (destroys the window)
Exits when stdin closes.

Usage: overlay.py DISPLAY [--external-overlay] [--fullscreen]
  --external-overlay  set GAMESCOPE_EXTERNAL_OVERLAY=1 (how mangoapp / OverLaid get drawn over every app)
  --fullscreen        a transparent window covering the whole screen with the box painted top-left (the
                      window shape mangoapp and OverLaid use); otherwise a window just the size of the box
The window always gets GAMESCOPE_NO_FOCUS=1, so gamescope never gives it input focus.
"""
import ctypes

import cairo
import json
import sys
import threading

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk  # noqa: E402,F401  (GdkX11 provides get_xid)

args = [a for a in sys.argv[1:] if not a.startswith("--")]
DISPLAY = args[0] if args else ":0"
EXTERNAL = "--external-overlay" in sys.argv
FULLSCREEN = "--fullscreen" in sys.argv
MARGIN, PAD, TITLE_SIZE, LINE_SIZE = 24, 14, 18, 15
SEP_GAP = 8  # header divider
TITLE_FONT = "Noto Serif"

Gdk.set_allowed_backends("x11")
Gtk.init([sys.argv[0], f"--display={DISPLAY}"])

state = {"title": "", "lines": [], "color": (1.0, 1.0, 0.47), "visible": False}
screen = Gdk.Screen.get_default()
rgba = screen.get_rgba_visual()
win = None  # created on show, destroyed on hide (see apply)


def hex_color(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4)) if len(s) == 6 else (1.0, 1.0, 0.47)


def box_size(cr=None):
    """Width/height of the text box; measured with cairo when a context is available."""
    lines = [state["title"]] + state["lines"]
    if cr is None:  # measure off-screen so the window fits the drawn box
        cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    width = 0
    for i, line in enumerate(lines):
        cr.select_font_face(TITLE_FONT if i == 0 else "Sans", 0, 1 if i <= 1 else 0)
        cr.set_font_size(TITLE_SIZE if i == 0 else LINE_SIZE)
        width = max(width, cr.text_extents(line).x_advance)
    width += 2 * PAD
    height = PAD + TITLE_SIZE + 6 + SEP_GAP + len(state["lines"]) * (LINE_SIZE + 6) + PAD - 2
    return int(max(width, 240)), int(height)


def on_draw(_w, cr):
    cr.set_operator(0)  # CLEAR: fully transparent everywhere
    cr.paint()
    cr.set_operator(2)  # OVER
    if not state["visible"]:
        return
    x0 = y0 = MARGIN if FULLSCREEN else 0
    w, h = box_size(cr)
    # Path of Exile tooltip look: near-black box, gold-brown frame, rarity-tinted header with a serif title
    rgb = state["color"]
    cr.set_source_rgba(0.025, 0.022, 0.018, 0.95)
    cr.rectangle(x0, y0, w, h)
    cr.fill()
    band = PAD + TITLE_SIZE + 4
    cr.set_source_rgb(*(0.05 + c * 0.16 for c in rgb))
    cr.rectangle(x0 + 2, y0 + 2, w - 4, band)
    cr.fill()
    cr.set_source_rgb(0.47, 0.37, 0.22)
    cr.set_line_width(2)
    cr.rectangle(x0 + 1, y0 + 1, w - 2, h - 2)
    cr.stroke()
    y = y0 + PAD + TITLE_SIZE - 2
    cr.select_font_face(TITLE_FONT, 0, 1)
    cr.set_font_size(TITLE_SIZE)
    cr.set_source_rgb(*rgb)
    cr.move_to(x0 + (w - cr.text_extents(state["title"]).x_advance) / 2, y)
    cr.show_text(state["title"])
    # divider fading out to both sides
    sep_y = y0 + band + 2
    g = cairo.LinearGradient(x0, 0, x0 + w, 0)
    for stop, a in ((0, 0), (0.5, 0.9), (1, 0)):
        g.add_color_stop_rgba(stop, *(c * 0.75 for c in rgb), a)
    cr.set_source(g)
    cr.rectangle(x0 + 4, sep_y, w - 8, 1)
    cr.fill()
    y += SEP_GAP
    for i, line in enumerate(state["lines"]):
        y += LINE_SIZE + 6
        cr.select_font_face("Sans", 0, 1 if i == 0 else 0)
        cr.set_font_size(LINE_SIZE)
        cr.set_source_rgb(*((0.95, 0.84, 0.55) if i == 0 else (0.62, 0.62, 0.62)))
        cr.move_to(x0 + (w - cr.text_extents(line).x_advance) / 2, y)
        cr.show_text(line)


def set_gamescope_props(w):
    xid = w.get_window().get_xid()
    V, UL = ctypes.c_void_p, ctypes.c_ulong
    x = ctypes.CDLL("libX11.so.6")
    x.XOpenDisplay.restype = V
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XInternAtom.restype = UL
    x.XInternAtom.argtypes = [V, ctypes.c_char_p, ctypes.c_int]
    x.XChangeProperty.argtypes = [V, UL, UL, UL, ctypes.c_int, ctypes.c_int, V, ctypes.c_int]
    x.XFlush.argtypes = [V]
    x.XCloseDisplay.argtypes = [V]
    d = x.XOpenDisplay(DISPLAY.encode())
    if not d:
        return None
    one = (ctypes.c_long * 1)(1)
    names = [b"GAMESCOPE_NO_FOCUS"] + ([b"GAMESCOPE_EXTERNAL_OVERLAY"] if EXTERNAL else [])
    for name in names:
        x.XChangeProperty(d, xid, x.XInternAtom(d, name, 0), 6, 32, 0, one, 1)  # XA_CARDINAL, Replace
    x.XFlush(d)
    x.XCloseDisplay(d)
    return hex(xid)


def make_window():
    """New override-redirect window with the gamescope properties set before it is first mapped."""
    w = Gtk.Window(type=Gtk.WindowType.POPUP)
    if rgba is not None:
        w.set_visual(rgba)
    w.set_app_paintable(True)
    w.connect("draw", on_draw)
    w.realize()  # create the X window so the properties are there before it is first mapped
    # set_gamescope_props uses its own X connection: make sure GTK's CreateWindow has reached the server
    # first, otherwise the properties are rejected (BadWindow) without any visible error.
    Gdk.Display.get_default().sync()
    set_gamescope_props(w)
    return w


def apply(msg):
    global win
    if msg.get("hide"):
        state["visible"] = False
        # Destroy, don't just unmap: gamescope keeps drawing an unmapped external overlay's last frame
        # until the window is destroyed.
        if win is not None:
            win.destroy()
            win = None
            Gdk.Display.get_default().flush()
        return False
    state.update(title=msg.get("title", ""), lines=list(msg.get("lines", [])),
                 color=hex_color(msg.get("color", "#ffff77")), visible=True)
    if win is None:
        win = make_window()
    if FULLSCREEN:
        win.move(0, 0)
        win.resize(screen.get_width(), screen.get_height())
    else:
        w, h = box_size()
        win.move(MARGIN, MARGIN)
        win.resize(w, h)
    win.show_all()
    win.queue_draw()
    return False


def reader():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        GLib.idle_add(apply, msg)
    GLib.idle_add(Gtk.main_quit)


threading.Thread(target=reader, daemon=True).start()
print(json.dumps({"ready": True, "display": DISPLAY, "external": EXTERNAL, "fullscreen": FULLSCREEN,
                  "rgba": rgba is not None}), flush=True)
Gtk.main()
