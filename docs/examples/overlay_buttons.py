#!/usr/bin/env python3
"""Reference example: buttons in a Game Mode overlay = a visible window + an invisible input window.

Verified on a Steam Deck (SteamOS 3.10, gamescope 3.17.0) over Path of Exile: taps/trackpad clicks on the
drawn buttons arrived here (probe "F"). See ../OVERLAY.md for the explanation and the rules.
Run it from a shell inside Game Mode: env DISPLAY=:0 python3 -I overlay_buttons.py :0 --timeout 10

- view: override-redirect window with GAMESCOPE_EXTERNAL_OVERLAY=1 + GAMESCOPE_NO_FOCUS=1, draws the menu
  (gamescope draws external overlays over the game, but never gives them input).
- input: full-screen, fully transparent window with STEAM_OVERLAY=1 + STEAM_INPUT_FOCUS=<mode>
  (+ STEAM_GAME=<appid> so the "focused app" stays the game). gamescope gives it the input but doesn't
  draw it. Clicks/taps on it are hit-tested against the buttons drawn in the view window.

Closes itself after --timeout seconds (default 10). Prints JSON lines.
Usage: hybrid_test.py DISPLAY [--timeout N] [--focus-mode 1|2] [--steam-game APPID]
"""
import ctypes
import json
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk  # noqa: E402,F401

args = sys.argv[1:]
DISPLAY = next((a for a in args if a.startswith(":")), ":0")


def opt(name, default):
    return int(args[args.index(name) + 1]) if name in args and args.index(name) + 1 < len(args) else default


TIMEOUT = max(3, min(opt("--timeout", 10), 30))
FOCUS_MODE = 2 if opt("--focus-mode", 1) == 2 else 1
STEAM_GAME = opt("--steam-game", 0)
LETTER = ""
START = time.monotonic()
X0, Y0, BW, BH, GAP = 24, 24, 200, 64, 14
BUTTONS = ["Mod A", "Mod B", "Close"]

Gdk.set_allowed_backends("x11")
Gtk.init([sys.argv[0], f"--display={DISPLAY}"])
state = {"on": {b: False for b in BUTTONS}, "last": "none yet", "clicks": 0}


def emit(**kw):
    print(json.dumps(kw), flush=True)


def button_rect(i):
    return X0 + 16 + i * (BW + GAP), Y0 + 60, BW, BH


def set_props(win, props):
    xid = win.get_window().get_xid()
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
    for name, value in props:
        v = (ctypes.c_long * 1)(value)
        x.XChangeProperty(d, xid, x.XInternAtom(d, name, 0), 6, 32, 0, v, 1)
    x.XFlush(d)
    x.XCloseDisplay(d)
    return hex(xid)


def fullscreen_rgba(win):
    screen = win.get_screen()
    visual = screen.get_rgba_visual()
    if visual is not None:
        win.set_visual(visual)
    win.set_app_paintable(True)
    win.move(0, 0)
    win.resize(screen.get_width(), screen.get_height())


# ---- view window (visible, external overlay) ----
view = Gtk.Window(type=Gtk.WindowType.POPUP)
fullscreen_rgba(view)


def draw_view(_w, cr):
    cr.set_operator(0)
    cr.paint()
    cr.set_operator(2)
    width = 16 * 2 + len(BUTTONS) * BW + (len(BUTTONS) - 1) * GAP
    cr.set_source_rgba(0.086, 0.086, 0.086, 0.94)
    cr.rectangle(X0, Y0, width, 60 + BH + 50)
    cr.fill()
    cr.set_source_rgb(1, 1, 0.47)
    cr.select_font_face("Sans", 0, 1)
    cr.set_font_size(22)
    cr.move_to(X0 + 16, Y0 + 36)
    left = max(0, TIMEOUT - int(time.monotonic() - START))
    cr.show_text(f"Buttons in the overlay  ({left} s)")
    for i, name in enumerate(BUTTONS):
        x, y, w, h = button_rect(i)
        on = state["on"][name]
        cr.set_source_rgb(*((0.2, 0.55, 0.25) if on else (0.25, 0.25, 0.28)))
        cr.rectangle(x, y, w, h)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.set_font_size(20)
        label = name if name == "Close" else f"{name}: {'ON' if on else 'off'}"
        cr.move_to(x + 16, y + 40)
        cr.show_text(label)
    cr.select_font_face("Sans", 0, 0)
    cr.set_font_size(16)
    cr.move_to(X0 + 16, Y0 + 60 + BH + 32)
    cr.show_text(f"Clicks: {state['clicks']}   last: {state['last']}")


