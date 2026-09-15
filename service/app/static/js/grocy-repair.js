// Repair Grocy config (Pi Hosted appliances only).
//
// A Grocy image update can leave Grocy's config.php naming a class that moved
// (the AUTH_CLASS namespace change in Grocy 4.7), after which Grocy answers
// every request with an error page and the inventory stops loading. The
// outage banner offers this button when the app recognises that error and the
// device can fix it itself: it posts to /setup/grocy/repair, which asks the
// host bridge to run the repair helper and then re-probes Grocy. Nothing here
// touches the page markup with HTML from the server; every message lands via
// textContent.
async function repairGrocyConfig(btn) {
  const holder = btn.parentElement;
  const out = holder ? holder.querySelector('[data-grocy-repair-result]') : null;
  const show = (text) => {
    if (!out) return;
    out.textContent = text;
    out.classList.remove('d-none');
  };
  const label = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = 'Repairing...';
  try {
    const r = await fetch('/setup/grocy/repair', { method: 'POST' });
    const d = await r.json().catch(() => ({}));
    if (d.ok && d.grocy_ok) {
      show('Repaired. Grocy answers again; reloading.');
      setTimeout(() => location.reload(), 1200);
      return;
    }
    if (d.ok) {
      show((d.reason || 'The repair ran.') +
           ' Grocy is not answering yet; give it a few seconds and press Refresh.');
    } else {
      show(d.reason || d.error || 'The repair did not run.');
    }
  } catch (e) {
    show('Could not reach the device helper: ' + e.message);
  }
  btn.disabled = false;
  btn.innerHTML = label;
}
