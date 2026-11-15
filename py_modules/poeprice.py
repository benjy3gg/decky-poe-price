"""Path of Exile 1 item parsing and pricing (poe.ninja + official trade API). Standard library only.

Used by main.py (the Decky backend) and runnable on its own for testing:
    python3 -I py_modules/poeprice.py item.txt [League]
"""
from __future__ import annotations

import json
import math
import os
import re
import ssl
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "decky-poe-price/1.5.2 (Steam Deck Decky Loader plugin)"
TRADE = "https://www.pathofexile.com"
NINJA = "https://poe.ninja/poe1/api/economy"

NINJA_EXCHANGE_TYPES = [
    "Currency", "Fragment", "DivinationCard", "Essence", "Scarab", "Oil", "Fossil", "Resonator",
    "DeliriumOrb", "Omen", "Tattoo", "Artifact", "AllflameEmber", "Runegraft",
]
NINJA_UNIQUE_TYPES = [
    "UniqueWeapon", "UniqueArmour", "UniqueAccessory", "UniqueFlask", "UniqueJewel", "UniqueMap",
    "UniqueRelic", "UniqueTincture",
]

# "Item Class:" line -> trade category id
CATEGORIES = {
    "Body Armours": "armour.chest", "Helmets": "armour.helmet", "Gloves": "armour.gloves",
    "Boots": "armour.boots", "Shields": "armour.shield", "Quivers": "armour.quiver",
    "Rings": "accessory.ring", "Amulets": "accessory.amulet", "Belts": "accessory.belt",
    "Trinkets": "accessory.trinket", "Jewels": "jewel.base", "Abyss Jewels": "jewel.abyss",
    "One Hand Swords": "weapon.onesword", "Thrusting One Hand Swords": "weapon.onesword",
    "Two Hand Swords": "weapon.twosword", "One Hand Axes": "weapon.oneaxe", "Two Hand Axes": "weapon.twoaxe",
    "One Hand Maces": "weapon.onemace", "Two Hand Maces": "weapon.twomace", "Sceptres": "weapon.sceptre",
    "Daggers": "weapon.dagger", "Rune Daggers": "weapon.runedagger", "Claws": "weapon.claw",
    "Bows": "weapon.bow", "Staves": "weapon.staff", "Warstaves": "weapon.warstaff", "Wands": "weapon.wand",
    "Fishing Rods": "weapon.rod", "Life Flasks": "flask", "Mana Flasks": "flask", "Hybrid Flasks": "flask",
    "Utility Flasks": "flask", "Tinctures": "tincture", "Maps": "map",
}
# influence flag line ("Shaper Item") -> trade misc filter id
INFLUENCES = {"Shaper": "shaper_item", "Elder": "elder_item", "Crusader": "crusader_item",
              "Redeemer": "redeemer_item", "Hunter": "hunter_item", "Warlord": "warlord_item"}
LOCAL_CLASSES = {c for c, cat in CATEGORIES.items() if cat.startswith(("weapon.", "armour."))}

_NUM = re.compile(r"\d+(?:\.\d+)?")
_RANGE = re.compile(r"\((-?[\d.]+)-(-?[\d.]+)\)")  # advanced copy: "+95(90-99) to maximum Life"
_TAG = re.compile(r"\s+\((implicit|enchant|crafted|fractured|scourge|crucible)\)$")
_PROPERTY = re.compile(
    r"^(Requirements|Level|Str|Dex|Int|Sockets|Item Level|Quality|Stack Size|Map Tier|Armour|Evasion Rating|"
    r"Energy Shield|Ward|Chance to Block|Physical Damage|Elemental Damage|Chaos Damage|Critical Strike Chance|"
    r"Attacks per Second|Weapon Range|Experience|Charm Slots|Limited to|Radius|Item Quantity|Item Rarity|"
    r"Monster Pack Size|More Currency|More Maps|More Scarabs|Note|Lasts|Consumes|Currently has)\b"
)


# ---------------------------------------------------------------------------------------------- HTTP

def _ssl_context():
    for path in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ca-certificates/extracted/tls-ca-bundle.pem"):
        if os.path.exists(path):
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


_SSL = _ssl_context()
_trade_lock = threading.Lock()
_trade_next_ok = 0.0


class TradeError(Exception):
    pass


