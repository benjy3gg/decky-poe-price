#!/usr/bin/env python3
"""Interactive price-check menu over the game, in the spirit of Awakened PoE Trade's price-check window.
Started by main.py for rare, magic and unique items.

Two full-screen transparent windows on Steam's display (see docs/OVERLAY.md, "Buttons in the overlay"):
  view   GAMESCOPE_EXTERNAL_OVERLAY=1, GAMESCOPE_NO_FOCUS=1   drawn over the game, never gets input
  input  STEAM_OVERLAY=1, STEAM_INPUT_FOCUS=1                gets taps/clicks, never drawn
Clicks on the input window are hit-tested against the same layout the view window draws.

stdin (JSON lines from main.py):
  {"type": "mods", "title", "subtitle", "color", "ninja": str|null,
   "mods": [{"text", "kind", "enabled", "value": n|null, "min": n|null, "step": n}],
   "filters": [{"id", "label", "options": [...], "value"}],   options [false, true] = a toggle
   "required": n|null, "required_default": "all"|null}   "all": match n/n unless changed
  chips with "emit": true report each change: {"action": "chip", "id", "value"} (main.py resends "mods")
  {"type": "busy", "text": "Searching…"}
  {"type": "results", "summary", "info": [str], "rows": [[price, chaos, ilvl, age, seller]], "error": str|null,
   "items": [card]}   one poeprice.item_card() per row: tapping a row opens the item view
stdout (JSON lines to main.py):
  {"ready": ...}
  {"action": "search", "enabled": [bool], "mins": [n|null], "required": n, "filters": {id: value}}
  {"action": "close", "reason": "button" | "timeout" | "steam_overlay" | "stdin", + the choices as in search}

Safety: closes itself after IDLE_TIMEOUT s without input, when stdin closes, and as soon as Steam's own
overlay opens (Steam's Big Picture window sets STEAM_OVERLAY=1); gamescope then returns input to the game.
The menu never sends input anywhere.

Usage: menu.py DISPLAY [--idle-timeout N] [--preview OUT.png [--crop] < messages]   (preview: render to a PNG, no X)
"""
import ctypes
import json
import sys
import threading
import time

import cairo
import io
import ssl
import urllib.request

args = sys.argv[1:]
DISPLAY = next((a for a in args if a.startswith(":")), ":0")
IDLE_TIMEOUT = int(args[args.index("--idle-timeout") + 1]) if "--idle-timeout" in args else 45
PREVIEW = args[args.index("--preview") + 1] if "--preview" in args else None

# ---------------------------------------------------------------------------------------- layout
PX, PY, PW = 24, 24, 780          # panel position / width
PAD, ROW_H, FOOT_H, BTN_H = 16, 46, 72, 50
CHIP_H, CHIP_GAP = 40, 8
STEP_W, MINV_W = 44, 74           # per-mod  [−] [min] [+]
TABLE_ROW, TABLE_ROWS = 30, 10
COLS = [("Price", 0), ("In chaos", 170), ("ilvl", 290), ("Listed", 360), ("Seller", 460)]
THUMB = 34                        # results: item icon column (thumbnail) left of the price
ICON_COL = 130                    # item view: icon column left of the item text
CELL = 47                         # PoE icons are 47 px per inventory cell
SCREEN_W, SCREEN_H = 1280, 800

GREY, DIM, WHITE = (0.75, 0.75, 0.78), (0.55, 0.55, 0.58), (1, 1, 1)

state = {
    "view": "mods",               # "mods" | "busy" | "results"
    "title": "", "subtitle": "", "color": (1.0, 1.0, 0.47), "ninja": None,
    "mods": [], "filters": [], "required": None, "scroll": 0,
    "busy_text": "",
    "summary": "", "info": [], "rows": [], "page": 0, "error": None,
    "items": [], "item_idx": 0,   # item view: the listed item behind each results row
    "last_input": time.monotonic(),
    "closing": False,
}

_measure = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))


def text_w(s, size, bold=False):
    _measure.select_font_face("Sans", 0, 1 if bold else 0)
    _measure.set_font_size(size)
    return _measure.text_extents(s).x_advance


# ---------------------------------------------------------------------------------------- item icons
# Listed items' icons from PoE's CDN, loaded in the background so the table shows at once.
ICONS = {}                        # url -> cairo.ImageSurface | None (failed)
_icon_lock = threading.Lock()


def _ssl_ctx():
    for path in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ca-certificates/extracted/tls-ca-bundle.pem"):
        try:
            return ssl.create_default_context(cafile=path)
        except OSError:
            pass
    return ssl.create_default_context()


def _fetch_icon(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "decky-poe-price (Steam Deck Decky Loader plugin)"})
        with urllib.request.urlopen(req, timeout=10, context=_ssl_ctx()) as r:
            data = r.read(2_000_000)
        return cairo.ImageSurface.create_from_png(io.BytesIO(data))
    except Exception:
        return None  # not a PNG, offline, ...: no icon


def load_icons(cards, done=None):
    """Fetch the icons of `cards` that aren't loaded yet; calls done() after each one."""
    urls = []
    with _icon_lock:
        for c in cards:
            u = (c or {}).get("icon")
            if u and u.startswith("https://web.poecdn.com/") and u not in ICONS and u not in urls:
                urls.append(u)
                ICONS[u] = None
    def work():
        for u in urls:
            surf = _fetch_icon(u)
            with _icon_lock:
                ICONS[u] = surf
            if done:
                done()
    if urls:
        if done is None:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()


