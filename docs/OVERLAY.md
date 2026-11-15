# Showing things over a game in Game Mode

Notes from building PoE Price Check's price box (v1.1.0), tested on a Steam Deck with SteamOS 3.10,
gamescope 3.17.0 and Decky Loader v3.3.1. Each finding is marked **verified** (seen on the Deck) or
**untested**.

## What changed since v1.0.0

| Area | Change |
| --- | --- |
| `overlay.py` (new) | Display-only box drawn top-left over the game. GTK 3 + cairo, run with the system `/usr/bin/python3`. Reads JSON lines on stdin: `{"title", "lines", "color"}` shows or updates, `{"hide": true}` hides, closing stdin exits. |
| `main.py` | Price overlay: "Checking price…" on copy, then the result, then auto-hide (`overlay`, `overlay_seconds` settings). It closes when PoE isn't running. The Debug section's overlay test runs four overlay variants, numbered, 6 s each. No toast is sent when the box showed the price. |
| `src/index.tsx` | Settings: "Show price over the game" and "Price box stays for". Debug section: "Run overlay test" button with live status. |
| `py_modules/poeprice.py` | Quest items return "Quest item, can't be traded" without a lookup. Network failures say "No internet connection" / "Can't reach the price sites". |

## How the price box works (verified)

```
PoE copies item ──> clipwatch.py (game display :1) ──> main.py prices it
                                                        │
                                                        └─> overlay.py on Steam's display :0
                                                            window flags:
                                                              GAMESCOPE_EXTERNAL_OVERLAY = 1
                                                              GAMESCOPE_NO_FOCUS         = 1
```

- The window must be on **Steam's X display (`:0`)**, not the game's (`:1`). Gamescope picks the
  external overlay from the root Xwayland (`root_ctx`) and draws it for every app in front.
- The flags go on the **top-level window**, the child of the root window. Toolkits often wrap the
  window they report. With Tk, `winfo_id()` returned an inner window. With the flag on the wrong window,
  gamescope treated the box as an ordinary override-redirect pop-up of whatever app was in front. It then
  showed over the nested desktop but never over PoE, and it took keyboard focus.
- Set the flags **before the first map**: `realize()` the window, set the properties, then show it.
  Gamescope reads them when the window is mapped.
- `GAMESCOPE_NO_FOCUS=1` (as mangoapp uses) keeps gamescope from ever giving the window input focus.
- Only **one** external overlay is drawn per app. Gamescope picks the most opaque untagged one. mangoapp
  (the performance overlay) also owns an external-overlay window. While the performance overlay is off,
  its flag is `0`, so ours wins. With the performance overlay on, expect a conflict (**untested**).
- A small box window and a full-screen transparent window (mangoapp's and OverLaid's shape) both work.
  The overlay test's variants 1 and 2 were seen over PoE; variants 3 and 4 on the game's display were not.

- **Race to avoid:** the properties are set over a second X connection (ctypes), while GTK creates the
  window over its own connection. Call `Gdk.Display.get_default().sync()` after `realize()` and before
  setting properties. Otherwise the server may not know the window yet: the properties are rejected with
  `BadWindow`, and because the second connection is closed straight away, the error never shows. The
  price box only worked by luck before this was fixed.

- **Hide by destroying, not unmapping:** gamescope only forgets an external overlay when its window is
  destroyed. An unmapped (hidden) one stays the external overlay and its last frame keeps being drawn.
  This left a "Clasped Skull" price box stuck on screen. `overlay.py` now destroys the window on hide and
  creates a new one, with its properties, on the next show.

### Debugging tools

- `gamescopectl focus_info` (with `XDG_RUNTIME_DIR=/run/user/1000 GAMESCOPE_WAYLAND_DISPLAY=gamescope-0`)
  writes the focus state into gamescope's log (`journalctl --user`, not the terminal). It shows the
  focus, input-focus, keyboard-focus, override and Steam-overlay windows, and the "focused app". It does
  not list the external overlay.
- `gamescopectl screenshot <file> [type]`: type 1 (default) is the game only; 2 adds "real" overlay
  layers; 3 is "full composition". On this Deck even type 3 did **not** show our overlays, so only a person
  looking at the screen can confirm an overlay is visible.