def _http(url: str, body: dict | None = None, timeout: float = 20) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return json.loads(resp.read().decode())


def _trade(path: str, body: dict | None = None) -> dict:
    """Official trade API with simple spacing between requests and one retry on 429."""
    global _trade_next_ok
    with _trade_lock:
        for attempt in range(2):
            wait = _trade_next_ok - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _trade_next_ok = time.monotonic() + 1.2
            try:
                return _http(TRADE + path, body)
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 0:
                    retry = float(e.headers.get("Retry-After") or 5)
                    if retry > 15:
                        raise TradeError(f"Trade API rate limit, try again in {int(retry)} s")
                    _trade_next_ok = time.monotonic() + retry
                    continue
                detail = ""
                try:
                    detail = json.loads(e.read().decode()).get("error", {}).get("message", "")
                except Exception:
                    pass
                raise TradeError(f"Trade API {e.code}: {detail or e.reason}")
    raise TradeError("Trade API rate limit")


# --------------------------------------------------------------------------------------------- Cache

class Cache:
    def __init__(self, directory: str | None):
        self.dir = directory
        self.mem: dict[str, tuple[float, object]] = {}
        self.lock = threading.Lock()

    def get(self, key: str, ttl: float, fetch):
        now = time.time()
        with self.lock:
            hit = self.mem.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        path = os.path.join(self.dir, re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json") if self.dir else None
        if not hit and path and os.path.exists(path) and now - os.path.getmtime(path) < ttl:
            try:
                with open(path) as f:
                    value = json.load(f)
                with self.lock:
                    self.mem[key] = (os.path.getmtime(path), value)
                return value
            except Exception:
                pass
        try:
            value = fetch()
        except Exception:
            if hit:  # stale is better than nothing
                return hit[1]
            raise
        with self.lock:
            self.mem[key] = (now, value)
        if path:
            try:
                os.makedirs(self.dir, exist_ok=True)
                with open(path, "w") as f:
                    json.dump(value, f)
            except Exception:
                pass
        return value


# -------------------------------------------------------------------------------------------- Parsing

def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_item(text: str) -> dict | None:
    """Parse the text PoE puts on the clipboard (Ctrl+C or Ctrl+Alt+C). None if it isn't an item."""
    text = text.replace("\r\n", "\n").strip()
    if not text.startswith("Item Class:") or "\nRarity:" not in text:
        return None
    sections = [s.strip("\n").split("\n") for s in re.split(r"\n-{8}\n?", text)]
    header = sections[0]
    item: dict = {"class": header[0].split(":", 1)[1].strip(), "raw": text, "mods_raw": []}
    item["rarity"] = header[1].split(":", 1)[1].strip()
    names = [l for l in header[2:] if l.strip()]
    if item["rarity"] in ("Rare", "Unique") and len(names) >= 2:
        item["name"], item["base"] = names[0], names[1]
    else:
        item["name"], item["base"] = "", names[0] if names else ""
    item["base"] = re.sub(r"^(Superior|Synthesised) ", "", item["base"])
    flags = set()
    mod_kind = None
    for sec in sections[1:]:
        for line in sec:
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):  # advanced copy header, e.g. { Prefix Modifier "Hale" (Tier: 2) }
                low = line.lower()
                mod_kind = ("implicit" if "implicit" in low else "crafted" if "master crafted" in low
                            else "enchant" if "enchant" in low else "explicit")
                continue
            m = re.match(r"^(Stack Size|Item Level|Level|Quality|Map Tier): ([+\d,.]+)", line)
            if m:
                key = {"Stack Size": "stack", "Item Level": "ilvl", "Level": "level", "Quality": "quality",
                       "Map Tier": "tier"}[m.group(1)]
                if key not in item:
                    item[key] = _num(m.group(2).lstrip("+").rstrip("%"))
                continue
            if line.startswith("Sockets:"):
                groups = line.split(":", 1)[1].strip().split(" ")
                item["links"] = max((g.count("-") + 1 for g in groups if g), default=0)
                item["sockets"] = sum(g.count("-") + 1 for g in groups if g)
                continue
            if line in ("Corrupted", "Unidentified", "Mirrored", "Split", "Fractured Item", "Synthesised Item",
                        "Foulborn") or line.endswith(" Item") and len(line.split()) <= 3:
                flags.add(line)
                continue
            if _PROPERTY.match(line) or line.endswith(":"):
                continue
            item["mods_raw"].append((line, mod_kind))
        mod_kind = None
    item["flags"] = sorted(flags)
    item["corrupted"] = "Corrupted" in flags
    item["influences"] = [i for i in INFLUENCES if f"{i} Item" in flags]
    if item["rarity"] == "Gem" and "level" not in item:
        item["level"] = 1
    return item