def paint_icon(cr, url, x, y, max_w, max_h, center=True):
    surf = ICONS.get(url) if url else None
    if surf is None:
        return False
    w, h = surf.get_width(), surf.get_height()
    k = min(max_w / w, max_h / h, 1.0 if max_w >= CELL else 10)
    cr.save()
    cr.translate(x + ((max_w - w * k) / 2 if center else 0), y + ((max_h - h * k) / 2 if center else 0))
    cr.scale(k, k)
    cr.set_source_surface(surf, 0, 0)
    cr.paint()
    cr.restore()
    return True


def icon_box(card):
    """Size the item view reserves for the icon: (w, h) scaled into the icon column."""
    if not (card or {}).get("icon"):
        return 0, 0
    w, h = CELL * card.get("w", 1), CELL * card.get("h", 1)
    k = min((ICON_COL - 16) / w, 190 / h, 1.0)
    return w * k, h * k


def emit(**kw):
    print(json.dumps(kw), flush=True)


def n_enabled():
    return sum(1 for m in state["mods"] if m["enabled"])


def default_required():
    n = n_enabled()
    if state.get("required_default") == "all":   # APT's searches need every selected affix
        return n
    return n if n <= 2 else n - 1 if n <= 4 else n - 2


def required():
    n = n_enabled()
    r = state["required"] if state["required"] is not None else default_required()
    return max(1 if n else 0, min(r, n))


def fmt_num(v):
    if v is None:
        return "any"
    return f"{v:.1f}".rstrip("0").rstrip(".") if abs(v - round(v)) > 1e-6 else f"{int(round(v))}"


def chip_label(f):
    if f["options"] == [False, True]:
        return f["label"]
    return f"{f['label']}: {f['value']}"


def chip_on(f):
    return f["value"] not in (False, "any", None)


def header_h():
    return 74 + (30 if state["ninja"] else 0)


def chip_rects():
    """Filter chips, wrapped onto as many rows as needed."""
    out, x, y = [], PX + PAD, PY + header_h()
    for i, f in enumerate(state["filters"]):
        w = text_w(chip_label(f), 16, True) + 28
        if x + w > PX + PW - PAD and x > PX + PAD:
            x, y = PX + PAD, y + CHIP_H + CHIP_GAP
        out.append((f"chip:{i}", x, y, w, CHIP_H))
        x += w + CHIP_GAP
    bottom = (y + CHIP_H + 12) if out else PY + header_h()
    return out, bottom


