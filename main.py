"""PoE Price Check - Decky backend.

Finds the running Path of Exile process, watches the CLIPBOARD of the X display it draws on (a gamescope
Xwayland in Game Mode), and when PoE copies an item (Ctrl+C / Ctrl+Alt+C over an item) prices it and
emits a "poe_price" event that the frontend shows as a Steam toast.
"""
import asyncio
import hashlib
import json
import os
import time
import urllib.parse

import aptengine
import aptupdate
import decky
import poeprice

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
ENGINE_RUNTIME = os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "engine")  # APT engine updates live here
DEFAULTS = {
    "enabled": True,
    "league": "",           # "" = current challenge league (first one the trade site lists)
    "toasts": True,
    "instant_buyout": False,
    "mod_factor": 0.85,     # search rolls >= 85% of the item's value
    "use_ilvl": False,
    "popup": "rares",       # open the QAM panel on a check: "off" | "rares" | "always"
    "overlay": True,        # show the price in a box over the game (top-left)
    "overlay_seconds": 8,   # how long the box stays
    "rare_menu": True,      # rares/magic: pick affixes in an overlay menu first, then search
}
RARITY_COLORS = {"Unique": "#e08a3c", "Rare": "#ffff77", "Magic": "#8888ff", "Gem": "#1ba29b",
                 "Currency": "#d8cfb4", "Divination Card": "#c0e0ff"}
PYTHON = "/usr/bin/python3"
GAME_EXES = ("pathofexile_x64steam.exe", "pathofexilesteam.exe", "pathofexile_x64.exe", "pathofexile.exe")


def _clean_env():
    """Environment for the system python child: drop the PyInstaller paths Decky runs with."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "LD_PRELOAD", "_MEIPASS2")}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def find_game_display():
    """(pid, DISPLAY, XAUTHORITY) of the running Path of Exile client, or None."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0].decode(errors="replace")
        except OSError:
            continue
        base = argv0.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base not in GAME_EXES:
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                env = dict(x.split(b"=", 1) for x in f.read().split(b"\0") if b"=" in x)
        except OSError:
            continue
        display = env.get(b"DISPLAY", b"").decode()
        if display:
            xauth = env.get(b"XAUTHORITY", b"").decode()
            # Inside Steam's pressure-vessel container the path isn't valid outside it.
            if xauth.startswith("/run/pressure-vessel") or not os.path.exists(xauth):
                xauth = ""
            return int(pid), display, xauth
    return None


def _lite(result):
    """A result without the listed items' cards (only the overlay menu shows those), for the panel."""
    t = result.get("trade")
    if not t or not t.get("listings"):
        return result
    return {**result, "trade": {**t, "listings": [{k: v for k, v in l.items() if k != "item"}
                                                  for l in t["listings"]]}}


def _age(iso):
    """'2026-11-15T10:24:01Z' -> '3h' (how long ago a listing was indexed)."""
    if not iso:
        return ""
    try:
        import datetime
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        sec = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
    except Exception:
        return ""
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= n:
            return f"{int(sec // n)}{unit}"
    return "now"