class StatIndex:
    """Maps a modifier line to trade stat ids, e.g. '+95 to maximum Life' -> explicit.stat_3299347043."""

    def __init__(self, stats_json: dict):
        self.by_text: dict[str, dict[str, str]] = {}
        self.multiline: set[str] = set()
        for group in stats_json["result"]:
            for e in group["entries"]:
                key = e["text"]
                if "\n" in key:
                    self.multiline.add(key)
                self.by_text.setdefault(key, {}).setdefault(e["type"], e["id"])

    def _lookup(self, key: str, kind: str, local: bool):
        cands = []
        if local:
            cands.append(key + " (Local)")
        cands.append(key)
        for k in cands:
            ids = self.by_text.get(k)
            if ids and kind in ids:
                return ids[kind], k
        return None, None

    def match(self, line: str, kind: str, local: bool):
        """Return (stat_id, template, value) or (None, None, None)."""
        line = _RANGE.sub("", line).replace(" — Unscalable Value", "").strip()
        values = [float(v) for v in _NUM.findall(line)]
        key = _NUM.sub("#", line)
        value = sum(values) / len(values) if values else None
        sid, tpl = self._lookup(key, kind, local)
        if sid:
            return sid, tpl, value
        swaps = [("-#", "+#"), ("reduced", "increased"), ("less", "more")]
        for a, b in swaps:
            if a in key:
                sid, tpl = self._lookup(key.replace(a, b), kind, local)
                if sid:
                    return sid, tpl, -value if value is not None else None
        if key.startswith("#") or key.startswith("+#"):
            alt = "+" + key if key.startswith("#") else key[1:]
            sid, tpl = self._lookup(alt, kind, local)
            if sid:
                return sid, tpl, value
        return None, None, None


def extract_mods(item: dict, index: StatIndex) -> list[dict]:
    local = item["class"] in LOCAL_CLASSES
    out = []
    raw = item["mods_raw"]
    i = 0
    while i < len(raw):
        line, kind = raw[i]
        m = _TAG.search(line)
        if m:
            kind, line = m.group(1), line[: m.start()]
        kind = kind or "explicit"
        # try two-line templates first
        if i + 1 < len(raw):
            two = line + "\n" + _TAG.sub("", raw[i + 1][0])
            sid, tpl, val = index.match(two, kind, local)
            if sid and tpl in index.multiline:
                out.append(_mod(two, kind, sid, val, item))
                i += 2
                continue
        sid, tpl, val = index.match(line, kind, local)
        if sid:
            out.append(_mod(line, kind, sid, val, item))
        i += 1
    return _add_pseudo(out)


_RES = {"Fire": 1, "Cold": 1, "Lightning": 1}


def _add_pseudo(mods: list[dict]) -> list[dict]:
    """Like Awakened PoE Trade: search total elemental resistance / total life instead of each roll."""
    res_total, res_mods, life_total, life_mods = 0.0, [], 0.0, []
    for m in mods:
        t = m["text"]
        if m["value"] is None:
            continue
        r = re.match(r"^\+?-?[\d.]+% to (.+) Resistances?$", t)
        if r:
            what = r.group(1)
            n = 3 if "all Elemental" in what else sum(1 for k in _RES if k in what)
            if n:
                res_total += m["value"] * n
                res_mods.append(m)
                continue
        if re.match(r"^\+[\d.]+ to maximum Life$", t):
            life_total += m["value"]
            life_mods.append(m)
    extra = []
    if len(res_mods) >= 1 and res_total:
        for m in res_mods:
            m["enabled"] = False
        extra.append({"text": f"+{res_total:g}% total Elemental Resistance", "kind": "pseudo",
                      "id": "pseudo.pseudo_total_elemental_resistance", "value": res_total,
                      "enabled": True})
    if life_mods:
        for m in life_mods:
            m["enabled"] = False
        extra.append({"text": f"+{life_total:g} total maximum Life", "kind": "pseudo",
                      "id": "pseudo.pseudo_total_life", "value": life_total, "enabled": True})
    return extra + mods


