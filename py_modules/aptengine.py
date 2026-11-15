"""Awakened PoE Trade's price-check engine (engine/dist/engine.mjs, run with Node.js) for the plugin.

APT parses the item, picks the filters and roll minimums, and builds the trade query; the plugin's GTK
menu shows APT's filters as rows and chips and sends the choices back. This module has:
  Engine          the Node process (JSON lines, see engine/src/main.ts)
  find_node()     a Node.js binary on this machine
  menu_view()     APT preset -> menu rows and chips
  apply_menu()    menu choices -> APT preset (in place)
The engine only gets the item text from the user's own copy; it never sends input to the game.
"""
import asyncio
import glob
import json
import os
import shutil

PRESET_LABELS = {"filters.preset_pseudo": "Pseudo", "filters.preset_base_item": "Base item",
                 "filters.preset_exact": "Exact", "filters.preset_bulk": "Bulk"}
CORRUPTED = ("any", "yes", "no")


def find_node(home="/home/deck", bundled=None):
    """A Node.js binary: the one bundled with the plugin (bin/node), PATH, the system, then nvm / local
    installs in the Deck user's home."""
    if bundled and os.path.isfile(bundled):
        if not os.access(bundled, os.X_OK):
            try:  # Decky's "install from ZIP" doesn't keep the executable bit
                os.chmod(bundled, 0o755)
            except OSError:
                pass
        if os.access(bundled, os.X_OK):
            return bundled
    found = shutil.which("node")
    if found:
        return found
    cands = ["/usr/bin/node", "/usr/local/bin/node", os.path.join(home, ".local/bin/node")]
    nvm = sorted(glob.glob(os.path.join(home, ".nvm/versions/node/v*/bin/node")),
                 key=lambda p: [int(x) for x in p.split("/v")[-1].split("/")[0].split(".") if x.isdigit()],
                 reverse=True)
    for c in nvm + cands:
        if os.access(c, os.X_OK):
            return c
    return None


class Engine:
    def __init__(self, script, home, logger, env=None, bundled_node=None):
        self.script, self.home, self.log, self.env = script, home, logger, env
        self.bundled = bundled_node  # plugin/bin/node (scripts/build.sh bundles it)
        self.proc = None
        self.pending = {}
        self.next_id = 0
        self.lock = asyncio.Lock()

    def node(self):
        return find_node(self.home, self.bundled)

    def available(self):
        return os.path.exists(self.script) and find_node(self.home, self.bundled) is not None

    async def _start(self):
        if self.proc and self.proc.returncode is None:
            return
        node = find_node(self.home, self.bundled)
        if not node:
            raise RuntimeError("Node.js not found")
        self.proc = await asyncio.create_subprocess_exec(
            node, self.script, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.env, limit=16 * 1024 * 1024)
        ready = await asyncio.wait_for(self.proc.stdout.readline(), 20)
        if b'"ready"' not in ready:
            err = (await self.proc.stderr.read()).decode(errors="replace")[-400:]
            raise RuntimeError(f"engine did not start: {ready[:200]!r} {err}")
        self.log.info(f"APT engine started ({node})")
        asyncio.create_task(self._read(self.proc))
        asyncio.create_task(self._drain(self.proc))

    async def _drain(self, proc):
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            self.log.warning("engine: " + line.decode(errors="replace").rstrip()[:300])

    async def _read(self, proc):
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            fut = self.pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                fut.set_result(msg)
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("engine stopped"))
        self.pending.clear()

    async def call(self, cmd, timeout=30, **kw):
        async with self.lock:
            await self._start()
        self.next_id += 1
        rid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        self.proc.stdin.write((json.dumps({"id": rid, "cmd": cmd, **kw}) + "\n").encode())
        await self.proc.stdin.drain()
        try:
            msg = await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rid, None)
        if msg.get("error"):
            raise EngineError(msg["error"])
        return msg

    async def stop(self):
        proc, self.proc = self.proc, None
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()


class EngineError(Exception):
    pass


# ------------------------------------------------------------------------------------------- mapping
def active_preset(apt):
    return next((p for p in apt["presets"] if p["id"] == apt["active"]), apt["presets"][0])


def _fmt(v):
    if v is None or v == "":
        return ""
    v = float(v)
    return f"{int(v)}" if v.is_integer() else f"{v:.2f}".rstrip("0").rstrip(".")


def stat_text(s):
    text = s.get("text", "")
    roll = s.get("roll")
    if roll and roll.get("value") is not None:
        if text.count("#") == 1:
            return text.replace("#", _fmt(roll["value"]))
        if text.count("#") > 1:
            return f"{text} (avg {_fmt(roll['value'])})"
    if s.get("option") is not None and "#" in text:
        return text.replace("#", _fmt(s["option"].get("value")), 1)
    return text


