/*
 * Pantry Raider service worker.
 *
 * Conservative on purpose: when the network is up the app behaves exactly as it
 * does with no worker at all. Live data and the login session always win because
 * page navigations and API calls go network-first (never served from a stale
 * cache), and only safe, version-busted static assets are cached for offline use
 * and installability. The dev --reload flow is unaffected: template asset URLs
 * carry ?v=<version>, so an update is a fresh URL and a fresh fetch.
 */

// Bump this string to roll the cache; the activate handler drops every older one.
const CACHE = 'pantryraider-shell-v1';

// App shell + brand assets that are safe to precache. All are static, public,
// and version-busted at their call sites, so caching them never serves stale
// authenticated data. allSettled keeps install from failing if one 404s.
const PRECACHE = [
  '/static/vendor/bootstrap.min.css',
  '/static/vendor/bootstrap.bundle.min.js',
  '/static/vendor/bootstrap-icons.min.css',
  '/static/pwa/icon-192.png',
  '/static/pwa/icon-512.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.allSettled(PRECACHE.map((url) => cache.add(url)));
    // Take over on next load rather than waiting for every tab to close.
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(
      names.filter((n) => n !== CACHE).map((n) => caches.delete(n))
    );
    await self.clients.claim();
  })());
});

// Stale-while-revalidate for static assets: serve the cached copy instantly,
// refresh it in the background. Version-busted URLs mean a new build is a cache
// miss and fetches fresh, so this never pins an old asset after an update.
async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then((response) => {
      if (response && response.status === 200 && response.type === 'basic') {
        cache.put(request, response.clone());
      }
      return response;
    })
    .catch(() => cached);
  return cached || network;
}

// Network-first for page navigations: always try the live server so auth
// redirects and fresh inventory win. Only fall back to a cached copy (if one
// exists) when the network is unreachable, so kiosk auto-refresh keeps pulling
// live pages whenever it can.
async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    return await fetch(request);
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    return Response.error();
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;

  // Only GET is ever cached or intercepted; writes always go straight through.
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Leave cross-origin requests (camera proxies, external recipe sources) alone.
  if (url.origin !== self.location.origin) return;

  // Never touch the settings/admin surfaces or the auth flow; they must always
  // be live so a stale page can never gate or expose configuration.
  if (url.pathname.startsWith('/setup') ||
      url.pathname.startsWith('/admin') ||
      url.pathname.startsWith('/ui/login') ||
      url.pathname.startsWith('/ui/logout') ||
      url.pathname.startsWith('/ui/pin')) {
    return;
  }

  // Safe static assets and the manifest: cache for offline + installability.
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest') {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }

  // Page navigations: network-first with an offline fallback.
  if (request.mode === 'navigate') {
    event.respondWith(networkFirst(request));
    return;
  }

  // Everything else (API/JSON, polling endpoints) goes to the network untouched
  // and is never cached, so live data and auth always decide the response.
});