def _mod(text, kind, sid, value, item):
    text = _RANGE.sub("", text).replace(" — Unscalable Value", "")
    # Uniques: their mods don't matter for the price search by default; rares: explicit/implicit/fractured do.
    enabled = item["rarity"] in ("Rare", "Magic") and kind in ("explicit", "implicit", "fractured", "crafted")
    return {"text": text, "kind": kind, "id": sid, "value": value, "enabled": enabled}


# -------------------------------------------------------------------------------------------- Pricing

class Pricer:
    def __init__(self, cache_dir: str | None = None):
        self.cache = Cache(cache_dir)

    # data ---------------------------------------------------------------------------------------
    def leagues(self) -> list[str]:
        data = self.cache.get("leagues", 6 * 3600, lambda: _http(TRADE + "/api/trade/data/leagues"))
        return [l["id"] for l in data["result"] if l.get("realm", "pc") == "pc"]

    def stat_index(self) -> StatIndex:
        if not hasattr(self, "_stat_index"):
            data = self.cache.get("stats", 24 * 3600, lambda: _http(TRADE + "/api/trade/data/stats"))
            self._stat_index = StatIndex(data)
        return self._stat_index

    def trade_currency_names(self) -> dict[str, str]:
        data = self.cache.get("static", 24 * 3600, lambda: _http(TRADE + "/api/trade/data/static"))
        return {e["id"]: e["text"] for g in data["result"] for e in g["entries"]}

    def exchange(self, league: str) -> dict[str, float]:
        """name -> chaos value for all exchange-traded things (currency, cards, essences, ...)."""
        def fetch():
            prices: dict[str, float] = {}
            for t in NINJA_EXCHANGE_TYPES:
                q = urllib.parse.urlencode({"league": league, "type": t})
                try:
                    d = _http(f"{NINJA}/exchange/current/overview?{q}")
                except Exception:
                    continue
                names = {it["id"]: it["name"] for it in d.get("items", [])}
                for line in d.get("lines", []):
                    if line["id"] in names and line.get("primaryValue") is not None:
                        prices[names[line["id"]]] = line["primaryValue"]
            prices.setdefault("Chaos Orb", 1.0)
            return prices
        return self.cache.get(f"ninja-exchange-{league}", 20 * 60, fetch)

    def ninja_lines(self, league: str, kind: str) -> list[dict]:
        def fetch():
            q = urllib.parse.urlencode({"league": league, "type": kind})
            lines = _http(f"{NINJA}/stash/current/item/overview?{q}").get("lines", [])
            keep = ("name", "baseType", "variant", "links", "chaosValue", "divineValue", "listingCount",
                    "gemLevel", "gemQuality", "corrupted", "mapTier", "levelRequired", "detailsId")
            return [{k: l.get(k) for k in keep} for l in lines]
        return self.cache.get(f"ninja-{kind}-{league}", 30 * 60, fetch)

    def divine_chaos(self, league: str) -> float:
        return self.exchange(league).get("Divine Orb") or 0.0

    def to_chaos(self, league: str, amount: float, currency: str) -> float | None:
        if currency == "chaos":
            return amount
        name = self.trade_currency_names().get(currency)
        value = self.exchange(league).get(name) if name else None
        return amount * value if value else None

    # main entry ---------------------------------------------------------------------------------
    def check(self, text: str, league: str, opts: dict | None = None) -> dict | None:
        opts = opts or {}
        item = parse_item(text)
        if not item:
            return None
        result: dict = {"item": {k: v for k, v in item.items() if k not in ("raw", "mods_raw")},
                        "league": league, "time": time.time(), "mods": []}
        try:
            result["divine"] = self.divine_chaos(league)
        except Exception:
            result["divine"] = 0.0
        rarity, cls = item["rarity"], item["class"]
        if rarity == "Quest" or cls == "Quest Items":
            result["summary"] = "Quest item, can't be traded"
            return result
        try:
            if rarity in ("Currency", "Divination Card") or "stack" in item:
                self._price_exchange(item, league, result)
            elif rarity == "Unique":
                self._price_unique(item, league, result)
            elif rarity == "Gem" or "Gem" in cls:
                self._price_gem(item, league, result)
            if opts.get("pick_mods") and rarity == "Unique" and item.get("name"):
                result["mods"] = extract_mods(item, self.stat_index())
                result["pending"] = True
                result["summary"] = summarize(result) if "ninja" in result else "Pick filters to search"
                return result
            if "ninja" not in result or opts.get("always_trade"):
                if rarity in ("Rare", "Magic"):
                    result["mods"] = extract_mods(item, self.stat_index())
                    if opts.get("pick_mods"):
                        # the user picks the affixes first (overlay menu); main.py calls trade_search later
                        result["pending"] = True
                        result["summary"] = "Pick affixes to search"
                        return result
                self.trade_search(result, opts)
        except Exception as e:
            result["error"] = friendly_error(e)
        result["summary"] = summarize(result)
        return result

    def _price_exchange(self, item, league, result):
        name = item["base"]
        value = self.exchange(league).get(name)
        if value is None:
            return
        stack = item.get("stack") or 1
        result["ninja"] = {"chaos": value, "total": value * stack, "stack": stack, "source": "poe.ninja exchange"}

    def _price_unique(self, item, league, result):
        name = item["name"] or ""
        links = item.get("links", 0)
        cands = []
        for kind in NINJA_UNIQUE_TYPES:
            for l in self.ninja_lines(league, kind):
                if (name and l["name"] == name) or (not name and l["baseType"] == item["base"]):
                    cands.append(l)
        if not cands:
            return
        if name:
            want_links = links if links >= 5 else None
            exact = [c for c in cands if c.get("links") == want_links] or [c for c in cands if not c.get("links")]
            plain = [c for c in exact if not c.get("variant")] or exact or cands
            best = min(plain, key=lambda c: c["chaosValue"] or 0)
            result["ninja"] = {"chaos": best["chaosValue"], "total": best["chaosValue"],
                               "variant": best.get("variant"), "listings": best.get("listingCount"),
                               "source": "poe.ninja"}
            others = sorted({(c["variant"] or f"{c['links']}L" if c.get("links") else c["variant"] or "", c["chaosValue"])
                             for c in cands if c is not best and (c.get("variant") or c.get("links"))},
                            key=lambda x: x[1])
            if others:
                result["ninja"]["variants"] = [{"label": v, "chaos": p} for v, p in others[:6]]
        else:  # unidentified: could be any unique on this base
            cands.sort(key=lambda c: c["chaosValue"] or 0)
            result["ninja"] = {"chaos": cands[0]["chaosValue"], "total": cands[0]["chaosValue"],
                               "source": "poe.ninja (unidentified)",
                               "variants": [{"label": c["name"], "chaos": c["chaosValue"]} for c in cands[:8]]}

    def _price_gem(self, item, league, result):
        name = item["base"]
        lvl, q = item.get("level", 1), item.get("quality", 0)
        cands = [l for l in self.ninja_lines(league, "SkillGem") if l["name"] == name
                 and bool(l.get("corrupted")) == item["corrupted"]]
        if not cands:
            return
        best = min(cands, key=lambda c: (abs((c.get("gemLevel") or 1) - lvl) * 3 + abs((c.get("gemQuality") or 0) - q) / 5,
                                         c["chaosValue"] or 0))
        result["ninja"] = {"chaos": best["chaosValue"], "total": best["chaosValue"],
                           "variant": f"L{best.get('gemLevel')} Q{best.get('gemQuality') or 0}"
                                      + (" corrupted" if best.get("corrupted") else ""),
                           "listings": best.get("listingCount"), "source": "poe.ninja"}

    # trade --------------------------------------------------------------------------------------
    def build_query(self, result: dict, opts: dict) -> dict:
        item = result["item"]
        status = "securable" if opts.get("instant_buyout") else "online"
        q: dict = {"status": {"option": status}, "stats": [], "filters": {}}
        rarity, cls = item["rarity"], item["class"]
        type_filters: dict = {}
        misc: dict = {}
        if rarity == "Unique":
            if item["name"]:
                q["name"] = item["name"]
            q["type"] = item["base"]
            type_filters["rarity"] = {"option": "unique"}
            if item.get("links", 0) >= 5:
                q["filters"]["socket_filters"] = {"filters": {"links": {"min": item["links"]}}}
            self._add_stats(q, result, opts)
        elif rarity in ("Rare", "Magic"):
            cat = CATEGORIES.get(cls)
            if "Cluster Jewel" in item["base"]:
                cat = "jewel.cluster"
            if cat and cat != "map":
                type_filters["category"] = {"option": cat}
            else:
                q["type"] = self._base_for_magic(item)
            type_filters["rarity"] = {"option": "nonunique"}
            if cat == "map" and item.get("tier"):
                q["filters"]["map_filters"] = {"filters": {"map_tier": {"min": item["tier"], "max": item["tier"]}}}
                q["type"] = self._base_for_magic(item)
            self._add_stats(q, result, opts)
            if opts.get("use_ilvl") and item.get("ilvl"):
                misc["ilvl"] = {"min": int(item["ilvl"])}
            if rarity == "Rare" and opts.get("filters") is None:
                for inf in item.get("influences", []):
                    misc[INFLUENCES[inf]] = {"option": "true"}
        elif rarity == "Gem" or "Gem" in cls:
            q["type"] = item["base"]
            misc["gem_level"] = {"min": int(item.get("level", 1))}
            if item.get("quality"):
                misc["quality"] = {"min": int(item["quality"])}
            misc["corrupted"] = {"option": "true" if item["corrupted"] else "false"}
        else:
            q["type"] = item["base"]
            if rarity == "Normal" and item.get("ilvl") and CATEGORIES.get(cls):
                misc["ilvl"] = {"min": int(item["ilvl"])}
        f = opts.get("filters")
        if f is not None and rarity in ("Rare", "Magic", "Unique"):
            self._apply_filters(q, misc, item, f)
        if type_filters:
            q["filters"]["type_filters"] = {"filters": type_filters}
        if misc:
            q["filters"]["misc_filters"] = {"filters": misc}
        if not q["stats"]:
            q["stats"] = [{"type": "and", "filters": []}]
        return {"query": q, "sort": {"price": "asc"}}

    def _add_stats(self, q: dict, result: dict, opts: dict):
        enabled = [m for m in result["mods"] if m.get("enabled")]
        if enabled:
            filters = []
            for m in enabled:
                f = {"id": m["id"]}
                if "min" in m:
                    if m["min"] is not None:
                        f["value"] = {"min": m["min"]}
                elif m.get("value") is not None:
                    f["value"] = {"min": default_min(m["value"], float(opts.get("mod_factor", 0.85)))}
                filters.append(f)
            need = opts.get("mods_required")
            if need is None:
                n = len(filters)
                need = n if n <= 2 else n - 1 if n <= 4 else n - 2
            q["stats"].append({"type": "count", "value": {"min": max(1, min(int(need), len(filters)))},
                               "filters": filters})

    @staticmethod
    def _apply_filters(q: dict, misc: dict, item: dict, f: dict):
        """Item filters picked in the overlay menu: {"ilvl", "links", "sockets", "quality": bool,
        "corrupted": "any"|"yes"|"no", "influences": bool}; they replace the defaults."""
        misc.pop("ilvl", None)
        q["filters"].pop("socket_filters", None)
        for name in INFLUENCES.values():
            misc.pop(name, None)
        if f.get("ilvl") and item.get("ilvl"):
            misc["ilvl"] = {"min": int(item["ilvl"])}
        if f.get("quality") and item.get("quality"):
            misc["quality"] = {"min": int(item["quality"])}
        sockets = {}
        if f.get("links") and item.get("links"):
            sockets["links"] = {"min": int(item["links"])}
        if f.get("sockets") and item.get("sockets"):
            sockets["sockets"] = {"min": int(item["sockets"])}
        if sockets:
            q["filters"]["socket_filters"] = {"filters": sockets}
        if f.get("corrupted") in ("yes", "no"):
            misc["corrupted"] = {"option": "true" if f["corrupted"] == "yes" else "false"}
        if f.get("influences"):
            for inf in item.get("influences", []):
                misc[INFLUENCES[inf]] = {"option": "true"}

    def _base_for_magic(self, item: dict) -> str:
        """Magic items have one name line ('Hale Gold Ring of the Lynx'); find the base type inside it."""
        if item["rarity"] != "Magic":
            return item["base"]
        data = self.cache.get("items", 24 * 3600, lambda: _http(TRADE + "/api/trade/data/items"))
        line = item["base"]
        best = ""
        for g in data["result"]:
            for e in g["entries"]:
                t = e.get("type", "")
                if not e.get("name") and t in line and len(t) > len(best):
                    best = t
        return best or line

    def trade_search(self, result: dict, opts: dict) -> dict:
        league = result["league"]
        body = self.build_query(result, opts)
        res = _trade(f"/api/trade/search/{urllib.parse.quote(league)}", body)
        qid, hashes = res.get("id"), res.get("result", [])
        trade = {"total": res.get("total", 0), "id": qid, "listings": [],
                 "url": f"{TRADE}/trade/search/{urllib.parse.quote(league)}/{qid}" if qid else None}
        fetched_all = []
        for start in range(0, min(len(hashes), int(opts.get("fetch", 10))), 10):
            fetched_all += _trade(f"/api/trade/fetch/{','.join(hashes[start:start + 10])}?query={qid}").get("result", [])
        return self.finish_trade(result, trade, fetched_all)

    def finish_trade(self, result: dict, trade: dict, fetched_all: list) -> dict:
        """Turn fetched trade results into result["trade"] listings (shared with the APT engine path)."""
        league = result["league"]
        if fetched_all:
            for r in fetched_all:
                if not r:
                    continue
                price = (r.get("listing") or {}).get("price") or {}
                amount, cur = price.get("amount"), price.get("currency")
                if amount is None:
                    continue
                trade["listings"].append({
                    "amount": amount, "currency": cur, "chaos": self.to_chaos(league, amount, cur),
                    "account": ((r.get("listing") or {}).get("account") or {}).get("name"),
                    "indexed": (r.get("listing") or {}).get("indexed"),
                    "ilvl": (r.get("item") or {}).get("ilvl"),
                    "item": item_card(r.get("item") or {}),
                    "corrupted": bool((r.get("item") or {}).get("corrupted")),
                })
        chaos = [l["chaos"] for l in trade["listings"] if l["chaos"] is not None]
        if chaos:
            trade["min"] = min(chaos)
            # median of the cheapest 5 ignores lone price-fixing bait at the very bottom less than min does
            trade["median"] = statistics.median(sorted(chaos)[:5])
        result["trade"] = trade
        result.pop("error", None)
        result["summary"] = summarize(result)
        return result