def _rows(preset):
    """(index, stat) for the stats the menu shows: visible ones, groups by their meta filter."""
    out = []
    for i, s in enumerate(preset["stats"]):
        if s.get("group"):
            out.append((i, s["meta"]))
        elif not s.get("hidden"):
            out.append((i, s))
    return out


def menu_view(apt, item_name=""):
    """APT's active preset as menu rows (mods) and chips (filters)."""
    preset = active_preset(apt)
    mods = []
    for _i, s in _rows(preset):
        roll = s.get("roll") or {}
        value = roll.get("value")
        lo = roll.get("min")
        mods.append({"text": stat_text(s).strip(), "kind": str(s.get("tag", "")),
                     "enabled": not s.get("disabled"),
                     "value": value if isinstance(value, (int, float)) else None,
                     "min": lo if isinstance(lo, (int, float)) else None,
                     "step": 0.1 if roll.get("dp") else 1})
    return mods, chips(apt, preset)


def chips(apt, preset):
    f = preset["filters"]
    out = []

    def add(cid, label, value, options=(False, True), emit=False):
        out.append({"id": cid, "label": label, "options": list(options), "value": value, "emit": emit})

    if len(apt["presets"]) > 1:
        labels = [PRESET_LABELS.get(p["id"], p["id"].split(".")[-1]) for p in apt["presets"]]
        add("preset", "Preset", PRESET_LABELS.get(preset["id"], preset["id"].split(".")[-1]), labels, emit=True)
    relaxed = f.get("searchRelaxed")
    if relaxed:
        exact = f.get("searchExact") or {}
        base = exact.get("baseType") or exact.get("name") or "base"
        add("relaxed", "Search", base if relaxed.get("disabled") else f"any {relaxed.get('category', 'item')}",
            [base, f"any {relaxed.get('category', 'item')}"])
    numeric = [("itemLevel", "ilvl {}+"), ("linkedSockets", "{}L"), ("whiteSockets", "{}W"),
               ("quality", "Q{}+"), ("gemLevel", "Level {}+"), ("mapTier", "Tier {}"),
               ("stackSize", "Stack {}+"), ("areaLevel", "Area {}+")]
    for key, fmt in numeric:
        if f.get(key):
            add(key, fmt.format(_fmt(f[key]["value"])), not f[key].get("disabled"))
    for k, inf in enumerate(f.get("influences") or []):
        add(f"influence:{k}", inf["value"], not inf.get("disabled"))
    c = f.get("corrupted")
    if c:
        add("corrupted", "Corrupted", "no" if c["value"] is False else "yes" if c.get("exact") else "any",
            CORRUPTED)
    if f.get("unidentified"):
        add("unidentified", "Unidentified", not f["unidentified"].get("disabled"))
    add("buyout", "Instant buyout", bool(f["trade"].get("merchantOnly")))
    return out


def apply_menu(apt, enabled, mins, chip_values):
    """Write the menu's choices back into APT's active preset (rows in menu_view order)."""
    preset = active_preset(apt)
    f = preset["filters"]
    for k, (i, _s) in enumerate(_rows(preset)):
        target = preset["stats"][i]
        s = target["meta"] if target.get("group") else target
        if k < len(enabled):
            s["disabled"] = not enabled[k]
        if mins is not None and k < len(mins) and s.get("roll") is not None:
            s["roll"]["min"] = mins[k] if mins[k] is not None else ""
    for cid, v in (chip_values or {}).items():
        if cid == "relaxed" and f.get("searchRelaxed"):
            f["searchRelaxed"]["disabled"] = not str(v).startswith("any ")
        elif cid.startswith("influence:"):
            k = int(cid.split(":")[1])
            if k < len(f.get("influences") or []):
                f["influences"][k]["disabled"] = not v
        elif cid == "corrupted" and f.get("corrupted"):
            f["corrupted"] = {"value": v != "no", "exact": v != "any"}
        elif cid == "unidentified" and f.get("unidentified"):
            f["unidentified"]["disabled"] = not v
        elif cid == "buyout":
            f["trade"]["merchantOnly"] = bool(v)
        elif f.get(cid) and isinstance(f[cid], dict) and "value" in f[cid]:
            f[cid]["disabled"] = not v
    return preset


def set_preset(apt, label):
    for p in apt["presets"]:
        if PRESET_LABELS.get(p["id"], p["id"].split(".")[-1]) == label:
            apt["active"] = p["id"]
            return True
    return False


def n_enabled(apt):
    return sum(1 for _i, s in _rows(active_preset(apt)) if not s.get("disabled"))