class Plugin:
    async def _main(self):
        self.loop = asyncio.get_running_loop()
        self.settings = dict(DEFAULTS)
        try:
            with open(SETTINGS_FILE) as f:
                self.settings.update(json.load(f))
        except Exception:
            pass
        self.pricer = poeprice.Pricer(os.path.join(decky.DECKY_PLUGIN_RUNTIME_DIR, "cache"))
        self.history = []        # newest first
        self.status = {"game": None, "display": None, "watching": False, "error": None, "busy": False}
        self.child = None
        self.overlay = None       # debug overlay window process
        self.overlay_test = None  # state of the one-button overlay test
        self.price_overlay = None  # overlay.py process showing prices over the game
        self._overlay_hide = None
        self.menu = None          # menu.py process (affix picker + results over the game)
        self.menu_key = None      # history key of the item shown in the menu
        self._leagues = []
        # Awakened PoE Trade's engine (Node.js) for the menu's filters and searches; the Python pricer
        # stays as the fallback when Node or the engine isn't there.
        self.engine = aptengine.Engine(os.path.join(self._engine_dist(), "engine.mjs"),
                                       getattr(decky, "DECKY_USER_HOME", "/home/deck"), decky.logger, _clean_env(),
                                       bundled_node=os.path.join(PLUGIN_DIR, "bin", "node"))
        self.engine_update = {"checking": False, "updating": False, "progress": "", "error": None, "latest": None}
        self.task = asyncio.create_task(self._supervise())
        asyncio.create_task(self._warm_up())
        decky.logger.info("PoE Price Check started")

    async def _unload(self):
        if getattr(self, "task", None):
            self.task.cancel()
        await self._stop_child()
        await self.stop_overlay_test()
        await self._stop_price_overlay()
        await self._close_menu()
        await self.engine.stop()

    async def _uninstall(self):
        pass

    # ------------------------------------------------------------------------------------- internals
    async def _run(self, fn, *args):
        return await self.loop.run_in_executor(None, fn, *args)

    async def _warm_up(self):
        """Fetch leagues/stats/prices in the background so the first check is fast."""
        try:
            self._leagues = await self._run(self.pricer.leagues)
            league = self.league()
            await self._run(self.pricer.exchange, league)
            await self._run(self.pricer.stat_index)
            await self._run(self.pricer.trade_currency_names)
        except Exception as e:
            decky.logger.warning(f"warm-up failed: {e}")

    def league(self):
        if self.settings.get("league"):
            return self.settings["league"]
        return self._leagues[0] if self._leagues else "Standard"

    async def _stop_child(self):
        child, self.child = self.child, None
        if child and child.returncode is None:
            child.terminate()
            try:
                await asyncio.wait_for(child.wait(), 2)
            except asyncio.TimeoutError:
                child.kill()
        self.status["watching"] = False

    async def _supervise(self):
        while True:
            try:
                game = find_game_display() if self.settings.get("enabled", True) else None
                if not game:
                    if self.child:
                        await self._stop_child()
                    if self.price_overlay:
                        await self._stop_price_overlay()
                    if self.menu:
                        await self._close_menu()
                    self.status.update(game=None, display=None)
                elif self.child is None or self.child.returncode is not None or self.status["display"] != game[1]:
                    await self._stop_child()
                    await self._start_child(*game)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.exception("supervisor")
                self.status["error"] = str(e)
            await asyncio.sleep(3)

    async def _start_child(self, pid, display, xauth):
        env = _clean_env()
        env["DISPLAY"] = display
        if xauth:
            env["XAUTHORITY"] = xauth
        else:
            env.pop("XAUTHORITY", None)
        self.child = await asyncio.create_subprocess_exec(
            PYTHON, "-I", os.path.join(PLUGIN_DIR, "clipwatch.py"), display,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, limit=1 << 22)
        self.status.update(game=pid, display=display, error=None)
        decky.logger.info(f"watching clipboard of PoE (pid {pid}) on {display}")
        asyncio.create_task(self._read_child(self.child))

    async def _read_child(self, child):
        while True:
            line = await child.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("ready"):
                self.status["watching"] = True
                decky.logger.info(f"clipwatch ready: {msg}")
            elif msg.get("error"):
                self.status["error"] = msg["error"]
            elif "text" in msg and not msg.get("initial"):
                text = msg["text"]
                decky.logger.info(f"clipboard changed: {text[:60]!r}")
                if text.startswith("Item Class:"):
                    asyncio.create_task(self._check(text))
        err = (await child.stderr.read()).decode(errors="replace").strip()
        if err:
            decky.logger.warning(f"clipwatch exited: {err[-500:]}")
            self.status["error"] = err.splitlines()[-1][:200]
        if child is self.child:
            self.status["watching"] = False

    def _opts(self):
        return {k: self.settings.get(k) for k in ("instant_buyout", "mod_factor", "use_ilvl")}

    def _use_menu(self, item):
        return (bool(item) and (item.get("rarity") in ("Rare", "Magic") or item.get("rarity") == "Unique" and item.get("name"))
                and self.settings.get("rare_menu", True)
                and not (self.overlay_test or {}).get("running"))

    async def _reopen_menu(self, text_hash):
        """Copying an item again reopens its menu with the choices made last time (filters, minimums,
        match count, preset), e.g. after closing it by accident."""
        prev = next((r for r in self.history if r.get("text_hash") == text_hash and r.get("menu")), None)
        if not prev:
            return False
        await self._overlay_hide_now()
        self.history.remove(prev)
        self.history.insert(0, prev)
        if not await self._open_menu(prev):
            return False
        decky.logger.info(f"menu reopened for {prev['item'].get('name') or prev['item'].get('base')}")
        await decky.emit("poe_price", _lite(prev), False, "off")
        return True

    async def _check(self, text):
        item = poeprice.parse_item(text)
        use_menu = self._use_menu(item)
        text_hash = hashlib.sha1(text.encode()).hexdigest()
        if use_menu and await self._reopen_menu(text_hash):
            return
        self.status["busy"] = True
        opts = self._opts()
        if use_menu:
            opts["pick_mods"] = True
            await self._overlay_hide_now()  # one external overlay at a time: the menu replaces the price box
        else:
            if self.menu:
                await self._close_menu()
            if item:
                await self._overlay_show({"title": item.get("name") or item.get("base") or "Item",
                                          "lines": ["Checking price…"],
                                          "color": RARITY_COLORS.get(item.get("rarity"), "#ffffff")}, hide_after=20)
        try:
            result = await self._run(self.pricer.check, text, self.league(), opts)
        except Exception as e:
            decky.logger.exception("price check")
            result = {"item": {"name": "", "base": "Price check failed"}, "summary": str(e), "error": str(e)}
        finally:
            self.status["busy"] = False
        if not result:
            return
        result["key"] = f"{time.time():.3f}"
        result["text_hash"] = text_hash
        if result.get("pending") and self.engine.available():
            try:
                apt = await self.engine.call("check", 20, text=text, league=self.league())
                result["apt"] = {"presets": apt["presets"], "active": apt["active"]}
                for p in result["apt"]["presets"]:
                    p["filters"]["trade"]["merchantOnly"] = bool(self.settings.get("instant_buyout"))
                if apt["item"].get("unknown"):
                    decky.logger.info(f"APT: unknown mods {apt['item']['unknown']}")
            except Exception as e:
                decky.logger.warning(f"APT engine check failed, using the built-in search: {e}")
        self.history.insert(0, result)
        del self.history[15:]
        if result.get("pending"):
            if await self._open_menu(result):
                result["menu"] = True
                await decky.emit("poe_price", _lite(result), False, "off")
                return
            # the menu failed to start: search right away and show the price box instead
            try:
                await self._run(self.pricer.trade_search, result, self._opts())
            except Exception as e:
                result["error"] = poeprice.friendly_error(e)
            result.pop("pending", None)
            result["summary"] = poeprice.summarize(result)
        shown = await self._overlay_show(self._overlay_message(result))
        # Steam keeps plugin toasts in the tray during games, so no toast when the overlay showed the price
        toast = bool(self.settings.get("toasts", True)) and not shown
        await decky.emit("poe_price", _lite(result), toast, self.settings.get("popup", "rares"))

    # ------------------------------------------------------------------------------- affix menu
    # menu.py: affix picker + results over the game (docs/OVERLAY.md, "Buttons in the overlay").
    # It closes itself on Close, after 45 s idle, and as soon as Steam's own overlay opens.
    def _menu_mods_message(self, r):
        item = r.get("item", {})
        bits = [item.get("rarity", ""), item.get("class", "")]
        if item.get("name"):
            bits.append(item.get("base", ""))
        if item.get("ilvl"):
            bits.append(f"ilvl {int(item['ilvl'])}")
        if item.get("corrupted"):
            bits.append("corrupted")
        bits.append(r.get("league", ""))
        n = r.get("ninja")
        ninja = None
        if n:
            ninja = f"poe.ninja: {poeprice.fmt_chaos(n['total'], r.get('divine') or 0)}"
            if n.get("variant"):
                ninja += f" ({n['variant']})"
            if n.get("listings"):
                ninja += f" · {n['listings']} listed"
        factor = float(self.settings.get("mod_factor", 0.85))
        mods = []
        for m in r.get("mods", []):
            v = m.get("value")
            if "min" in m:
                lo = m["min"]
            elif v is not None:
                lo = poeprice.default_min(v, factor)
            else:
                lo = None
            step = 1 if v is None or abs(v) >= 10 or float(v).is_integer() else 0.1
            if v is not None and abs(v) >= 200:
                step = 5
            mods.append({"text": m["text"], "kind": m.get("kind", ""), "enabled": m.get("enabled", False),
                         "value": v, "min": lo, "step": step})
        msg = {"type": "mods", "title": item.get("name") or item.get("base") or "Item",
               "subtitle": " · ".join(b for b in bits if b), "ninja": ninja,
               "color": RARITY_COLORS.get(item.get("rarity"), "#ffffff"),
               "mods": mods, "filters": self._menu_filters(r), "required": r.get("required")}
        if r.get("apt"):
            # Awakened PoE Trade's filters: its stat rows, roll minimums and item filters
            msg["mods"], msg["filters"] = aptengine.menu_view(r["apt"])
            msg["required_default"] = "all"
            msg["subtitle"] += " · APT"
        return msg

    def _menu_filters(self, r):
        """Item filter chips for the menu (APT's filter row); r["filters"] keeps the last choice."""
        item, saved = r.get("item", {}), r.get("filters") or {}
        unique = item.get("rarity") == "Unique"
        out = []

        def add(fid, label, default, options=(False, True)):
            out.append({"id": fid, "label": label, "options": list(options), "value": saved.get(fid, default)})

        if item.get("ilvl") and not unique:
            add("ilvl", f"ilvl {int(item['ilvl'])}+", bool(self.settings.get("use_ilvl")))
        if item.get("links", 0) >= 5:
            add("links", f"{item['links']}L", True)
        if item.get("sockets", 0) >= 6 and item.get("links", 0) < 6:
            add("sockets", f"{item['sockets']}S", False)
        if item.get("quality", 0) >= 20 or unique and item.get("quality"):
            add("quality", f"Q{int(item['quality'])}+", False)
        if item.get("influences"):
            add("influences", " + ".join(item["influences"]), item.get("rarity") == "Rare")
        add("corrupted", "Corrupted", ("yes" if item.get("corrupted") else "no") if unique else "any",
            ("any", "yes", "no"))
        add("buyout", "Instant buyout", bool(self.settings.get("instant_buyout")))
        return out

    def _menu_results_message(self, r):
        t = r.get("trade") or {}
        div = r.get("divine") or 0
        info = []
        if t.get("total") is not None:
            info.append(f"{t['total']} listed " + ("for instant buyout" if r.get("buyout") else "online"))
        searched = r["affixes"] if "affixes" in r else sum(1 for m in r.get("mods", []) if m.get("enabled"))
        if r.get("item", {}).get("rarity") != "Unique" and not searched:
            info.append("No affixes selected: searched by item type only")
        rows, items = [], []
        for l in t.get("listings") or []:
            items.append(l.get("item") or {})
            cur = {"chaos": "c", "divine": " div"}.get(l["currency"], " " + str(l["currency"]))
            eq = poeprice.fmt_chaos(l["chaos"], 0) if l.get("chaos") is not None else "?"
            rows.append([f"{l['amount']:g}{cur}", eq if l["currency"] != "chaos" else "",
                         str(l["ilvl"]) if l.get("ilvl") else "", _age(l.get("indexed")), l.get("account") or ""])
        summary = poeprice.summarize({**r, "ninja": None})
        ninja = self._menu_mods_message(r)["ninja"]
        return {"type": "results", "summary": summary, "ninja": ninja, "info": info, "rows": rows, "items": items,
                "error": r.get("error") if not t.get("listings") else None}

    async def _send_menu(self, msg):
        proc = self.menu
        if not proc or proc.returncode is not None:
            return False
        try:
            proc.stdin.write((json.dumps(msg) + "\n").encode())
            await proc.stdin.drain()
            return True
        except Exception:
            return False

    async def _open_menu(self, result):
        try:
            if self.menu is None or self.menu.returncode is not None:
                env = _clean_env()
                env["DISPLAY"] = ":0"
                env.pop("XAUTHORITY", None)
                env.pop("WAYLAND_DISPLAY", None)
                self.menu = await asyncio.create_subprocess_exec(
                    PYTHON, "-I", os.path.join(PLUGIN_DIR, "menu.py"), ":0", "--idle-timeout", "45",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env=env)
                ready = await asyncio.wait_for(self.menu.stdout.readline(), 8)
                if not ready:
                    raise RuntimeError((await self.menu.stderr.read()).decode(errors="replace")[-300:])
                decky.logger.info(f"menu ready: {ready.decode().strip()}")
                asyncio.create_task(self.menu.stderr.read())  # drain GTK warnings
                asyncio.create_task(self._read_menu(self.menu))
            self.menu_key = result["key"]
            return await self._send_menu(self._menu_mods_message(result))
        except Exception as e:
            decky.logger.warning(f"menu failed: {e}")
            await self._close_menu()
            return False

    async def _read_menu(self, proc):
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            action = msg.get("action")
            if action == "search":
                asyncio.create_task(self._menu_search(msg.get("enabled") or [], msg.get("required"),
                                                      msg.get("mins"), msg.get("filters")))
            elif action == "chip" and msg.get("id") == "preset":
                result = next((r for r in self.history if r.get("key") == self.menu_key), None)
                if result and result.get("apt") and aptengine.set_preset(result["apt"], msg.get("value")):
                    result["required"] = None
                    await self._send_menu(self._menu_mods_message(result))
            elif action == "close":
                decky.logger.info(f"menu closed: {msg.get('reason')}")
                result = next((r for r in self.history if r.get("key") == self.menu_key), None)
                if result and msg.get("enabled") is not None:  # keep the choices for the next copy
                    self._save_menu_choices(result, msg["enabled"], msg.get("mins"), msg.get("filters"),
                                            msg.get("required"))
                break
        if proc is self.menu:
            self.menu = None
            self.menu_key = None

    def _save_menu_choices(self, result, enabled, mins, filters, required):
        """Store the menu's choices in the result: used by the search and when the menu is reopened."""
        result["required"] = required
        if result.get("apt"):
            aptengine.apply_menu(result["apt"], enabled, mins, filters)
            return
        for i, (m, on) in enumerate(zip(result.get("mods", []), enabled)):
            m["enabled"] = bool(on)
            if mins is not None and i < len(mins):
                m["min"] = mins[i]
        if filters is not None:
            result["filters"] = filters

    async def _menu_search(self, enabled, required, mins=None, filters=None):
        result = next((r for r in self.history if r.get("key") == self.menu_key), None)
        if not result:
            return
        self._save_menu_choices(result, enabled, mins, filters, required)
        opts = self._opts()
        opts["fetch"] = 20
        if required is not None:
            opts["mods_required"] = required
        if filters is not None:
            opts["filters"] = filters
            opts["instant_buyout"] = bool(filters.get("buyout"))
        result["buyout"] = bool(opts.get("instant_buyout"))
        self.status["busy"] = True
        try:
            if result.get("apt"):
                await self._apt_search(result, enabled, mins, filters, required)
            else:
                await self._run(self.pricer.trade_search, result, opts)
        except Exception as e:
            result["error"] = poeprice.friendly_error(e)
            result.pop("trade", None)
            result["summary"] = poeprice.summarize(result)
        finally:
            self.status["busy"] = False
        result.pop("pending", None)
        if result.get("trade"):
            result["summary"] = poeprice.summarize({**result, "ninja": None})
        decky.logger.info(f"menu search: {sum(1 for x in enabled if x)} affixes, need {required}: "
                          f"{result.get('summary')}")
        if result.get("key") == self.menu_key:
            await self._send_menu(self._menu_results_message(result))
        await decky.emit("poe_price", _lite(result), False, "off")

    async def _apt_search(self, result, enabled, mins, filters, required):
        apt = result["apt"]
        preset = aptengine.active_preset(apt)  # the menu's choices are already applied
        result["buyout"] = bool(preset["filters"]["trade"].get("merchantOnly"))
        result["affixes"] = aptengine.n_enabled(apt)
        league = result["league"]
        res = await self.engine.call("search", 60, filters=preset["filters"], stats=preset["stats"],
                                     league=league, fetch=20, required=required)
        trade = {"total": res.get("total", 0), "id": res.get("queryId"), "listings": [], "engine": "apt",
                 "url": f"{poeprice.TRADE}/trade/search/{urllib.parse.quote(league)}/{res.get('queryId')}"}
        await self._run(self.pricer.finish_trade, result, trade, res.get("results") or [])

    # ---------------------------------------------------------------------------- APT engine updates
    # The bundled engine is engine/dist; "Update" builds a newer APT release into the plugin's runtime folder
    # (it survives plugin reinstalls). Whichever is newer is used.
    def _engine_dist(self):
        bundled = os.path.join(PLUGIN_DIR, "engine", "dist")
        runtime = os.path.join(ENGINE_RUNTIME, "dist")
        rv, bv = aptupdate.read_version(runtime), aptupdate.read_version(bundled)
        if rv and os.path.exists(os.path.join(runtime, "engine.mjs")) and \
                aptupdate.version_key(rv.get("apt")) >= aptupdate.version_key((bv or {}).get("apt")):
            return runtime
        return bundled

    def _engine_info(self):
        dist = os.path.dirname(self.engine.script)
        v = aptupdate.read_version(dist) or {}
        node = self.engine.node()
        latest = self.engine_update.get("latest")
        kit = os.path.join(PLUGIN_DIR, "engine", "kit", "build.mjs")
        return {
            **self.engine_update,
            "version": v.get("apt"), "built": v.get("built"),
            "source": "updated" if dist.startswith(ENGINE_RUNTIME) else "bundled",
            "available": self.engine.available(),
            "node": node, "node_bundled": bool(node) and node == self.engine.bundled,
            "can_update": os.path.exists(kit) and bool(node),
            "update_available": bool(latest) and aptupdate.version_key(latest["tag"]) > aptupdate.version_key(v.get("apt")),
        }

    async def check_engine_update(self):
        st = self.engine_update
        st.update(checking=True, error=None)
        try:
            st["latest"] = await self._run(aptupdate.latest_release)
        except Exception as e:
            st["error"] = f"Couldn't check GitHub: {poeprice.friendly_error(e)}"
        finally:
            st["checking"] = False
        return self._engine_info()

    async def update_engine(self):
        st = self.engine_update
        if st["updating"] or not st.get("latest"):
            return self._engine_info()
        st.update(updating=True, error=None, progress="Starting…")
        asyncio.create_task(self._update_engine(st["latest"]["tag"]))
        return self._engine_info()

    async def _update_engine(self, tag):
        st = self.engine_update

        def progress(msg):
            st["progress"] = msg
        try:
            os.makedirs(ENGINE_RUNTIME, exist_ok=True)
            await self._run(aptupdate.update, tag, os.path.join(PLUGIN_DIR, "engine", "kit"), self.engine.node(),
                            ENGINE_RUNTIME, _clean_env(), progress)
            await self.engine.stop()
            self.engine.script = os.path.join(self._engine_dist(), "engine.mjs")
            decky.logger.info(f"APT engine updated to {tag}: {self.engine.script}")
        except Exception as e:
            decky.logger.exception("APT engine update")
            st["error"] = f"Update failed: {str(e)[-300:]}"
            st["progress"] = ""
        finally:
            st["updating"] = False

    async def _close_menu(self):
        proc, self.menu, self.menu_key = self.menu, None, None
        if proc and proc.returncode is None:
            try:
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), 2)
            except Exception:
                proc.kill()

    async def _overlay_hide_now(self):
        if self._overlay_hide:
            self._overlay_hide.cancel()
            self._overlay_hide = None
        proc = self.price_overlay
        if proc and proc.returncode is None:
            try:
                proc.stdin.write(b'{"hide": true}\n')
                await proc.stdin.drain()
            except Exception:
                pass

    # ------------------------------------------------------------------------------- price overlay
    # A box top-left over the game: overlay.py on Steam's display (:0) with GAMESCOPE_EXTERNAL_OVERLAY,
    # which gamescope draws over whatever app is in front. Display only; it never gets input.
    def _overlay_message(self, r):
        item, div = r.get("item", {}), r.get("divine") or 0
        title = item.get("name") or item.get("base") or "Item"
        lines = [r.get("summary") or "No price"]
        n = r.get("ninja")
        if n:
            src = n.get("source", "poe.ninja")
            if n.get("variant"):
                src += f" · {n['variant']}"
            if n.get("listings") is not None:
                src += f" · {n['listings']} listed"
            lines.append(src)
        t = r.get("trade") or {}
        listings = t.get("listings") or []
        if listings:
            parts = []
            for l in listings[:4]:
                cur = {"chaos": "c", "divine": " div"}.get(l["currency"], " " + str(l["currency"]))
                parts.append(f"{l['amount']:g}{cur}")
            lines.append("Cheapest: " + ", ".join(parts))
        if r.get("error") and not n and not listings:
            lines.append(str(r["error"])[:70])
        if item.get("rarity") in ("Rare", "Magic"):
            lines.append("⋯ → PoE Price Check to pick mods")
        return {"title": title, "lines": lines, "color": RARITY_COLORS.get(item.get("rarity"), "#ffffff")}

    async def _overlay_show(self, msg, hide_after=None):
        """Show msg in the price overlay; returns False when the overlay is off or failed."""
        if (not self.settings.get("overlay", True) or (self.overlay_test or {}).get("running")
                or (self.menu is not None and self.menu.returncode is None)):
            return False
        try:
            if self.price_overlay is None or self.price_overlay.returncode is not None:
                self.price_overlay = await self._spawn_overlay(":0", ["--external-overlay"])
            self.price_overlay.stdin.write((json.dumps(msg) + "\n").encode())
            await self.price_overlay.stdin.drain()
        except Exception as e:
            decky.logger.warning(f"price overlay failed: {e}")
            await self._stop_price_overlay()
            return False
        if self._overlay_hide:
            self._overlay_hide.cancel()
        seconds = hide_after or float(self.settings.get("overlay_seconds", 8))
        self._overlay_hide = asyncio.create_task(self._overlay_hide_later(seconds))
        return True

    async def _overlay_hide_later(self, seconds):
        await asyncio.sleep(seconds)
        proc = self.price_overlay
        if proc and proc.returncode is None:
            try:
                proc.stdin.write(b'{"hide": true}\n')
                await proc.stdin.drain()
            except Exception:
                pass

    async def _stop_price_overlay(self):
        if self._overlay_hide:
            self._overlay_hide.cancel()
            self._overlay_hide = None
        proc, self.price_overlay = self.price_overlay, None
        if proc and proc.returncode is None:
            try:
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), 2)
            except Exception:
                proc.kill()

    async def _spawn_overlay(self, display, flags):
        env = _clean_env()
        env["DISPLAY"] = display
        env.pop("XAUTHORITY", None)
        env.pop("WAYLAND_DISPLAY", None)
        proc = await asyncio.create_subprocess_exec(
            PYTHON, "-I", os.path.join(PLUGIN_DIR, "overlay.py"), display, *flags,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        try:
            ready = await asyncio.wait_for(proc.stdout.readline(), 8)
        except asyncio.TimeoutError:
            ready = b""
        if not ready:
            err = "overlay did not start"
            if proc.returncode is not None:
                err = (await proc.stderr.read()).decode(errors="replace").strip()[-300:]
            else:
                proc.kill()
            raise RuntimeError(err)
        asyncio.create_task(proc.stderr.read())  # drain GTK warnings so the pipe never fills
        return proc

    # ------------------------------------------------------------------------------------- callables
    async def get_state(self):
        return {"settings": self.settings, "status": self.status, "history": [_lite(r) for r in self.history],
                "leagues": self._leagues, "league": self.league(), "engine": self._engine_info()}

    async def set_settings(self, changes: dict):
        self.settings.update({k: v for k, v in changes.items() if k in DEFAULTS})
        if not self.settings.get("overlay", True):
            await self._stop_price_overlay()
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump(self.settings, f, indent=1)
        return self.settings

    async def research(self, key: str, mods: list, mods_required=None):
        """Run the trade search again for a history entry with the user's mod choices."""
        result = next((r for r in self.history if r.get("key") == key), None)
        if not result:
            return None
        result["mods"] = mods
        opts = self._opts()
        if mods_required is not None:
            opts["mods_required"] = mods_required
        try:
            await self._run(self.pricer.trade_search, result, opts)
        except Exception as e:
            result["error"] = str(e)
            result["summary"] = poeprice.summarize(result)
        return result

    async def trade_lookup(self, key: str):
        """Live trade listings for an item that was priced from poe.ninja."""
        result = next((r for r in self.history if r.get("key") == key), None)
        if not result:
            return None
        try:
            await self._run(self.pricer.trade_search, result, self._opts())
            if result.get("ninja"):
                result["summary"] = poeprice.summarize(result)
        except Exception as e:
            result["error"] = str(e)
        return result

    async def check_text(self, text: str):
        """Price an item text directly (for testing from the panel)."""
        await self._check(text)
        return self.history[0] if self.history else None

    async def clear_history(self):
        self.history.clear()

    # ------------------------------------------------------------------------------- debug overlay
    # One button runs every variant in turn so it is easy to see which one gamescope draws over the game.
    OVERLAY_TESTS = [
        # (label, display "steam"/"game", overlay.py flags)
        ("Small box, Steam display, overlay flag", "steam", ["--external-overlay"]),
        ("Full-screen transparent, Steam display, overlay flag", "steam", ["--external-overlay", "--fullscreen"]),
        ("Small box, game display, pop-up", "game", []),
        ("Full-screen transparent, game display, overlay flag", "game", ["--external-overlay", "--fullscreen"]),
    ]
    TEST_DELAY, TEST_STEP = 5, 6  # seconds before the first variant (to close the menu), per variant

    async def _stop_overlay(self):
        proc, self.overlay = self.overlay, None
        if proc and proc.returncode is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                proc.kill()

    async def _show_overlay(self, display, flags, msg):
        self.overlay = await self._spawn_overlay(display, flags)
        self.overlay.stdin.write((json.dumps(msg) + "\n").encode())
        await self.overlay.stdin.drain()
        return {"display": display, "flags": flags}

    async def _run_overlay_test(self):
        n = len(self.OVERLAY_TESTS)
        try:
            for left in range(self.TEST_DELAY, 0, -1):
                self.overlay_test.update(step=0, text=f"Starting in {left} s, close the menu and look top-left")
                await asyncio.sleep(1)
            for i, (label, where, flags) in enumerate(self.OVERLAY_TESTS, 1):
                self.overlay_test.update(step=i, text=f"Test {i}/{n}: {label}")
                if where == "steam":
                    display = ":0"
                else:
                    game = find_game_display()
                    if not game:
                        self.overlay_test["results"].append(f"{i}: skipped, PoE not running")
                        continue
                    display = game[1]
                msg = {"title": f"Test {i}/{n}  -  PoE Price Check",
                       "lines": [label, f"display {display}"]}
                try:
                    info = await self._show_overlay(display, flags, msg)
                    decky.logger.info(f"overlay test {i}/{n}: {label} on {display}: {info}")
                    self.overlay_test["results"].append(f"{i}: shown on {display}")
                except Exception as e:
                    decky.logger.warning(f"overlay test {i}/{n} failed: {e}")
                    self.overlay_test["results"].append(f"{i}: failed ({str(e)[:80]})")
                await asyncio.sleep(self.TEST_STEP)
                await self._stop_overlay()
            self.overlay_test.update(step=n + 1, text="Done. Which test numbers did you see over the game?")
        except asyncio.CancelledError:
            self.overlay_test.update(text="Stopped")
            raise
        finally:
            await self._stop_overlay()
            self.overlay_test["running"] = False

    async def start_overlay_test(self):
        """Show each overlay variant for a few seconds, numbered, after a short countdown."""
        await self.stop_overlay_test()
        await self._stop_price_overlay()  # gamescope draws one external overlay at a time
        self.overlay_test = {"running": True, "step": 0, "text": "Starting", "results": []}
        self._overlay_task = asyncio.create_task(self._run_overlay_test())
        return self.overlay_test

    async def stop_overlay_test(self):
        task = getattr(self, "_overlay_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._stop_overlay()
        return getattr(self, "overlay_test", None)

    async def get_overlay_test(self):
        return getattr(self, "overlay_test", None) or {"running": False, "step": 0, "text": "", "results": []}
