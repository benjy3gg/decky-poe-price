// Stand-in for APT's renderer/src/web/background/IPC.ts. APT's main process proxies trade requests;
// here they go straight out with our own User-Agent.
export const USER_AGENT = 'decky-poe-price-engine/0.1 (Steam Deck Decky Loader plugin)'

export const Host = {
  async proxy (url: string, init: RequestInit = {}) {
    const headers = new Headers(init.headers)
    headers.set('User-Agent', USER_AGENT)
    return await fetch(`https://${url}`, { ...init, headers })
  }
}