def visible_rows(top):
    return max(1, int((SCREEN_H - PY - FOOT_H - 12 - top) // ROW_H))


def layout():
    """((panel x, y, w, h), [(id, x, y, w, h)]): everything clickable; shared by drawing and hit-testing."""
    items = []
    if state["view"] == "mods":
        chips, top = chip_rects()
        items += chips
        rows = visible_rows(top)
        mods = state["mods"]
        state["scroll"] = max(0, min(state["scroll"], max(0, len(mods) - rows)))
        shown = mods[state["scroll"]:state["scroll"] + rows]
        for k, m in enumerate(shown):
            i = state["scroll"] + k
            y = top + k * ROW_H
            right = PX + PW - PAD
            if m.get("value") is not None:
                items += [(f"min+:{i}", right - STEP_W, y, STEP_W, ROW_H - 6),
                          (f"minv:{i}", right - STEP_W - MINV_W, y, MINV_W, ROW_H - 6),
                          (f"min-:{i}", right - 2 * STEP_W - MINV_W, y, STEP_W, ROW_H - 6)]
                right -= 2 * STEP_W + MINV_W + 6
            items.append((f"mod:{i}", PX + PAD, y, right - PX - PAD, ROW_H - 6))
        ph = (top - PY) + max(1, len(shown)) * ROW_H + FOOT_H
        fy = PY + ph - FOOT_H + 10
        items += [("req-", PX + PAD, fy, 50, BTN_H), ("req+", PX + PAD + 50 + 90, fy, 50, BTN_H)]
        if len(mods) > rows:
            items += [("up", PX + PAD + 204, fy, 64, BTN_H), ("down", PX + PAD + 274, fy, 76, BTN_H)]
        items += [("search", PX + PW - PAD - 130 - 12 - 170, fy, 170, BTN_H),
                  ("close", PX + PW - PAD - 130, fy, 130, BTN_H)]
    elif state["view"] == "results":
        n = len(state["rows"][state["page"] * TABLE_ROWS:(state["page"] + 1) * TABLE_ROWS])
        body = 56 + len(state["info"]) * 24 + (34 + n * TABLE_ROW if n else 0)
        ph = header_h() + body + FOOT_H
        fy = PY + ph - FOOT_H + 10
        ry = PY + header_h() + 56 + len(state["info"]) * 24 + 34
        for k in range(n):
            i = state["page"] * TABLE_ROWS + k
            if i < len(state["items"]) and state["items"][i]:
                items.append((f"row:{i}", PX + PAD, ry + k * TABLE_ROW, PW - 2 * PAD, TABLE_ROW))
        items.append(("back", PX + PAD, fy, 200, BTN_H))
        if len(state["rows"]) > TABLE_ROWS:
            items += [("prev", PX + PAD + 212, fy, 110, BTN_H), ("next", PX + PAD + 334, fy, 110, BTN_H)]
        items.append(("close", PX + PW - PAD - 130, fy, 130, BTN_H))
    elif state["view"] == "item":
        lines = item_lines()
        idx = state["item_idx"]
        card = state["items"][idx] if 0 <= idx < len(state["items"]) else None
        ph = ITEM_TOP + max(sum(l[0] for l in lines), icon_box(card)[1] + 12) + 12 + FOOT_H
        fy = PY + ph - FOOT_H + 10
        items += [("back", PX + PAD, fy, 220, BTN_H),
                  ("iprev", PX + PAD + 232, fy, 110, BTN_H), ("inext", PX + PAD + 354, fy, 110, BTN_H),
                  ("close", PX + PW - PAD - 130, fy, 130, BTN_H)]
    else:  # busy
        ph = header_h() + 70
    return (PX, PY, PW, ph), items


# ---------------------------------------------------------------------------------------- item view
ITEM_TOP = 112                    # title, base line, listing line
RARITY_RGB = {"Unique": (0.88, 0.54, 0.24), "Rare": (1, 1, 0.47), "Magic": (0.53, 0.53, 1),
              "Gem": (0.11, 0.64, 0.61), "Currency": (0.85, 0.81, 0.71)}
MOD_RGB = {"enchant": (0.72, 0.84, 1), "implicit": (0.53, 0.53, 1), "fractured": (0.64, 0.57, 0.38),
           "explicit": (0.53, 0.53, 1), "crafted": (0.71, 0.71, 1)}
PROP_RGB, CORRUPT_RGB = (0.85, 0.85, 0.87), (0.82, 0.1, 0.1)


def wrap(s, size, max_w, bold=False):
    out = []
    for para in str(s).split("\n"):
        words, line = para.split(" "), ""
        for w in words:
            t = f"{line} {w}" if line else w
            if line and text_w(t, size, bold) > max_w:
                out.append(line)
                line = w
            else:
                line = t
        out.append(line)
    return out


def item_lines():
    """[(height, kind, text, rgb, right)] for the item view; kind "sep" draws a separator."""
    idx = state["item_idx"]
    card = state["items"][idx] if 0 <= idx < len(state["items"]) else None
    if not card:
        return [(26, "text", "No details for this listing", GREY, "")]
    out, size = [], 16
    full = PW - 2 * PAD - (ICON_COL if card.get("icon") else 0)

    def add(txt, rgb, right=""):
        rw = text_w(right, 13) + 14 if right else 0
        for k, line in enumerate(wrap(txt, size, full - rw)):
            out.append((24, "text", line, rgb, right if k == 0 else ""))

    def sep():
        if out and out[-1][1] != "sep":
            out.append((20, "sep", "", None, ""))

    for p in card.get("properties", []):
        add(p, PROP_RGB)
    sep()
    if card.get("requirements"):
        add("Requires " + ", ".join(card["requirements"]), GREY)
    if card.get("sockets"):
        add("Sockets: " + card["sockets"], PROP_RGB)
    if card.get("ilvl"):
        add(f"Item Level: {card['ilvl']}", PROP_RGB)
    if card.get("influences"):
        add(" / ".join(card["influences"]) + " item", (0.75, 0.75, 0.6))
    for group in ("enchant", "implicit"):
        if card.get(group):
            sep()
            for m in card[group]:
                add(m["text"], MOD_RGB[group], tier_tag(m))
    if any(card.get(g) for g in ("fractured", "explicit", "crafted")):
        sep()
        for group in ("fractured", "explicit", "crafted"):
            for m in card.get(group) or []:
                add(m["text"], MOD_RGB[group], tier_tag(m) or ("crafted" if group == "crafted" else ""))
    if not card.get("identified", True):
        sep()
        add("Unidentified", CORRUPT_RGB)
    if card.get("corrupted"):
        sep()
        add("Corrupted", CORRUPT_RGB)
    if card.get("note"):
        sep()
        add("Note: " + card["note"], (0.9, 0.75, 0.45))
    room = SCREEN_H - PY - ITEM_TOP - 12 - FOOT_H - 8
    if sum(l[0] for l in out) > room:
        while out and sum(l[0] for l in out) > room - 24:
            out.pop()
        out.append((24, "text", "…", GREY, ""))
    return out


def tier_tag(m):
    return " ".join(x for x in (m.get("tier", ""), f"({m['range']})" if m.get("range") else "") if x)


# ---------------------------------------------------------------------------------------- drawing
# Colours after Path of Exile's item tooltips: near-black box, rarity-tinted header with a serif title,
# grey labels, blue affixes, gold-trimmed buttons.
TITLE_FONT, BODY_FONT = "Noto Serif", "Sans"
BG = (0.025, 0.022, 0.018, 0.95)
FRAME = (0.47, 0.37, 0.22)
TEXT, LABEL = (0.80, 0.80, 0.80), (0.50, 0.50, 0.50)
OFF_TEXT = (0.40, 0.40, 0.43)
GOLD, PRICE, CURRENCY = (0.85, 0.74, 0.50), (0.95, 0.84, 0.55), (0.67, 0.62, 0.51)
ERROR = (0.90, 0.35, 0.30)
KIND_RGB = {"explicit": (0.53, 0.53, 1), "implicit": (0.53, 0.53, 1), "crafted": (0.71, 0.71, 1),
            "enchant": (0.72, 0.84, 1), "fractured": (0.64, 0.57, 0.38), "pseudo": (0.80, 0.80, 0.80),
            "property": (0.80, 0.80, 0.80)}


def shade(rgb, k):
    return tuple(c * k for c in rgb[:3])


def text(cr, s, x, y, size, rgb=TEXT, bold=False, max_w=None, font=BODY_FONT):
    cr.select_font_face(font, 0, 1 if bold else 0)
    cr.set_font_size(size)
    if max_w is not None:
        while s and cr.text_extents(s).x_advance > max_w:
            s = s[:-2] + "…" if len(s) > 2 else ""
    cr.set_source_rgb(*rgb)
    cr.move_to(x, y)
    cr.show_text(s)


def centered(cr, s, cx, y, size, rgb, bold=False, max_w=None, font=BODY_FONT):
    cr.select_font_face(font, 0, 1 if bold else 0)
    cr.set_font_size(size)
    w = min(cr.text_extents(s).x_advance, max_w or 1e9)
    text(cr, s, cx - w / 2, y, size, rgb, bold, max_w, font)


def box(cr, rect, rgb, border=None, width=1.5):
    _, x, y, w, h = rect
    cr.set_source_rgb(*rgb)
    cr.rectangle(x, y, w, h)
    cr.fill()
    if border:
        cr.set_source_rgb(*border)
        cr.set_line_width(width)
        cr.rectangle(x + width / 2, y + width / 2, w - width, h - width)
        cr.stroke()


def separator(cr, cx, y, half_w, rgb):
    """PoE-style divider: a thin line fading out to both sides, with a small diamond in the middle."""
    import cairo as _c
    g = _c.LinearGradient(cx - half_w, 0, cx + half_w, 0)
    g.add_color_stop_rgba(0, *rgb, 0)
    g.add_color_stop_rgba(0.5, *rgb, 0.9)
    g.add_color_stop_rgba(1, *rgb, 0)
    cr.set_source(g)
    cr.rectangle(cx - half_w, y, 2 * half_w, 1)
    cr.fill()
    cr.set_source_rgb(*rgb)
    cr.move_to(cx, y - 3)
    cr.line_to(cx + 4, y + 0.5)
    cr.line_to(cx, y + 4)
    cr.line_to(cx - 4, y + 0.5)
    cr.close_path()
    cr.fill()


def button(cr, rect, label, primary=False, disabled=False, size=19):
    _, x, y, w, h = rect
    if disabled:
        bg, border, fg = (0.06, 0.055, 0.05), (0.20, 0.17, 0.13), (0.35, 0.33, 0.30)
    elif primary:
        bg, border, fg = (0.30, 0.19, 0.07), (0.85, 0.66, 0.33), (1, 0.92, 0.70)
    else:
        bg, border, fg = (0.10, 0.08, 0.05), (0.48, 0.38, 0.24), (0.86, 0.77, 0.57)
    box(cr, rect, bg, border)
    centered(cr, label, x + w / 2, y + h / 2 + size * 0.36, size, fg, bold=True)


def header(cr, px, pw, title, subtitle, rgb, extra=None):
    """Tooltip header: rarity-tinted band, serif title and subtitle centred, divider below."""
    band = tuple(0.05 + c * 0.16 for c in rgb[:3])
    box(cr, (None, px + 2, PY + 2, pw - 4, 64), band)
    centered(cr, title, px + pw / 2, PY + 32, 23, rgb, bold=True, max_w=pw - 2 * PAD - 80, font=TITLE_FONT)
    centered(cr, subtitle, px + pw / 2, PY + 56, 14, LABEL, max_w=pw - 2 * PAD)
    separator(cr, px + pw / 2, PY + 66, pw / 2 - 2, shade(rgb, 0.75))
    if extra:
        centered(cr, extra, px + pw / 2, PY + 90, 17, CURRENCY, bold=True, max_w=pw - 2 * PAD)


def draw_mods(cr, rects):
    for cid, f in enumerate(state["filters"]):
        r = rects[f"chip:{cid}"]
        if chip_on(f):
            box(cr, r, (0.24, 0.16, 0.07), (0.78, 0.60, 0.30))
            text(cr, chip_label(f), r[1] + 14, r[2] + 26, 16, (1, 0.92, 0.70), bold=True)
        else:
            box(cr, r, (0.06, 0.05, 0.04), (0.27, 0.22, 0.15))
            text(cr, chip_label(f), r[1] + 14, r[2] + 26, 16, (0.55, 0.52, 0.47), bold=True)
    for i, m in enumerate(state["mods"]):
        r = rects.get(f"mod:{i}")
        if not r:
            continue
        _, x, y, w, h = r
        on = m["enabled"]
        kind = m.get("kind", "") or "explicit"
        if on:
            box(cr, (None, x, y, PW - 2 * PAD, h), (0.085, 0.075, 0.055), (0.40, 0.31, 0.19), 1)
        else:
            box(cr, (None, x, y, PW - 2 * PAD, h), (0.045, 0.042, 0.038))
        # checkbox
        cr.set_source_rgb(*(GOLD if on else (0.30, 0.28, 0.25)))
        cr.set_line_width(2)
        cr.rectangle(x + 12, y + h / 2 - 11, 22, 22)
        cr.stroke()
        if on:
            cr.move_to(x + 16, y + h / 2)
            cr.line_to(x + 22, y + h / 2 + 7)
            cr.line_to(x + 31, y + h / 2 - 8)
            cr.stroke()
        kind_w = 0
        if kind != "explicit":
            kind_w = text_w(kind, 13) + 12
            text(cr, kind, x + w - kind_w, y + h / 2 + 5, 13, LABEL)
        text(cr, m["text"].replace("\n", " / "), x + 46, y + h / 2 + 6, 16,
             KIND_RGB.get(kind, KIND_RGB["explicit"]) if on else OFF_TEXT, max_w=w - 54 - kind_w)
        if f"minv:{i}" in rects:
            dis = not on
            button(cr, rects[f"min-:{i}"], "−", disabled=dis, size=20)
            button(cr, rects[f"min+:{i}"], "+", disabled=dis, size=20)
            _, vx, vy, vw, vh = rects[f"minv:{i}"]
            label = (fmt_num(m["min"]) + "+") if m["min"] is not None else "any"
            centered(cr, label, vx + vw / 2, vy + vh / 2 + 6, 16, OFF_TEXT if dis else PRICE, bold=True)
    if not state["mods"]:
        _, top = chip_rects()
        text(cr, "No affixes to filter: Search uses the filters above", PX + PAD, top + 28, 16, LABEL)
    n_on = n_enabled()
    button(cr, rects["req-"], "−", disabled=required() <= 1)
    _, x, y, w, h = rects["req-"]
    text(cr, "match", x + 60, y + 19, 13, LABEL)
    text(cr, f"{required()}/{n_on}" if n_on else "0", x + 64, y + 43, 20, PRICE, bold=True)
    button(cr, rects["req+"], "+", disabled=required() >= n_on)
    if "up" in rects:
        button(cr, rects["up"], "Up", disabled=state["scroll"] == 0, size=17)
        button(cr, rects["down"], "Down", disabled=f"mod:{len(state['mods']) - 1}" in rects, size=17)
    button(cr, rects["search"], "Search", primary=True)
    button(cr, rects["close"], "Close")


def draw_results(cr, rects, px, pw):
    y = PY + header_h()
    if state["error"]:
        text(cr, state["error"], px + PAD, y + 34, 18, ERROR, bold=True, max_w=pw - 2 * PAD)
    else:
        text(cr, state["summary"], px + PAD, y + 36, 26, PRICE, bold=True, max_w=pw - 2 * PAD, font=TITLE_FONT)
    y += 56
    for line in state["info"]:
        text(cr, line, px + PAD, y + 16, 15, LABEL, max_w=pw - 2 * PAD)
        y += 24
    rows = state["rows"][state["page"] * TABLE_ROWS:(state["page"] + 1) * TABLE_ROWS]
    if rows:
        for name, cx in COLS:
            text(cr, name, px + PAD + THUMB + cx, y + 22, 14, LABEL, bold=True)
        separator(cr, px + pw / 2, y + 30, pw / 2 - PAD, shade(GOLD, 0.6))
        y += 34
        for k, row in enumerate(rows):
            if k % 2:
                box(cr, (None, px + PAD, y, pw - 2 * PAD, TABLE_ROW), (0.065, 0.055, 0.042))
            gi_ = state["page"] * TABLE_ROWS + k
            card = state["items"][gi_] if gi_ < len(state["items"]) else None
            if card:
                text(cr, ">", px + pw - PAD - 14, y + 21, 16, shade(GOLD, 0.8), bold=True)
                paint_icon(cr, card.get("icon"), px + PAD + 2, y + 2, THUMB - 6, TABLE_ROW - 4)
            for c, (name, cx) in enumerate(COLS):
                nxt = COLS[c + 1][1] if c + 1 < len(COLS) else pw - 2 * PAD - 18 - THUMB
                val = str(row[c]) if c < len(row) and row[c] is not None else ""
                text(cr, val, px + PAD + THUMB + cx + 2, y + 21, 16, PRICE if c == 0 else LABEL if c == 1 else TEXT,
                     bold=c == 0, max_w=nxt - cx - 10)
            y += TABLE_ROW
    button(cr, rects["back"], "Back to filters")
    if "prev" in rects:
        pages = (len(state["rows"]) + TABLE_ROWS - 1) // TABLE_ROWS
        button(cr, rects["prev"], "< Prev", disabled=state["page"] == 0, size=17)
        button(cr, rects["next"], "Next >", disabled=state["page"] >= pages - 1, size=17)
    button(cr, rects["close"], "Close")


def draw_item(cr, rects, px, pw):
    idx = state["item_idx"]
    card = state["items"][idx] if 0 <= idx < len(state["items"]) else {}
    row = state["rows"][idx] if idx < len(state["rows"]) else []
    rgb = RARITY_RGB.get(card.get("rarity"), TEXT)
    name = card.get("name") or card.get("base") or "Item"
    header(cr, px, pw, name, card.get("base", "") if card.get("name") else (card.get("rarity") or ""), rgb)
    pos = f"{idx + 1}/{len(state['rows'])}"
    text(cr, pos, px + pw - PAD - text_w(pos, 14), PY + 24, 14, LABEL)
    if row:
        price = f"{row[0]}" + (f"  ({row[1]})" if len(row) > 1 and row[1] else "")
        who = "  ·  ".join(str(x) for x in (row[4] if len(row) > 4 else "", (row[3] + " ago") if len(row) > 3 and row[3] else "") if x)
        text(cr, price, px + PAD, PY + 92, 20, PRICE, bold=True)
        text(cr, who, px + PAD + text_w(price, 20, True) + 18, PY + 92, 15, LABEL,
             max_w=pw - 3 * PAD - text_w(price, 20, True))
    y = PY + ITEM_TOP
    separator(cr, px + pw / 2, y - 6, pw / 2 - PAD, shade(rgb, 0.6))
    left = px + PAD
    if card.get("icon"):
        bw, bh = icon_box(card)
        box(cr, (None, left, y + 4, ICON_COL - 12, bh + 8), (0.05, 0.045, 0.04), (0.27, 0.22, 0.15), 1)
        if not paint_icon(cr, card["icon"], left + 4, y + 8, ICON_COL - 20, bh):
            centered(cr, "…", left + (ICON_COL - 12) / 2, y + 8 + bh / 2, 16, OFF_TEXT)
        left += ICON_COL
    cx = (left + px + pw - PAD) / 2
    for h, kind, line, color, right in item_lines():
        if kind == "sep":
            separator(cr, cx, y + h / 2 - 2, 150, shade(rgb, 0.6))
        else:
            centered(cr, line, cx, y + 18, 16, color,
                     max_w=(px + pw - PAD - left) - 2 * (text_w(right, 13) + 14 if right else 0))
            if right:
                text(cr, right, px + pw - PAD - text_w(right, 13), y + 17, 13, OFF_TEXT)
        y += h
    button(cr, rects["back"], "Back to results")
    button(cr, rects["iprev"], "< Prev", disabled=idx <= 0, size=17)
    button(cr, rects["inext"], "Next >", disabled=idx >= len(state["rows"]) - 1, size=17)
    button(cr, rects["close"], "Close")


def draw_view(_w, cr):
    cr.set_operator(0)  # CLEAR
    cr.paint()
    cr.set_operator(2)  # OVER
    if state["closing"]:
        return False
    (px, py, pw, ph), items = layout()
    cr.set_source_rgba(*BG)
    cr.rectangle(px, py, pw, ph)
    cr.fill()
    cr.set_source_rgb(*FRAME)
    cr.set_line_width(2)
    cr.rectangle(px + 1, py + 1, pw - 2, ph - 2)
    cr.stroke()
    rects = {i[0]: i for i in items}
    if state["view"] == "item":
        draw_item(cr, rects, px, pw)
        return False
    header(cr, px, pw, state["title"], state["subtitle"], state["color"], state["ninja"])
    if state["view"] == "mods":
        draw_mods(cr, rects)
    elif state["view"] == "busy":
        text(cr, state["busy_text"] or "Searching…", px + PAD, py + header_h() + 30, 20, GOLD, bold=True)
    else:
        draw_results(cr, rects, px, pw)
    return False


# ---------------------------------------------------------------------------------------- input
def step_min(m, direction):
    step = m.get("step") or 1
    if m["min"] is None:
        m["min"] = m["value"] if direction > 0 else m["value"] - step
    else:
        m["min"] = round(m["min"] + direction * step, 1)


def on_click(xp, yp):
    state["last_input"] = time.monotonic()
    _, items = layout()
    target = next((i[0] for i in items if i[1] <= xp <= i[1] + i[3] and i[2] <= yp <= i[2] + i[4]), None)
    if target is None:
        return
    if target == "close":
        close("button")
        return
    kind, _, idx = target.partition(":")
    if state["view"] == "mods":
        if kind == "mod":
            m = state["mods"][int(idx)]
            m["enabled"] = not m["enabled"]
            if state["required"] is not None:
                state["required"] = max(1, min(state["required"], n_enabled()))
        elif kind in ("min-", "min+", "minv"):
            m = state["mods"][int(idx)]
            if not m["enabled"]:
                m["enabled"] = True
            elif kind == "minv":
                m["min"] = None if m["min"] is not None else m["value"]  # tap the number: any value <-> roll
            else:
                step_min(m, -1 if kind == "min-" else 1)
        elif kind == "chip":
            f = state["filters"][int(idx)]
            opts = f["options"]
            f["value"] = opts[(opts.index(f["value"]) + 1) % len(opts)] if f["value"] in opts else opts[0]
            if f.get("emit"):  # main.py rebuilds the menu for this choice (APT presets)
                emit(action="chip", id=f["id"], value=f["value"])
        elif target == "req-":
            state["required"] = max(1, required() - 1)
        elif target == "req+":
            state["required"] = min(n_enabled(), required() + 1)
        elif target == "up":
            state["scroll"] = max(0, state["scroll"] - 3)
        elif target == "down":
            state["scroll"] += 3
        elif target == "search":
            state["view"] = "busy"
            state["busy_text"] = "Searching…"
            emit(action="search", **choices())
    elif state["view"] == "item":
        if target == "back":
            state["view"] = "results"
            state["page"] = state["item_idx"] // TABLE_ROWS
        elif target == "iprev":
            state["item_idx"] = max(0, state["item_idx"] - 1)
        elif target == "inext":
            state["item_idx"] = min(len(state["rows"]) - 1, state["item_idx"] + 1)
    elif state["view"] == "results":
        if kind == "row":
            state["view"] = "item"
            state["item_idx"] = int(idx)
        elif target == "back":
            state["view"] = "mods"
        elif target == "prev":
            state["page"] = max(0, state["page"] - 1)
        elif target == "next":
            state["page"] = min((len(state["rows"]) - 1) // TABLE_ROWS, state["page"] + 1)
    redraw()


def choices():
    """The current selections, sent with search and close so main.py can restore them on the next copy."""
    if not state.get("mods") and not state.get("filters"):
        return {}
    return {"enabled": [m["enabled"] for m in state["mods"]], "mins": [m["min"] for m in state["mods"]],
            "required": required(), "filters": {f["id"]: f["value"] for f in state["filters"]}}


def apply(msg):
    t = msg.get("type")
    if t == "mods":
        state.update(view="mods", title=msg.get("title", ""), subtitle=msg.get("subtitle", ""),
                     color=hex_color(msg.get("color", "#ffff77")), ninja=msg.get("ninja"), scroll=0,
                     mods=[{"text": m.get("text", ""), "kind": m.get("kind", ""), "enabled": bool(m.get("enabled")),
                            "value": m.get("value"), "min": m.get("min"), "step": m.get("step") or 1}
                           for m in msg.get("mods", [])],
                     filters=[{"id": f["id"], "label": f.get("label", f["id"]),
                               "options": f.get("options") or [False, True], "value": f.get("value"),
                               "emit": bool(f.get("emit"))}
                              for f in msg.get("filters", [])],
                     required=msg.get("required"), required_default=msg.get("required_default"),
                     last_input=time.monotonic())
    elif t == "busy":
        state.update(view="busy", busy_text=msg.get("text", "Searching…"))
    elif t == "results":
        state.update(view="results", ninja=msg.get("ninja", state["ninja"]), summary=msg.get("summary", ""), info=list(msg.get("info", []))[:3],
                     rows=list(msg.get("rows", [])), items=list(msg.get("items", [])), page=0, error=msg.get("error"), last_input=time.monotonic())
        load_icons(state["items"], None if PREVIEW else lambda: GLib.idle_add(redraw_once))


def hex_color(s):
    s = (s or "").lstrip("#")
    return tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4)) if len(s) == 6 else (1.0, 1.0, 0.47)


# ---------------------------------------------------------------------------------------- preview
if PREVIEW:
    # Offline check of the drawing: menu.py --preview out.png < messages.jsonl
    # Lines that are {"click": [x, y]} are taps; each {"shot": "name"} writes out-name.png.
    def close(reason):
        emit(action="close", reason=reason)

    def redraw():
        pass

    def shot(path):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, SCREEN_W, SCREEN_H)
        cr = cairo.Context(surf)
        if "--crop" in args:  # README images: dark backdrop, cropped to the panel
            g = cairo.RadialGradient(PX + PW / 2, 200, 50, PX + PW / 2, 300, 800)
            g.add_color_stop_rgb(0, 0.16, 0.13, 0.10)
            g.add_color_stop_rgb(1, 0.05, 0.045, 0.04)
            cr.set_source(g)
        else:
            cr.set_source_rgb(0.25, 0.3, 0.25)  # stand-in for the game
        cr.paint()
        cr.push_group()
        draw_view(None, cr)
        cr.pop_group_to_source()
        cr.paint()
        if "--crop" in args:
            (px, py, pw, ph), _ = layout()
            m = 24
            out = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(pw + 2 * m), int(ph + 2 * m))
            c2 = cairo.Context(out)
            c2.set_source_surface(surf, -(px - m), -(py - m))
            c2.paint()
            surf = out
        surf.write_to_png(path)

    for line in sys.stdin:
        msg = json.loads(line)
        if "click" in msg:
            on_click(*msg["click"])
        elif "shot" in msg:
            shot(PREVIEW.replace(".png", f"-{msg['shot']}.png"))
        else:
            apply(msg)
    shot(PREVIEW)
    sys.exit(0)

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
from gi.repository import Gdk, GdkX11, GLib, Gtk  # noqa: E402,F401


