# APT engine

[Awakened PoE Trade](https://github.com/SnosMe/awakened-poe-trade)'s price-check engine (MIT) without its UI:
APT's parser, filter presets (`createPresets`) and trade query (`createTradeRequest`), bundled for Node.js
into `dist/engine.mjs` together with APT's English item and stat data. The plugin's GTK menu shows APT's
filters; `py_modules/aptengine.py` maps them to menu rows and chips and back.

```
./fetch-apt.sh      # APT sources into vendor/ (pinned commit, APT_REF=... to change)
pnpm install
node build.mjs      # dist/engine.mjs + dist/data/en (+ the lookup indexes APT's vite build makes)
```
`scripts/build.sh` (and `install.sh`) fetches the sources if needed, builds `dist/`, and also ships a build
kit (`engine/kit`: `build.mjs`, the stand-ins, `src/`, a sample item and a flat `node_modules` with esbuild)
so the plugin can rebuild the engine for a newer APT release on the Deck: `py_modules/aptupdate.py`.
`dist/version.json` records the APT version (`APT_VERSION`, or the checkout's tag).

Vue: the engine uses only Vue's reactivity (rate limiter state), so `vue` is resolved to `@vue/reactivity`.

## What is replaced

Only three of APT's app modules, in `shims/`:
- `web/Config`: the price-check settings (`AppConfig`, `poeWebApi`).
- `web/background/IPC`: `Host.proxy`, which in APT goes through its main process; here it is a direct
  `fetch` with the plugin's User-Agent.
- `web/background/Leagues`: only `PERMANENT_SC`.

Everything else is APT's code unchanged, including its rate limiter and cache. The plugin's additions are
in `src/main.ts`: raw fetch results (for the item view) and `required` (the menu's "match n/m", as a
trade `count` group; APT itself always needs all selected affixes).

## Protocol

JSON lines on stdin/stdout; first line `{"ready": true}`.
- `{"id", "cmd": "check", "text", "league"}` → `{"id", "item", "presets", "active"}`
- `{"id", "cmd": "search", "filters", "stats", "league", "fetch", "required"}` →
  `{"id", "total", "queryId", "body", "results"}`
- `{"id", "cmd": "query", "filters", "stats"}` → `{"id", "body"}`
- errors: `{"id", "error"}`

Test from the shell: `node dist/engine.mjs check ITEM.txt LEAGUE [--search]`.

## Notes

- APT reads affixes only from the advanced copy (`{ Prefix Modifier … }` lines, Ctrl+Alt+C in game).
- The engine never touches input: it only gets the item text main.py read from the clipboard.
- Node.js comes from PATH, `/usr/bin`, or the Deck user's nvm (`aptengine.find_node`). SteamOS has no Node
  by default; without it the plugin falls back to its own search.
- New league: bump `APT_REF` in `fetch-apt.sh`, rebuild, and check the stand-ins still match APT's exports.