_FRAME = {0: "Normal", 1: "Magic", 2: "Rare", 3: "Unique", 4: "Gem", 5: "Currency", 6: "Divination Card",
          9: "Relic"}
_MARKUP = re.compile(r"\[([^\]|]+)\|([^\]]+)\]|\[([^\]]+)\]")


def _clean(s: str) -> str:
    """Strip the trade API's link markup: "[Resistances|Resistance]" -> "Resistance"."""
    return _MARKUP.sub(lambda m: m.group(2) or m.group(3), s or "")


def _prop(p: dict) -> str:
    name = _clean(p.get("name", ""))
    vals = [_clean(v[0]) for v in p.get("values") or []]
    if p.get("displayMode") == 3:
        for i, v in enumerate(vals):
            name = name.replace("{%d}" % i, v)
        return name
    if not vals:
        return name
    if p.get("displayMode") == 1:
        return f"{' '.join(vals)} {name}"
    return f"{name}: {', '.join(vals)}"


def item_card(it: dict) -> dict:
    """What a listed item looks like, compact enough to send to the overlay menu (item view)."""
    if not it:
        return {}
    sockets = ""
    last = None
    for sk in it.get("sockets") or []:
        if last is not None:
            sockets += "-" if sk.get("group") == last else " "
        sockets += sk.get("sColour", "?")
        last = sk.get("group")
    reqs = []
    for r in it.get("requirements") or []:
        vals = [v[0] for v in r.get("values") or []]
        reqs.append(f"{_clean(r.get('name', ''))} {' '.join(vals)}".strip())

    def mods(key):
        out = []
        for m in it.get(key) or []:
            if isinstance(m, str):
                out.append({"text": _clean(m)})
                continue
            info = (m.get("mods") or [{}])[0]
            rng = " ".join(f"{g.get('min')}–{g.get('max')}" for g in info.get("magnitudes") or []
                           if g.get("min") is not None and g.get("min") != g.get("max"))
            out.append({"text": _clean(m.get("description", "")), "tier": info.get("tier") or "",
                        "range": rng})
        return out

    return {
        "rarity": _FRAME.get(it.get("frameType"), it.get("rarity", "")),
        "name": _clean(it.get("name", "")), "base": _clean(it.get("typeLine", "")),
        "ilvl": it.get("ilvl"), "corrupted": bool(it.get("corrupted")), "identified": it.get("identified", True),
        "properties": [_prop(p) for p in it.get("properties") or []],
        "requirements": reqs, "sockets": sockets.strip(),
        "enchant": mods("enchantMods"), "implicit": mods("implicitMods"),
        "fractured": mods("fracturedMods"), "explicit": mods("explicitMods"), "crafted": mods("craftedMods"),
        "influences": sorted(k.title() for k, v in (it.get("influences") or {}).items() if v),
        "note": it.get("note") or "",
        # icon on PoE's CDN and the item's size in inventory cells (an icon is 47 px per cell)
        "icon": it.get("icon") if str(it.get("icon", "")).startswith("https://web.poecdn.com/") else None,
        "w": it.get("w") or 1, "h": it.get("h") or 1,
    }