def redraw():
    view.queue_draw()


def redraw_once():
    view.queue_draw()
    return False


_last_touch = [0.0]


def on_button(_w, ev):
    if ev.button == 1 and time.monotonic() - _last_touch[0] > 0.3:
        on_click(ev.x, ev.y)
    return True


def on_touch(_w, ev):
    if ev.type == Gdk.EventType.TOUCH_BEGIN:
        _last_touch[0] = time.monotonic()
        on_click(ev.x, ev.y)
    return True


def apply_and_show(msg):
    if not state.get("shown") and msg.get("type") in ("mods", "busy", "results"):
        state["shown"] = True
        view.show_all()  # map only once there is something to show; the input window takes input from now on
        inp.show_all()
    apply(msg)
    view.queue_draw()
    return False


def reader():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        GLib.idle_add(apply_and_show, msg)
    GLib.idle_add(close, "stdin")


# ---------------------------------------------------------------------------------------- X helpers
X = ctypes.CDLL("libX11.so.6")
V, UL, I = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int
X.XOpenDisplay.restype = V
X.XOpenDisplay.argtypes = [ctypes.c_char_p]
X.XDefaultRootWindow.restype = UL
X.XDefaultRootWindow.argtypes = [V]
X.XInternAtom.restype = UL
X.XInternAtom.argtypes = [V, ctypes.c_char_p, I]
X.XChangeProperty.argtypes = [V, UL, UL, UL, I, I, V, I]
X.XQueryTree.argtypes = [V, UL, ctypes.POINTER(UL), ctypes.POINTER(UL), ctypes.POINTER(ctypes.POINTER(UL)),
                         ctypes.POINTER(ctypes.c_uint)]
