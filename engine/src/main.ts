// Awakened PoE Trade's price-check engine without its UI, for the Decky plugin.
// Talks JSON lines over stdin/stdout:
//   {"id", "cmd": "check", "text", "league"}            -> {"id", "item", "presets", "active"}
//   {"id", "cmd": "query", "filters", "stats"}          -> {"id", "body"}           (no network)
//   {"id", "cmd": "search", "filters", "stats", "league", "fetch": 20, "required": n|null}
//                                                       -> {"id", "total", "queryId", "body", "results"}
//   results are the trade site's raw fetch results (listing + item).
// Errors: {"id", "error"}. First line out: {"ready": true}.
// It only reads the item text it is given; it never touches the keyboard or the game.
// CLI test: node engine.mjs check ITEM.txt [LEAGUE] [--search]
import fs from 'fs'
import path from 'path'
import readline from 'readline'
import { fileURLToPath, pathToFileURL } from 'url'
import { init } from '@/assets/data'
import { parseClipboard } from '@/parser'
import { createPresets } from '@/web/price-check/filters/create-presets'
import { createTradeRequest, requestTradeResultList } from '@/web/price-check/trade/pathofexile-trade'
import { RATE_LIMIT_RULES, adjustRateLimits, getTradeEndpoint } from '@/web/price-check/trade/common'
import { RateLimiter } from '@/web/price-check/trade/RateLimiter'
import { Host } from '@/web/background/IPC'
import { ENGINE_CONFIG } from '../shims/Config'

const here = path.dirname(fileURLToPath(import.meta.url))
;(globalThis as any).__APT_BASE_URL = pathToFileURL(here).href + '/'

// APT loads its data with fetch(); serve file: URLs from disk
const netFetch = globalThis.fetch
globalThis.fetch = (async (input: any, initArg?: any) => {
  const url = typeof input === 'string' ? input : input.url ?? String(input)
  if (url.startsWith('file:')) {
    return new Response(fs.readFileSync(fileURLToPath(url)))
  }
  return await netFetch(input, initArg)
}) as typeof fetch

function check (text: string, league: string) {
  const parsed = parseClipboard(text)
  if (parsed.isErr()) throw new Error(parsed.error)
  const item = parsed.value
  const pc = ENGINE_CONFIG.priceCheck
  const { presets, active } = createPresets(item, {
    league,
    merchantOnly: pc.merchantOnly,
    collapseListings: pc.collapseListings as any,
    activateStockFilter: pc.activateStockFilter,
    searchStatRange: pc.searchStatRange,
    useEn: true,
    currency: pc.defaultCurrency ?? undefined
  } as any)
  return {
    item: {
      name: item.info.name,
      refName: item.info.refName,
      rarity: item.rarity,
      category: item.category,
      itemLevel: item.itemLevel,
      tradeTag: item.info.tradeTag ?? null,
      unknown: item.unknownModifiers.map(m => m.text)
    },
    presets,
    active
  }
}

// The trade site's raw fetch results (the whole item, for the plugin's item view), with APT's rate limiting.
async function fetchRaw (queryId: string, ids: string[]) {
  await RateLimiter.waitMulti(RATE_LIMIT_RULES.FETCH)
  const response = await Host.proxy(`${getTradeEndpoint()}/api/trade/fetch/${ids.join(',')}?query=${queryId}`)
  adjustRateLimits(RATE_LIMIT_RULES.FETCH, response.headers)
  const data = await response.json() as any
  if (data.error) throw new Error(data.error.message)
  return (data.result as any[]).filter(r => r != null)
}

// required: how many of the enabled stat filters must match (APT always needs all of them; the plugin's
// "match n/m" lets fewer count). Influence filters stay required either way.
function applyRequired (body: any, required?: number) {
  if (required == null) return
  const and = body.query.stats[0]
  const isInfluence = (f: any) => String(f.id).startsWith('pseudo.pseudo_has_') && String(f.id).endsWith('_influence')
  const counted = and.filters.filter((f: any) => !f.disabled && !isInfluence(f))
  if (required >= counted.length || required < 1) return
  and.filters = and.filters.filter((f: any) => isInfluence(f))
  body.query.stats.splice(1, 0, { type: 'count', value: { min: required }, filters: counted })
}

async function search (filters: any, stats: any, league: string, fetchN = 20, required?: number) {
  filters.trade.league = league
  const body = createTradeRequest(filters, stats)
  applyRequired(body, required)
  const res = await requestTradeResultList(body, league)
  const results = []
  for (let i = 0; i < Math.min(fetchN, res.result.length); i += 10) {
    results.push(...await fetchRaw(res.id, res.result.slice(i, i + 10)))
  }
  return { total: res.total, inexact: res.inexact ?? false, queryId: res.id, body, results }
}

async function handle (msg: any) {
  switch (msg.cmd) {
    case 'check': return check(msg.text, msg.league)
    case 'query': return { body: createTradeRequest(msg.filters, msg.stats) }
    case 'search': return await search(msg.filters, msg.stats, msg.league, msg.fetch, msg.required)
    default: throw new Error(`unknown cmd ${msg.cmd}`)
  }
}

await init('en')

const argv = process.argv.slice(2)
if (argv[0] === 'check') {
  const league = argv[2] && !argv[2].startsWith('--') ? argv[2] : 'Standard'
  const out: any = check(fs.readFileSync(argv[1], 'utf-8'), league)
  if (argv.includes('--search')) {
    const p = out.presets.find((x: any) => x.id === out.active)
    out.search = await search(p.filters, p.stats, league)
  }
  console.log(JSON.stringify(out, null, 1))
  process.exit(0) // APT's rate-limit timers would keep Node alive
} else {
  console.log(JSON.stringify({ ready: true }))
  const rl = readline.createInterface({ input: process.stdin })
  rl.on('line', (line) => {
    let msg: any
    try { msg = JSON.parse(line) } catch { return }
    handle(msg).then(
      (r) => console.log(JSON.stringify({ id: msg.id, ...r })),
      (e) => console.log(JSON.stringify({ id: msg.id, error: String(e?.message ?? e) }))
    )
  })
}
