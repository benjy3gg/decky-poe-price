// Bundle Awakened PoE Trade's price-check engine (MIT, https://github.com/SnosMe/awakened-poe-trade)
// into dist/engine.mjs and copy its English data with lookup indexes into dist/data.
// Usage: ./fetch-apt.sh && node build.mjs   (or APT_DIR=/path/to/awakened-poe-trade node build.mjs)
import * as esbuild from 'esbuild'
import fnv1a from '@sindresorhus/fnv1a'
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'
import { createRequire } from 'module'
import { execFileSync } from 'child_process'

const here = path.dirname(fileURLToPath(import.meta.url))
const require_ = createRequire(import.meta.url)
const APT = process.env.APT_DIR || path.join(here, 'vendor/awakened-poe-trade')
if (!fs.existsSync(path.join(APT, 'renderer/src'))) throw new Error('no APT sources: run ./fetch-apt.sh (or set APT_DIR)')
const SRC = path.join(APT, 'renderer/src')
const DATA = path.join(APT, 'renderer/public/data')
const OUT = process.env.ENGINE_OUT || path.join(here, 'dist')

const shims = {
  '@/web/Config': path.join(here, 'shims/Config.ts'),
  '@/web/background/IPC': path.join(here, 'shims/IPC.ts'),
  '@/web/background/Leagues': path.join(here, 'shims/Leagues.ts')
}


const aliasPlugin = {
  name: 'apt-alias',
  setup (build) {
    // relative imports of the replaced app modules ('../../background/Leagues')
    build.onResolve({ filter: /^\.\.?\// }, args => {
      if (!args.resolveDir.startsWith(SRC)) return undefined
      const rel = '@/' + path.relative(SRC, path.resolve(args.resolveDir, args.path)).replace(/\.ts$/, '')
      if (shims[rel]) return { path: shims[rel] }
      return undefined
    })
    // the engine only needs Vue's reactivity (RateLimiter, trade/common): skip the rest of Vue
    build.onResolve({ filter: /^vue$/ }, () => ({ path: require_.resolve('@vue/reactivity') }))
    build.onResolve({ filter: /^@\// }, args => {
      if (shims[args.path]) return { path: shims[args.path] }
      const base = path.join(SRC, args.path.slice(2))
      for (const p of [base + '.ts', path.join(base, 'index.ts'), base]) {
        if (fs.existsSync(p) && fs.statSync(p).isFile()) return { path: p }
      }
      return { errors: [{ text: `cannot resolve ${args.path}` }] }
    })
  }
}

if (!process.env.ENTRY) fs.rmSync(OUT, { recursive: true, force: true })
await esbuild.build({
  entryPoints: [path.join(here, process.env.ENTRY || 'src/main.ts')],
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node20',
  outfile: path.join(OUT, process.env.ENTRY ? 'debug.mjs' : 'engine.mjs'),
  plugins: [aliasPlugin],
  nodePaths: [path.join(here, 'node_modules')],
  define: { 'import.meta.env.BASE_URL': 'globalThis.__APT_BASE_URL' },
  banner: { js: "// Awakened PoE Trade engine, MIT License, Copyright (c) Alexander Drozdov (SnosMe)\nimport { createRequire as __cr } from 'module'; const require = __cr(import.meta.url);" },
  logLevel: 'warning'
})

// data: en only, plus the binary indexes APT's vite plugin makes at build time
fs.mkdirSync(path.join(OUT, 'data/en'), { recursive: true })
for (const f of ['item-drop.json', 'patrons.json']) fs.copyFileSync(path.join(DATA, f), path.join(OUT, 'data', f))
for (const f of ['client_strings.js', 'items.ndjson', 'stats.ndjson', 'app_i18n.json']) {
  fs.copyFileSync(path.join(DATA, 'en', f), path.join(OUT, 'data/en', f))
}
fs.copyFileSync(path.join(APT, 'LICENSE'), path.join(OUT, 'LICENSE-awakened-poe-trade'))

const hash = s => Number(fnv1a(s, { size: 32 }))
function writeIndex (file, rows, key) {
  rows.sort((a, b) => a[key] - b[key])
  const data = new Uint32Array(rows.length * 2)
  rows.forEach((r, i) => { data[i * 2] = r[key]; data[i * 2 + 1] = r.start })
  fs.writeFileSync(path.join(OUT, 'data/en', file), data)
}
{
  const ndjson = fs.readFileSync(path.join(DATA, 'en/stats.ndjson'), 'utf-8')
  const byRef = []
  const matchers = []
  for (let start = 0; start !== ndjson.length;) {
    const end = ndjson.indexOf('\n', start)
    const g = JSON.parse(ndjson.slice(start, end))
    for (const stat of ('stats' in g ? g.stats : [g])) {
      byRef.push({ start, h: hash(stat.ref) })
      for (const m of stat.matchers) matchers.push({ start, h: hash(m.advanced || m.string) })
    }
    start = end + 1
  }
  writeIndex('stats-ref.index.bin', byRef, 'h')
  writeIndex('stats-matcher.index.bin', matchers, 'h')
}
{
  const ndjson = fs.readFileSync(path.join(DATA, 'en/items.ndjson'), 'utf-8')
  const seen = new Map()
  for (let start = 0; start !== ndjson.length;) {
    const end = ndjson.indexOf('\n', start)
    const it = JSON.parse(ndjson.slice(start, end))
    const key = `${it.namespace}::${it.refName}`
    if (!seen.has(key)) {
      seen.set(key, { start, hn: hash(`${it.namespace}::${it.name}`), hr: hash(key) })
    }
    start = end + 1
  }
  const rows = [...seen.values()]
  writeIndex('items-name.index.bin', rows.map(r => ({ ...r })), 'hn')
  writeIndex('items-ref.index.bin', rows.map(r => ({ ...r })), 'hr')
}
// which APT this engine is: the release tag (APT_VERSION, or the checkout's exact tag) or its commit
function aptVersion () {
  if (process.env.APT_VERSION) return process.env.APT_VERSION
  for (const args of [['describe', '--tags', '--exact-match'], ['rev-parse', '--short', 'HEAD']]) {
    try { return execFileSync('git', ['-C', APT, ...args], { stdio: ['ignore', 'pipe', 'ignore'] }).toString().trim() } catch {}
  }
  return 'unknown'
}
fs.writeFileSync(path.join(OUT, 'version.json'),
  JSON.stringify({ apt: aptVersion(), built: new Date().toISOString() }, null, 1) + '\n')

console.log('built', path.join(OUT, 'engine.mjs'), 'from APT', aptVersion(),
  (fs.statSync(path.join(OUT, 'engine.mjs')).size / 1024).toFixed(0) + ' KiB')
