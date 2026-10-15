// Start page head (templates/start.html), loaded plain so it runs before
// anything paints.

// Mark an appliance panel BEFORE anything paints, so the ambient drift never
// starts there (see .pr-still in the page styles).
try {
  if (localStorage.getItem('kioskMode') === 'true')
    document.documentElement.classList.add('pr-still');
} catch (e) {}

// The status script is deferred, so it runs after the document parses.
// Every block below therefore waits for the same moment before it looks
// for window.PRKioskStatus, otherwise the fallback pollers start first and
// the page goes back to one request per count.
function prOnReady(fn) {
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', fn);
  else fn();
}