def default_min(value: float, factor: float) -> float:
    """Search minimum for an affix roll: factor x the roll, rounded down.
    Whole-number affixes keep whole-number minimums, and never drop to 0 (or below the roll's sign):
    "+1 to Level of all Minion Skill Gems" searches 1, not 0.8; "+11% to Item Rarity" searches 9."""
    lo = value * factor
    if float(value).is_integer():
        lo = math.floor(lo) if value > 0 else math.ceil(lo)
        if value > 0 and lo < 1:
            lo = value
        return int(lo)
    lo = math.floor(lo * 10) / 10 if abs(lo) < 10 else math.floor(lo)
    return lo if lo != 0 else value


def friendly_error(e: Exception) -> str:
    """Short, readable text for errors shown to the user."""
    if isinstance(e, TradeError):
        return str(e)
    if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
        reason = str(getattr(e, "reason", e))  # offline, DNS failure, timeout
        if "name resolution" in reason or "unreachable" in reason:
            return "No internet connection"
        return f"Can't reach the price sites ({reason})"
    return f"{type(e).__name__}: {e}"


# ------------------------------------------------------------------------------------------- Display

def fmt_chaos(chaos: float | None, divine: float) -> str:
    if chaos is None:
        return "?"
    if divine and chaos >= divine * 0.8:
        d = chaos / divine
        return f"{d:.1f} div" if d < 100 else f"{d:.0f} div"
    if chaos >= 10:
        return f"{chaos:.0f}c"
    if chaos < 1:
        return f"{chaos:.2f}c"
    return f"{chaos:.1f}c".replace(".0c", "c")