- List a display's top-level windows with their `GAMESCOPE_*` / `STEAM_*` properties, map state and
  override-redirect flag (`XQueryTree` + `XListProperties` via ctypes). That's how the missing
  properties were found.
- To test over the game, script the test to wait until Steam reports the game in front:
  `SteamUIStore.MainRunningAppID` via Steam's debugging port (`http://127.0.0.1:8080/json`, target
  `SharedJSContext`). Otherwise the app in front is often the nested desktop.

## Approaches that did not work

| Approach | Result |
| --- | --- |
| Steam notification toasts (`toaster.toast`) | Arrive in the bell tray but are not shown over a running game. Do Not Disturb was off. |
| Faking another toast type (`eType` 2 or 13) | **Broke Steam's UI**: the Main Menu, Quick Access and notification windows disappeared until `steamwebhelper` was restarted. Don't do this. |
| Drawing into Steam's `notificationtoasts` window via CDP | That window is only 320×80 and is hidden unless Steam itself shows a toast. Nothing appeared. |
| Overlay window on the game's display (`:1`) | Not drawn over PoE, with or without the overlay flag. |
| Our window as the Steam overlay (`STEAM_OVERLAY=1`), meant to be visible | Gamescope picks it for the Steam-overlay slot (and, with `STEAM_INPUT_FOCUS`, gives it input), but **never draws it**: not as a pop-up, not as a normal window, not with `STEAM_GAME=769`, not with an explicit `_NET_WM_WINDOW_OPACITY`. |
| `STEAM_GAME=<game id>` or `769` on the input window | Gamescope then no longer uses it as the Steam overlay ("Global overlay window: none"), so it gets no input. Leave `STEAM_GAME` off. |
| Steam-overlay window up while opening the Quick Access Menu | First experiment (window not visible, nested desktop in front): the QAM flickered and the **Deck froze**; a reboot was needed. Not yet retried with the working setup below. |

## Buttons in the overlay (verified)

Gamescope 3.17 never gives input to an external overlay, and never draws a non-Steam window placed in
the Steam-overlay slot. Each layer does one half, so a menu with buttons uses **two windows from one
process**, both on Steam's display `:0` and both full-screen and transparent:

```
            ┌──────────── view window ─────────────┐   GAMESCOPE_EXTERNAL_OVERLAY = 1
 drawn  ◀── │  draws the menu + buttons (cairo)    │   GAMESCOPE_NO_FOCUS         = 1
            └──────────────────────────────────────┘   (gamescope draws it, never gives it input)

            ┌──────────── input window ────────────┐   STEAM_OVERLAY     = 1
 input  ──▶ │  fully transparent, catches clicks   │   STEAM_INPUT_FOCUS = 1
            └──────────────────────────────────────┘   no STEAM_GAME!
                                                        (gamescope gives it input, never draws it)

 click (x, y) on the input window ──▶ hit-test against the button rectangles ──▶ toggle + redraw the view
```

Verified over PoE (probe F, 18:56): three taps/trackpad clicks arrived at the input window as
mouse `button1` events, at exactly the coordinates of the drawn buttons (Mod A, Mod B, Close).
Working reference: [examples/overlay_buttons.py](examples/overlay_buttons.py).

Open questions (**untested** with this setup):

- Does the game also receive the clicks while the menu is up? Gamescope moves input focus to our
  window, so it shouldn't.
- What happens when the Quick Access Menu or Steam menu is opened while the menu is up? That froze the
  Deck once (see above), so the UI must close itself first.
- How the "focused app" behaves while the menu is up. In earlier probes it switched to the starting
  process's app ID. Started from the plugin, it may be `0`.
- Controller buttons (A/B, d-pad) as input. Only taps and trackpad clicks were tested. The input window
  also listens for keys, but nothing arrived in the probes.

## The affix menu (v1.2.0, verified over PoE)

