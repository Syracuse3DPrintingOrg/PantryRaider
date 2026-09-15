/*
 * One place to turn a failed request into something worth reading.
 *
 * Pages used to do `throw new Error((await r.json()).detail || r.statusText)`.
 * When the server answered with HTML or plain text (a proxy page, a crash, a
 * dropped connection), the r.json() call threw first, so the banner showed a
 * parser complaint like "Unexpected token < in JSON at position 0", and when
 * the reply was JSON without a detail field it showed nothing at all.
 *
 * prErrText always comes back with a sentence: the server's own explanation
 * when there is one, otherwise a plain statement that the request failed.
 */
(function () {
  async function prErrText(r) {
    try {
      const j = await r.json();
      if (j && j.detail) return j.detail;
      if (j && j.message) return j.message;
      if (j && j.error) return j.error;
    } catch (e) { /* not JSON, fall through */ }
    if (r && r.status) return 'The server returned an error (HTTP ' + r.status + ').';
    return 'The request did not go through.';
  }
  // The catch side. An Error raised from prErrText already reads as a
  // sentence, but a dropped connection arrives as "Failed to fetch", which
  // tells a cook nothing. Say what actually happened instead.
  function prReason(e) {
    const m = (e && e.message) ? String(e.message) : '';
    if (!m || /failed to fetch|networkerror|load failed|network request failed|the internet connection appears/i.test(m))
      return 'The app could not reach the server.';
    return m;
  }

  window.prErrText = prErrText;
  window.prReason = prReason;
})();
