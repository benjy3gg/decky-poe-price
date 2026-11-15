import { useEffect, useState } from "react";
import {
  ButtonItem,
  DropdownItem,
  Field,
  Navigation,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import { FaCoins } from "react-icons/fa";

// ---------------------------------------------------------------------------------------------- types

interface Mod {
  text: string;
  kind: string;
  id: string;
  value: number | null;
  enabled: boolean;
  min?: number | null;
}
interface Listing {
  amount: number;
  currency: string;
  chaos: number | null;
  account?: string;
}
interface Result {
  key: string;
  league: string;
  item: { name: string; base: string; rarity?: string; class?: string; ilvl?: number; links?: number; corrupted?: boolean };
  summary: string;
  divine?: number;
  error?: string;
  mods: Mod[];
  ninja?: { chaos: number; total: number; source: string; variant?: string; listings?: number; variants?: { label: string; chaos: number }[] };
  trade?: { total: number; url?: string; listings: Listing[]; min?: number; median?: number };
}
interface Settings {
  enabled: boolean;
  league: string;
  toasts: boolean;
  instant_buyout: boolean;
  mod_factor: number;
  use_ilvl: boolean;
  popup: "off" | "rares" | "always";
  overlay: boolean;
  overlay_seconds: number;
  rare_menu: boolean;
}
interface EngineInfo {
  version: string | null;
  built: string | null;
  source: "bundled" | "updated";
  available: boolean;
  node: string | null;
  node_bundled: boolean;
  can_update: boolean;
  update_available: boolean;
  checking: boolean;
  updating: boolean;
  progress: string;
  error: string | null;
  latest: { tag: string; published?: string; url?: string } | null;
}
interface State {
  engine: EngineInfo;
  settings: Settings;
  status: { game: number | null; display: string | null; watching: boolean; error: string | null; busy: boolean };
  history: Result[];
  leagues: string[];
  league: string;
}

const getState = callable<[], State>("get_state");
const setSettings = callable<[Partial<Settings>], Settings>("set_settings");
const research = callable<[string, Mod[], number | null], Result | null>("research");
const tradeLookup = callable<[string], Result | null>("trade_lookup");
const clearHistory = callable<[], void>("clear_history");
const checkEngineUpdate = callable<[], EngineInfo>("check_engine_update");
const updateEngine = callable<[], EngineInfo>("update_engine");
interface OverlayTest {
  running: boolean;
  step: number;
  text: string;
  results: string[];
}
const startOverlayTest = callable<[], OverlayTest>("start_overlay_test");
const stopOverlayTest = callable<[], OverlayTest | null>("stop_overlay_test");
const getOverlayTest = callable<[], OverlayTest>("get_overlay_test");

function DebugOverlay() {
  const [test, setTest] = useState<OverlayTest | null>(null);
  useEffect(() => {
    const poll = () => getOverlayTest().then(setTest).catch(() => {});
    poll();
    const t = setInterval(poll, 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <PanelSection title="Debug">
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          description="Shows 4 overlay variants top-left, 6 s each, numbered 1-4. Starts after 5 s: close this menu and watch the game."
          onClick={() => startOverlayTest().then(setTest)}
        >
          {test?.running ? "Restart overlay test" : "Run overlay test"}
        </ButtonItem>
      </PanelSectionRow>
      {test?.running && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => stopOverlayTest().then((t) => t && setTest(t))}>
            Stop test
          </ButtonItem>
        </PanelSectionRow>
      )}
      {test && test.text && (
        <PanelSectionRow>
          <div style={{ fontSize: "12px", opacity: 0.85 }}>
            {test.text}
            {test.results.map((r) => (
              <div key={r}>{r}</div>
            ))}
          </div>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}

// ------------------------------------------------------------------------------------------- helpers

const RARITY_COLOR: Record<string, string> = {
  Unique: "#af6025",
  Rare: "#ffff77",
  Magic: "#8888ff",
  Gem: "#1ba29b",
  Currency: "#aa9e82",
  "Divination Card": "#c0e0ff",
};

function title(r: Result) {
  return r.item.name || r.item.base;
}

function fmtChaos(chaos: number | null | undefined, divine?: number) {
  if (chaos == null) return "?";
  if (divine && chaos >= divine * 0.8) return `${(chaos / divine).toFixed(chaos / divine < 100 ? 1 : 0)} div`;
  if (chaos >= 10) return `${Math.round(chaos)}c`;
  if (chaos < 1) return `${chaos.toFixed(2)}c`;
  return `${chaos.toFixed(1).replace(/\.0$/, "")}c`;
}

const CURRENCY_SHORT: Record<string, string> = { chaos: "c", divine: " div", exalted: " ex", alch: " alch" };

// Event listeners outlive the panel: toasts must appear while the game is in front and the QAM is closed.
const resultListeners = new Set<(r: Result) => void>();
const PLUGIN_NAME = "PoE Price Check";

/** Open the Quick Access Menu straight on this plugin's panel (the only UI drawn over a game in Game Mode). */
function openPanel() {
  try {
    // Decky keeps the selected plugin in a private DeckyState; there is no public API for it.
    (window as any).DeckyPluginLoader?.deckyState?.setActivePlugin(PLUGIN_NAME);
  } catch (e) {
    console.warn("[poe-price] could not select plugin", e);
  }
  Navigation.OpenQuickAccessMenu(999 as any); // EQuickAccessTab.Decky
}

function onPrice(result: Result, showToast: boolean, popup: Settings["popup"] = "rares") {
  resultListeners.forEach((l) => l(result));
  const rare = result.item.rarity === "Rare" || result.item.rarity === "Magic";
  if (popup === "always" || (popup === "rares" && rare)) {
    openPanel();
    return; // the panel shows the price, no toast needed
  }
  if (!showToast) return;
  const color = RARITY_COLOR[result.item.rarity ?? ""] ?? "#ffffff";
  toaster.toast({
    title: <span style={{ color }}>{title(result)}</span>,
    body: <b>{result.summary}</b>,
    icon: <FaCoins />,
    duration: 8000,
    playSound: false,
  });
}

// --------------------------------------------------------------------------------------------- panel

const small = { fontSize: "12px", opacity: 0.8 } as const;

function ResultView({ result, onUpdate }: { result: Result; onUpdate: (r: Result) => void }) {
  const [mods, setMods] = useState<Mod[]>(result.mods ?? []);
  const enabledCount = mods.filter((m) => m.enabled).length;
  const [required, setRequired] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    setMods(result.mods ?? []);
    setRequired(null);
    setEditing(false);
  }, [result.key, result.mods]);

  const run = async (fn: () => Promise<Result | null>) => {
    setBusy(true);
    try {
      const r = await fn();
      if (r) onUpdate({ ...r });
    } finally {
      setBusy(false);
    }
  };
  const div = result.divine;
  const color = RARITY_COLOR[result.item.rarity ?? ""] ?? undefined;
  const defaultRequired = enabledCount <= 2 ? enabledCount : enabledCount <= 4 ? enabledCount - 1 : enabledCount - 2;
  const details = [
    result.item.name ? result.item.base : result.item.class,
    result.item.ilvl ? `ilvl ${result.item.ilvl}` : "",
    result.item.links && result.item.links >= 5 ? `${result.item.links}L` : "",
    result.item.corrupted ? "corrupted" : "",
  ].filter(Boolean).join(" · ");
  const listings = result.trade?.listings ?? [];

  return (
    <>
      <PanelSectionRow>
        <div style={{ padding: "2px 0" }}>
          <div style={{ color, fontWeight: "bold", fontSize: "15px" }}>{title(result)}</div>
          <div style={small}>{details}</div>
          <div style={{ fontSize: "22px", fontWeight: "bold", marginTop: "6px" }}>{busy ? "Searching…" : result.summary}</div>
          {result.error && <div style={{ color: "#ff7070", fontSize: "12px" }}>{result.error}</div>}
          {result.ninja && (
            <div style={small}>
              {result.ninja.source}
              {result.ninja.variant ? ` · ${result.ninja.variant}` : ""}
              {result.ninja.listings != null ? ` · ${result.ninja.listings} listed` : ""}
            </div>
          )}
          {listings.length > 0 && (
            <div style={{ fontSize: "12px", marginTop: "6px" }}>
              {listings.slice(0, 5).map((l, i) => (
                <div key={i} style={{ display: "flex", justifyContent: "space-between" }}>
                  <span>
                    {l.amount}
                    {CURRENCY_SHORT[l.currency] ?? ` ${l.currency}`}
                    {l.currency !== "chaos" && l.chaos != null ? ` (${fmtChaos(l.chaos, div)})` : ""}
                  </span>
                  <span style={{ opacity: 0.6, overflow: "hidden", textOverflow: "ellipsis", maxWidth: "55%" }}>{l.account}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </PanelSectionRow>
      {result.trade?.url && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => Navigation.NavigateToExternalWeb(result.trade!.url!)}>
            Open on trade site
          </ButtonItem>
        </PanelSectionRow>
      )}
      {result.ninja && !result.trade && (
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => run(() => tradeLookup(result.key))}>
            Show live trade listings
          </ButtonItem>
        </PanelSectionRow>
      )}
      {mods.length > 0 && (
        <PanelSectionRow>
          <ToggleField label="Edit affixes here" checked={editing} onChange={setEditing} />
        </PanelSectionRow>
      )}
      {editing &&
        mods.map((m, i) => (
          <PanelSectionRow key={`${m.id}-${i}`}>
            <ToggleField
              label={<span style={{ fontSize: "13px" }}>{m.text}</span>}
              description={m.kind !== "explicit" ? m.kind : undefined}
              checked={m.enabled}
              onChange={(v) => setMods(mods.map((x, j) => (j === i ? { ...x, enabled: v } : x)))}
            />
          </PanelSectionRow>
        ))}
      {editing && enabledCount > 1 && (
        <PanelSectionRow>
          <SliderField
            label="Affixes that must match"
            value={required ?? defaultRequired}
            min={1}
            max={enabledCount}
            step={1}
            showValue
            onChange={(v) => setRequired(v)}
          />
        </PanelSectionRow>
      )}
      {editing && (
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={() => run(() => research(result.key, mods, required))}>
            Search again
          </ButtonItem>
        </PanelSectionRow>
      )}
    </>
  );
}

function EngineSection({ engine, refresh }: { engine: EngineInfo; refresh: () => void }) {
  const [busy, setBusy] = useState(false);
  const check = () => {
    setBusy(true);
    checkEngineUpdate().finally(() => {
      setBusy(false);
      refresh();
    });
  };
  const version = engine.version ? `APT ${engine.version}` : "not installed";
  const source = engine.source === "updated" ? "updated on this Deck" : "bundled with the plugin";
  let node = "Node.js missing: using the built-in search";
  if (engine.node) node = engine.node_bundled ? "Node.js bundled" : `Node.js: ${engine.node}`;
  const latest = engine.latest;

  return (
    <PanelSection title="Price engine">
      <PanelSectionRow>
        <Field label="Awakened PoE Trade" description={`${version} · ${source}`} bottomSeparator="none">
          <span style={{ color: engine.available ? "#7cd67c" : "#ff7070", fontSize: "12px" }}>
            {engine.available ? "active" : "off"}
          </span>
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={small}>
          {node}
          {latest && (
            <div>
              Latest release: {latest.tag}
              {latest.published ? ` (${latest.published.slice(0, 10)})` : ""}
              {engine.update_available ? "" : " · up to date"}
            </div>
          )}
          {engine.progress && <div>{engine.progress}</div>}
          {engine.error && <div style={{ color: "#ff7070" }}>{engine.error}</div>}
        </div>
      </PanelSectionRow>
      {engine.update_available && engine.can_update ? (
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={engine.updating} onClick={() => updateEngine().then(refresh)}>
            {engine.updating ? "Updating…" : `Update to ${latest!.tag}`}
          </ButtonItem>
        </PanelSectionRow>
      ) : (
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy || engine.checking || engine.updating} onClick={check}>
            {busy || engine.checking ? "Checking…" : "Check for APT update"}
          </ButtonItem>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [override, setOverride] = useState<Record<string, Result>>({});
  const [allHistory, setAllHistory] = useState(false);
  const [help, setHelp] = useState(false);

  const refresh = () => getState().then(setState).catch(() => {});
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 2000);
    const l = (r: Result) => {
      setSelected(r.key);
      refresh();
    };
    resultListeners.add(l);
    return () => {
      clearInterval(t);
      resultListeners.delete(l);
    };
  }, []);

  if (!state) return <PanelSection title="PoE Price Check">Loading…</PanelSection>;
  const { settings, status, history } = state;
  const base = history.find((h) => h.key === selected) ?? history[0];
  const current = base ? override[base.key] ?? base : undefined;
  const update = (patch: Partial<Settings>) => setSettings(patch).then(refresh);

  let statusText = "Waiting for Path of Exile";
  let statusColor = "#c8c8c8";
  if (!settings.enabled) {
    statusText = "Paused";
  } else if (status.game && status.watching) {
    statusText = "Watching PoE";
    statusColor = "#7cd67c";
  } else if (status.game) {
    statusText = "Connecting to PoE…";
  }
  const shownHistory = (allHistory ? history : history.slice(0, 4)).filter((h) => h.key !== current?.key);

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px" }}>
            <span>
              <span style={{ color: statusColor }}>●</span> {statusText}
              {status.busy ? " · checking…" : ""}
            </span>
            <span style={{ opacity: 0.7 }}>{state.league}</span>
          </div>
          {status.error && <div style={{ color: "#ff7070", fontSize: "12px" }}>{status.error}</div>}
        </PanelSectionRow>
        {current ? (
          <ResultView result={current} onUpdate={(r) => setOverride({ ...override, [r.key]: r })} />
        ) : (
          <PanelSectionRow>
            <div style={{ fontSize: "13px" }}>
              Hover an item in PoE and press <b>Ctrl+Alt+C</b> (bind it to a back button: see Help below).
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      {history.length > 1 && (
        <PanelSection title="Recent">
          {shownHistory.map((h) => (
            <PanelSectionRow key={h.key}>
              <Field
                label={<span style={{ color: RARITY_COLOR[h.item.rarity ?? ""] }}>{title(h)}</span>}
                onActivate={() => setSelected(h.key)}
                onClick={() => setSelected(h.key)}
                highlightOnFocus
                focusable
                bottomSeparator="none"
              >
                <span style={{ fontSize: "12px" }}>{(override[h.key] ?? h).summary}</span>
              </Field>
            </PanelSectionRow>
          ))}
          <PanelSectionRow>
            <div style={{ display: "flex", gap: "8px" }}>
              {history.length > 5 && (
                <ButtonItem layout="below" onClick={() => setAllHistory(!allHistory)}>
                  {allHistory ? "Show fewer" : `Show all (${history.length})`}
                </ButtonItem>
              )}
              <ButtonItem layout="below" onClick={() => clearHistory().then(refresh)}>
                Clear
              </ButtonItem>
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="Over the game">
        <PanelSectionRow>
          <ToggleField label="Price check copied items" checked={settings.enabled} onChange={(v) => update({ enabled: v })} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Price-check menu"
            description="Rares, magic and uniques: filters, affix minimums, listings"
            checked={settings.rare_menu}
            onChange={(v) => update({ rare_menu: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Price box"
            description="Currency, gems, cards and other items"
            checked={settings.overlay}
            onChange={(v) => update({ overlay: v })}
          />
        </PanelSectionRow>
        {settings.overlay && (
          <PanelSectionRow>
            <SliderField
              label="Price box stays for"
              value={settings.overlay_seconds}
              min={3}
              max={20}
              step={1}
              showValue
              valueSuffix=" s"
              onChange={(v) => update({ overlay_seconds: v })}
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Trade search">
        <PanelSectionRow>
          <DropdownItem
            label="League"
            rgOptions={[{ data: "", label: `Current (${state.leagues[0] ?? "auto"})` }, ...state.leagues.map((l) => ({ data: l, label: l }))]}
            selectedOption={settings.league}
            onChange={(o) => update({ league: o.data })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Instant buyout only"
            description="Listings you can buy without whispering"
            checked={settings.instant_buyout}
            onChange={(v) => update({ instant_buyout: v })}
          />
        </PanelSectionRow>
        {!state.engine.available && (
          <>
            <PanelSectionRow>
              <SliderField
                label="Affix rolls at least"
                description="Built-in search only (APT sets its own minimums)"
                value={Math.round(settings.mod_factor * 100)}
                min={50}
                max={100}
                step={5}
                showValue
                valueSuffix="%"
                onChange={(v) => update({ mod_factor: v / 100 })}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField label="Match item level (rares)" checked={settings.use_ilvl} onChange={(v) => update({ use_ilvl: v })} />
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      <PanelSection title="Notifications">
        <PanelSectionRow>
          <ToggleField
            label="Price notifications"
            description="Only when nothing was shown over the game"
            checked={settings.toasts}
            onChange={(v) => update({ toasts: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Open this panel on price check"
            rgOptions={[
              { data: "off", label: "Never" },
              { data: "rares", label: "Rare and magic items" },
              { data: "always", label: "Every item" },
            ]}
            selectedOption={settings.popup}
            onChange={(o) => update({ popup: o.data })}
          />
        </PanelSectionRow>
      </PanelSection>

      <EngineSection engine={state.engine} refresh={refresh} />

      <PanelSection title="Help">
        <PanelSectionRow>
          <ToggleField label="Show setup and debug" checked={help} onChange={setHelp} />
        </PanelSectionRow>
        {help && (
          <PanelSectionRow>
            <div style={{ fontSize: "12px", lineHeight: 1.4 }}>
              Path of Exile → Controller settings → Edit layout → pick a back button (e.g. L4) → Keyboard key{" "}
              <b>Ctrl</b>, <b>Alt</b> and <b>C</b> as three commands on the same button, and give the C command a
              Fire Start Delay of about 50 ms (gear icon next to it) so Ctrl and Alt are already held when C arrives.
              Hover an item with the trackpad cursor and press the button. The plugin only reads what PoE copies; it
              never sends input to the game.
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>
      {help && <DebugOverlay />}
    </>
  );
}

export default definePlugin(() => {
  const listener = addEventListener<[Result, boolean, Settings["popup"]]>("poe_price", onPrice);
  return {
    name: PLUGIN_NAME,
    titleView: <div>PoE Price Check</div>,
    content: <Content />,
    icon: <FaCoins />,
    onDismount() {
      removeEventListener("poe_price", listener);
    },
  };
});