`menu.py` is the first UI built this way: the rare/magic affix picker with search results. It follows the
guide below: JSON lines with `main.py` (`mods` / `busy` / `results` in, `search` / `close` out), one layout
table for drawing and hit-testing, windows mapped only when content arrives. It closes itself on Close,
after 45 s idle, on stdin EOF, and when Steam's Big Picture window sets `STEAM_OVERLAY=1`. That flag was
verified to switch 0 → 1 when the Quick Access Menu opens and back when it closes; the menu polls it every
200 ms. Verified over PoE: three rares opened, searched and closed from the overlay. Opening the Quick Access Menu
while the menu was up closed it ("menu closed: steam_overlay"), and the QAM opened normally: verified both
locally and over PoE.

## The price-check menu (v1.3.0): Awakened PoE Trade's window, natively

v1.3.0 extends `menu.py` into a native version of Awakened PoE Trade's price-check window instead of
embedding APT's web UI. Embedding was tested and dropped:

- A separate window over the game can't take pointer input. A window on PoE's display (`:1`) tagged
  `STEAM_GAME=238960` becomes gamescope's "override window": it is drawn and gets keyboard focus, but
  taps and clicks still go to the game (probe G). So any UI must use the view + input window pair above.
- APT's renderer could be served by our backend (its main process is a small HTTP + WebSocket server,
  `main/src/server.ts`, protocol in `ipc/types.ts`) and drawn by headless Chrome (the Chrome flatpak with
  `--headless=new --remote-debugging-port`). Frames would be copied into the view window and taps
  forwarded with CDP `Input.dispatchMouseEvent` (tested: works). It was not built: a whole browser next to
  PoE costs battery and frame time, and APT itself presses Ctrl+C for you, which this plugin must not do.

What was ported, mapped to APT's components (`renderer/src/web/price-check`):

| APT | `menu.py` |
|---|---|
| `FiltersBlock`, `FilterBtnNumeric`/`FilterBtnLogical` | filter chips: ilvl, links, sockets, quality, influence, corrupted (cycles any/yes/no), instant buyout (`OnlineFilter`) |
| `FilterModifier` (roll + editable min) | each affix: toggle, − / min / +, tap the min for "any value" |
| stat group "count" | **match n/m** |
| `TradeListing` | results table: price, in chaos, ilvl, listed age, seller; 20 listings, 10 per page |
| poe.ninja line for uniques | blue line under the title |

Protocol additions: `mods` messages carry `ninja`, per-mod `value`/`min`/`step` and `filters`
(`{"id", "label", "options", "value"}`, where `[false, true]` is a toggle); `search` returns `mins` and
`filters`; `results` carries `info` lines and `rows`. `main.py` turns the filter values into trade
filters (`Pricer._apply_filters`).

Since v1.4.0 the rows and chips come from APT's own engine (`engine/`, run with Node.js): `main.py` asks it
for the item's filter presets, `aptengine.menu_view()` turns the active preset into `mods` rows and chips,
and `aptengine.apply_menu()` writes the choices back before the engine searches. A chip with `"emit": true`
(the preset switch) reports each tap as `{"action": "chip", "id", "value"}` and `main.py` resends the
menu. `"required_default": "all"` makes match n/m start at all selected affixes, like APT.

Tapping a results row opens the **item view** (v1.3.1): the listed item as the trade site returns it,
compacted by `poeprice.item_card()` (properties, requirements, sockets, item level, influences, enchant /
implicit / fractured / explicit / crafted affixes with their tier and roll range, corrupted, the seller's
note). `results` messages carry these cards as `items`, one per row; `main.py` leaves them out of what it
sends to the Quick Access panel (`_lite`). Prev / Next step through the listings; Back returns to the page
the item was on.

Text is drawn with cairo's toy font API, which has no font fallback: `≥`, `≈`, `▲▼◀▶` render as boxes
with the default Sans font, so labels use ASCII ("90+", "Up", "< Prev").

`menu.py --preview OUT.png < messages.jsonl` renders the menu to PNGs without X or gamescope: message
lines update the state, `{"click": [x, y]}` lines are taps, and `{"shot": "name"}` lines write
`OUT-name.png`. Use it to check layout changes before testing over the game.

## How to build a UI on this

### Process layout