X.XGetWindowProperty.argtypes = [V, UL, UL, ctypes.c_long, ctypes.c_long, I, UL, ctypes.POINTER(UL),
                                 ctypes.POINTER(I), ctypes.POINTER(UL), ctypes.POINTER(UL),
                                 ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]
X.XFree.argtypes = [V]
X.XFlush.argtypes = [V]
XD = X.XOpenDisplay(DISPLAY.encode())  # our own connection for properties + watching Steam's window
if not XD:
    emit(action="close", reason=f"cannot open display {DISPLAY}")
    sys.exit(1)


def set_props(win, props):
    xid = win.get_window().get_xid()
    for name, value in props:
        v = (ctypes.c_long * 1)(value)
        X.XChangeProperty(XD, xid, X.XInternAtom(XD, name, 0), 6, 32, 0, v, 1)  # XA_CARDINAL, Replace
    X.XFlush(XD)
    return hex(xid)


def get_card(w, name):
    t, f, n, a = UL(), I(), UL(), UL()
    data = ctypes.POINTER(ctypes.c_ubyte)()
    X.XGetWindowProperty(XD, w, X.XInternAtom(XD, name, 0), 0, 1, 0, 0, ctypes.byref(t), ctypes.byref(f),
                         ctypes.byref(n), ctypes.byref(a), ctypes.byref(data))
    if not data:
        return None
    value = ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong))[0] if n.value else None
    X.XFree(data)
    return value


