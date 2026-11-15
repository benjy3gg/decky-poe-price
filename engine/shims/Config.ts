// Stand-in for APT's renderer/src/web/Config.ts: the price-check settings the engine reads.
export const ENGINE_CONFIG = {
  language: 'en',
  useIntlSite: false,
  priceCheck: {
    apiLatencySeconds: 2,
    searchStatRange: 10,
    merchantOnly: false,
    collapseListings: 'app',
    activateStockFilter: false,
    defaultCurrency: null as string | null,
    smartInitialSearch: true,
    lockedInitialSearch: true
  }
}

export function AppConfig (type?: string): any {
  if (type === 'price-check') return ENGINE_CONFIG.priceCheck
  return ENGINE_CONFIG
}

export function poeWebApi () {
  return 'www.pathofexile.com'
}
