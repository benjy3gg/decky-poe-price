#!/usr/bin/env python3
"""Watch the X CLIPBOARD of one display and print each new text as a JSON line on stdout.

Runs as its own process (started by main.py) because Xlib calls exit() when the display goes away
(the game closes); the parent just restarts it.

Usage: clipwatch.py DISPLAY        e.g. clipwatch.py :1

Read-only: it never sends input to the game.
"""
import ctypes
import ctypes.util
import json
import select
import sys
import time

args = [a for a in sys.argv[1:] if not a.startswith("--")]
display_name = args[0].encode() if args else None

x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
try:
    xfixes = ctypes.CDLL(ctypes.util.find_library("Xfixes") or "libXfixes.so.3")
except OSError:
    xfixes = None

c_ulong, c_int, c_long, c_void_p = ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.c_void_p
Atom = Window = c_ulong


class XEvent(ctypes.Union):
    _fields_ = [("type", c_int), ("pad", c_long * 24)]


class XSelectionEvent(ctypes.Structure):
    _fields_ = [
        ("type", c_int), ("serial", c_ulong), ("send_event", c_int), ("display", c_void_p),
        ("requestor", Window), ("selection", Atom), ("target", Atom), ("property", Atom), ("time", c_ulong),
    ]


x11.XOpenDisplay.restype = c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XDefaultRootWindow.restype = Window
x11.XDefaultRootWindow.argtypes = [c_void_p]
x11.XInternAtom.restype = Atom
x11.XInternAtom.argtypes = [c_void_p, ctypes.c_char_p, c_int]
x11.XCreateSimpleWindow.restype = Window
x11.XCreateSimpleWindow.argtypes = [c_void_p, Window, c_int, c_int, ctypes.c_uint, ctypes.c_uint,
                                    ctypes.c_uint, c_ulong, c_ulong]
x11.XConvertSelection.argtypes = [c_void_p, Atom, Atom, Atom, Window, c_ulong]
x11.XGetSelectionOwner.restype = Window
x11.XGetSelectionOwner.argtypes = [c_void_p, Atom]
x11.XPending.argtypes = [c_void_p]
x11.XNextEvent.argtypes = [c_void_p, ctypes.POINTER(XEvent)]
x11.XFlush.argtypes = [c_void_p]
x11.XConnectionNumber.argtypes = [c_void_p]
x11.XFree.argtypes = [c_void_p]
x11.XDeleteProperty.argtypes = [c_void_p, Window, Atom]
x11.XGetWindowProperty.argtypes = [
    c_void_p, Window, Atom, c_long, c_long, c_int, Atom,
    ctypes.POINTER(Atom), ctypes.POINTER(c_int), ctypes.POINTER(c_ulong), ctypes.POINTER(c_ulong),
    ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
]

dpy = x11.XOpenDisplay(display_name)
if not dpy:
    print(json.dumps({"error": f"cannot open display {sys.argv[1:]}"}), flush=True)
    sys.exit(2)

root = x11.XDefaultRootWindow(dpy)
win = x11.XCreateSimpleWindow(dpy, root, 0, 0, 1, 1, 0, 0, 0)
CLIPBOARD = x11.XInternAtom(dpy, b"CLIPBOARD", 0)
UTF8 = x11.XInternAtom(dpy, b"UTF8_STRING", 0)
STRING = 31  # XA_STRING
PROP = x11.XInternAtom(dpy, b"POE_PRICE_CLIP", 0)
SELECTION_NOTIFY = 31

xfixes_base = None
if xfixes is not None:
    ev_base, err_base = c_int(), c_int()
    xfixes.XFixesQueryExtension.argtypes = [c_void_p, ctypes.POINTER(c_int), ctypes.POINTER(c_int)]
    xfixes.XFixesSelectSelectionInput.argtypes = [c_void_p, Window, Atom, c_ulong]
    if xfixes.XFixesQueryExtension(dpy, ctypes.byref(ev_base), ctypes.byref(err_base)):
        xfixes_base = ev_base.value
        xfixes.XFixesSelectSelectionInput(dpy, root, CLIPBOARD, 1)  # SetSelectionOwnerNotifyMask

x11.XFlush(dpy)
fd = x11.XConnectionNumber(dpy)
print(json.dumps({"ready": True, "xfixes": xfixes_base is not None}), flush=True)


def read_property():
    actual_type, fmt = Atom(), c_int()
    nitems, after = c_ulong(), c_ulong()
    data = ctypes.POINTER(ctypes.c_ubyte)()
    x11.XGetWindowProperty(dpy, win, PROP, 0, 1 << 20, 1, 0, ctypes.byref(actual_type), ctypes.byref(fmt),
                           ctypes.byref(nitems), ctypes.byref(after), ctypes.byref(data))
    if not data:
        return None
    raw = ctypes.string_at(data, nitems.value * (fmt.value // 8 if fmt.value else 1))
    x11.XFree(data)
    return raw.decode("utf-8", "replace")


def request(target):
    x11.XConvertSelection(dpy, CLIPBOARD, target, PROP, win, 0)  # CurrentTime
    x11.XFlush(dpy)


last_text = None
last_time = 0.0
first = True
pending = False
copied = False  # the pending read follows a selection-owner change: a real copy, even of the same text
next_poll = 0.0
ev = XEvent()
while True:
    now = time.monotonic()
    # Without XFixes, poll every 0.7 s; with it, a slow safety poll in case an event was missed.
    if not pending and now >= next_poll:
        if x11.XGetSelectionOwner(dpy, CLIPBOARD):
            request(UTF8)
            pending = True
        else:
            first = False  # empty clipboard at start: the next text is a real copy
        next_poll = now + (0.7 if xfixes_base is None else 5.0)
    if not x11.XPending(dpy):
        select.select([fd], [], [], 0.25)
        continue
    x11.XNextEvent(dpy, ctypes.byref(ev))
    if xfixes_base is not None and ev.type == xfixes_base:
        request(UTF8)
        pending = True
        copied = True
    elif ev.type == SELECTION_NOTIFY:
        sel = ctypes.cast(ctypes.byref(ev), ctypes.POINTER(XSelectionEvent)).contents
        pending = False
        if sel.property == 0:
            if sel.target == UTF8:  # owner refused UTF8_STRING; try plain STRING
                request(STRING)
                pending = True
            continue
        text = read_property()
        # Report new text, and the same text again when PoE copied it again (owner change), so a second
        # Ctrl+Alt+C on the same item reopens the menu. Safety polls never repeat a text; a repeat within
        # 0.5 s is one copy seen twice.
        now = time.monotonic()
        repeat = text == last_text
        if text and (not repeat or (copied and now - last_time > 0.5)):
            last_text, last_time = text, now
            # The first read is whatever was on the clipboard before we started: report it as "initial".
            print(json.dumps({"text": text, "initial": first, "repeat": repeat}), flush=True)
        copied = False
        first = False