def find_steam_window():
    """Steam's Big Picture window: top-level with STEAM_GAME=769 and a STEAM_OVERLAY property."""
    r, p, ch, n = UL(), UL(), ctypes.POINTER(UL)(), ctypes.c_uint()
    X.XQueryTree(XD, X.XDefaultRootWindow(XD), ctypes.byref(r), ctypes.byref(p), ctypes.byref(ch), ctypes.byref(n))
    found = None
    for i in range(n.value):
        if get_card(ch[i], b"STEAM_GAME") == 769 and get_card(ch[i], b"STEAM_OVERLAY") is not None:
            found = ch[i]
            break
    if ch:
        X.XFree(ch)
    return found


STEAM_WIN = find_steam_window()


def watchdog():
    if state["closing"]:
        return False
    if STEAM_WIN and get_card(STEAM_WIN, b"STEAM_OVERLAY") == 1:
        close("steam_overlay")  # give Steam its overlay slot back right away
        return False
    if time.monotonic() - state["last_input"] > IDLE_TIMEOUT and state["view"] != "busy":
        close("timeout")
        return False
    return True


def close(reason):
    if state["closing"]:
        return False
    state["closing"] = True
    try:
        inp.hide()
        view.hide()
    except Exception:
        pass
    emit(action="close", reason=reason, **choices())
    Gtk.main_quit()
    return False