Keep the UI in a separate process started by `main.py` (like `overlay.py`): system `/usr/bin/python3 -I`,
`DISPLAY=:0`, without `XAUTHORITY`/`WAYLAND_DISPLAY`, talking JSON lines over stdin/stdout.

```
main.py ──stdin──▶ menu process      {"mods": [...], "summary": "...", "busy": false}  (show / update)
        ◀─stdout── menu process      {"toggle": 2} {"search": true} {"close": "button"}  (user actions)
```

The menu process only draws and reports. `main.py` keeps the state, calls
`pricer.trade_search(result, opts)` for "Search again" (the same code path as the `research` callable)
and sends the new summary back.

### Window setup (both windows)

1. `Gtk.Window(type=POPUP)` (override-redirect), full screen at `(0, 0)`, RGBA visual,
   `set_app_paintable(True)`. Clear the whole window in the `draw` handler (`cairo` operator CLEAR),
   then draw.
2. `realize()` both windows, then `Gdk.Display.get_default().sync()`.
3. Set the properties on `win.get_window().get_xid()` (that *is* the top-level window for a GTK popup):
   - view: `GAMESCOPE_EXTERNAL_OVERLAY=1`, `GAMESCOPE_NO_FOCUS=1`
   - input: `STEAM_OVERLAY=1`, `STEAM_INPUT_FOCUS=1`, no `STEAM_GAME`
4. `show_all()` both.
5. On the input window: `add_events(BUTTON_PRESS | TOUCH | KEY_PRESS)` before `realize()`, and handle
   `button-press-event` (taps arrive as mouse button 1) and `touch-event`.

### Layout and hit-testing

- Draw everything in the view window from one layout table, e.g. `[(id, x, y, w, h, label), …]`, and
  hit-test clicks against the same table. Both windows are full screen at `(0, 0)`, so coordinates
  match 1:1. The screen is 1280×800; take it from `screen.get_width()/get_height()`.
- Make targets finger-sized: about 60–70 px tall. The touchscreen has no hover, and a trackpad click
  lands where the cursor is.
- Give immediate feedback: redraw the button on the click (colour or "ON/off"), then report the action.
- A mod list fits as one row per mod (toggle on the left, text, tier/kind on the right), plus a bottom row
  with "Must match: − N +", "Search again" and "Close". Show the price summary in the header and
  "Searching…" while `main.py` is busy.

### Safety rules

- **Only map the input window while the menu is meant to be used**, and unmap or close it right
  afterwards. While it is up, the game gets no input.
- **Close on Steam's overlay:** in the frontend, `useQuickAccessVisible()` / `SteamClient.Overlay`
  events, or in `main.py` by watching Steam's Big Picture window: its `STEAM_OVERLAY` was seen
  switching between 1 and 0, presumably when the QAM or Steam menu opens and closes (**unverified** as a
  signal). Close the menu before Steam takes the slot.
- **Always time out** (e.g. 30 s without input) and close on stdin EOF, so a stuck menu can't block the game.
- **Never send input** to the game. The menu only reads clicks on its own window.
- If anything goes wrong, killing the process restores everything: gamescope returns input to the game as
  soon as the window disappears.

### Opening the menu

Options, all compatible with the "no automation" rule:

- Automatically after a rare is priced (like the current `popup` setting), with a visible "Close" and
  the timeout.
- From the plugin panel ("Edit mods in overlay").
- Later: a controller shortcut read by the plugin (see the untested input sources below).

## Other input options (untested)

- **Controller shortcuts read by the plugin:** `SteamClient.Input.RegisterForControllerInputMessages` /
  `RegisterForControllerAnalogInputMessages` / `RegisterForControllerCommandMessages` exist on this Deck.
  Whether they fire while a game has focus is unknown. Alternative: read the controller's evdev device
  from the backend, read-only and without grabbing it.
- **Steam Input touch/radial menus:** drawn by Steam over games and interactive, but each entry can only
  send a key or Steam action, not call the plugin.
- **Quick Access panel:** verified and still the fallback. The panel opens for rares via the plugin
  selecting itself (private `DeckyPluginLoader.deckyState`) and `Navigation.OpenQuickAccessMenu(999)`.