def summarize(result: dict) -> str:
    div = result.get("divine") or 0
    n = result.get("ninja")
    t = result.get("trade")
    if n:
        s = fmt_chaos(n["total"], div)
        if n.get("stack", 1) > 1:
            s += f" ({fmt_chaos(n['chaos'], div)} each)"
        return s
    if t:
        if not t.get("listings"):
            return "No listings found"
        lo, med = t.get("min"), t.get("median")
        if lo is None:
            return f"{t['total']} listed"

        def price(chaos):
            # below 1c show what the seller actually asks ("1 alch"), not "0.02c"
            if chaos is not None and chaos < 1:
                l = next((l for l in t["listings"] if l.get("chaos") == chaos), None)
                if l and l.get("currency") != "chaos":
                    return f"{l['amount']:g} {l['currency']}"
            return fmt_chaos(chaos, div)

        if med and med > lo * 1.05:
            return f"{price(lo)} – {price(med)}  ({t['total']} listed)"
        return f"{price(lo)}  ({t['total']} listed)"
    return result.get("error") or "No price found"


if __name__ == "__main__":
    import sys
    text = open(sys.argv[1]).read()
    p = Pricer(None)
    league = sys.argv[2] if len(sys.argv) > 2 else p.leagues()[0]
    r = p.check(text, league)
    print(json.dumps(r, indent=1, default=str)[:4000] if r else "not an item")