# ---------------------------------------------------------------------------------------- windows
Gdk.set_allowed_backends("x11")
Gtk.init([sys.argv[0], f"--display={DISPLAY}"])


def fullscreen_rgba(win):
    screen = win.get_screen()
    visual = screen.get_rgba_visual()
    if visual is not None:
        win.set_visual(visual)
    win.set_app_paintable(True)
    win.move(0, 0)
    win.resize(screen.get_width(), screen.get_height())


view = Gtk.Window(type=Gtk.WindowType.POPUP)
fullscreen_rgba(view)
SCREEN_H = view.get_screen().get_height() or SCREEN_H
view.connect("draw", draw_view)

inp = Gtk.Window(type=Gtk.WindowType.POPUP)
fullscreen_rgba(inp)
inp.connect("draw", lambda _w, cr: (cr.set_operator(0), cr.paint(), False)[-1])
inp.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.TOUCH_MASK)
inp.connect("button-press-event", on_button)
inp.connect("touch-event", on_touch)

view.realize()
inp.realize()
Gdk.Display.get_default().sync()  # windows must exist on the server before we set properties
vx = set_props(view, [(b"GAMESCOPE_EXTERNAL_OVERLAY", 1), (b"GAMESCOPE_NO_FOCUS", 1)])
ix = set_props(inp, [(b"STEAM_OVERLAY", 1), (b"STEAM_INPUT_FOCUS", 1)])
GLib.timeout_add(200, watchdog)
threading.Thread(target=reader, daemon=True).start()
emit(ready=True, display=DISPLAY, view=vx, input=ix, steam_window=hex(STEAM_WIN) if STEAM_WIN else None)
Gtk.main()