view.connect("draw", draw_view)

# ---- input window (invisible, Steam overlay slot with input focus) ----
inp = Gtk.Window(type=Gtk.WindowType.POPUP)
fullscreen_rgba(inp)
inp.connect("draw", lambda _w, cr: (cr.set_operator(0), cr.paint(), False)[-1])
inp.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.TOUCH_MASK | Gdk.EventMask.KEY_PRESS_MASK)


def hit(xp, yp, source):
    state["clicks"] += 1
    target = None
    for i, name in enumerate(BUTTONS):
        x, y, w, h = button_rect(i)
        if x <= xp <= x + w and y <= yp <= y + h:
            target = name
    state["last"] = f"{target or 'outside'} at {int(xp)},{int(yp)} ({source})"
    emit(click=target, x=int(xp), y=int(yp), source=source, t=round(time.monotonic() - START, 1))
    if target == "Close":
        return quit_("button")
    if target:
        state["on"][target] = not state["on"][target]
    view.queue_draw()


def on_button(_w, ev):
    hit(ev.x, ev.y, f"button{ev.button}")
    return True


def on_touch(_w, ev):
    if ev.type == Gdk.EventType.TOUCH_BEGIN:
        hit(ev.x, ev.y, "touch")
    return True


def on_key(_w, ev):
    emit(key=Gdk.keyval_name(ev.keyval), t=round(time.monotonic() - START, 1))
    state["last"] = f"key {Gdk.keyval_name(ev.keyval)}"
    view.queue_draw()
    return True


inp.connect("button-press-event", on_button)
inp.connect("touch-event", on_touch)
inp.connect("key-press-event", on_key)


def quit_(reason):
    emit(closed=reason)
    Gtk.main_quit()
    return False


def tick():
    view.queue_draw()
    if time.monotonic() - START >= TIMEOUT:
        return quit_("timeout")
    return True


view.realize()
inp.realize()
Gdk.Display.get_default().sync()  # make sure the X server has both windows before we set their properties
vx = set_props(view, [(b"GAMESCOPE_EXTERNAL_OVERLAY", 1), (b"GAMESCOPE_NO_FOCUS", 1)])
iprops = [(b"STEAM_OVERLAY", 1), (b"STEAM_INPUT_FOCUS", FOCUS_MODE)]
if STEAM_GAME:
    iprops.append((b"STEAM_GAME", STEAM_GAME))
ix = set_props(inp, iprops)


def list_props(win):
    x = ctypes.CDLL("libX11.so.6")
    V, UL, I = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int
    x.XOpenDisplay.restype = V
    x.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x.XListProperties.restype = ctypes.POINTER(UL)
    x.XListProperties.argtypes = [V, UL, ctypes.POINTER(I)]
    x.XGetAtomName.restype = ctypes.c_char_p
    x.XGetAtomName.argtypes = [V, UL]
    x.XCloseDisplay.argtypes = [V]
    d = x.XOpenDisplay(DISPLAY.encode())
    n = I()
    p = x.XListProperties(d, win.get_window().get_xid(), ctypes.byref(n))
    out = [x.XGetAtomName(d, p[i]).decode() for i in range(n.value)] if p else []
    x.XCloseDisplay(d)
    return [o for o in out if o.startswith(("STEAM", "GAMESCOPE"))]


if "--debug" in args:
    emit(debug="after set", view=list_props(view), input=list_props(inp), view_xid=vx, input_xid=ix)
view.show_all()
inp.show_all()
if "--debug" in args:
    GLib.timeout_add(800, lambda: (emit(debug="after show", view=list_props(view), input=list_props(inp),
                                        view_xid=hex(view.get_window().get_xid()),
                                        input_xid=hex(inp.get_window().get_xid())), False)[-1])
GLib.timeout_add(500, tick)
emit(ready=True, display=DISPLAY, view=vx, input=ix, timeout=TIMEOUT, focus_mode=FOCUS_MODE, steam_game=STEAM_GAME)
Gtk.main()
