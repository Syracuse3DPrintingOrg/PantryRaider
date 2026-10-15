// Expiring page (templates/expiring.html).
const EXPIRING_DATA = JSON.parse(document.getElementById('expiring-data').textContent);

// The window picker reloads the page for the chosen number of days.
function setExpiringDays(days) {
  window.location.search = '?days=' + encodeURIComponent(days);
}

// Print a food label for one expiring item (FoodAssistant-fb8x). The server
// pulls its name and dates from Grocy, so only the product id is sent.
async function printLabel(productId, name) {
  try {
    const r = await fetch('printing/label', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_id: productId }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.detail || d.error || r.statusText);
    showToast(`Label sent for ${name}.`);
  } catch (e) {
    showToast(`Print failed: ${e.message}`, true);
  }
}

// "Sniff test passed" (FoodAssistant-fxnr): the item is still good, so push
// its best-by out 1, 3, or 5 days. The server extends every dated stock entry
// (from today when the item is already past its date); reloading the page
// with the confirmation refreshes the row, the badges, and the summary counts
// the same way marking an item consumed does.
async function sniffTest(productId, name, extraDays) {
  try {
    const r = await fetch(`expiring/extend/${productId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days: extraDays }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.detail || d.message || r.statusText);
    const msg = d.new_best_by
      ? `Sniff test passed: ${name} is now best by ${d.new_best_by}.`
      : `Sniff test passed, but ${name} had no best-by date to extend.`;
    location = `ui/expiring?days=${EXPIRING_DATA.days}&msg=${encodeURIComponent(msg)}`;
  } catch (e) {
    showToast(`Could not update ${name}: ${e.message}`, true);
  }
}

// "Toss it" (FoodAssistant-64eg): the item went bad, so it leaves stock as
// spoiled rather than eaten. Grocy remembers the difference in its stock log,
// which feeds the Waste summary at the bottom of this page. Confirm first,
// like marking an item consumed: both remove it from stock.
async function tossIt(productId, name, amount) {
  if (!(await PR.confirmBox('This records it as spoiled and removes it from your stock.', { title: `Toss all ${name}?`, ok: 'Toss', danger: true }))) return;
  try {
    const r = await fetch(`expiring/toss/${productId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ amount: amount }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.detail || d.message || r.statusText);
    const msg = `${name} tossed and recorded as spoiled.`;
    location = `ui/expiring?days=${EXPIRING_DATA.days}&msg=${encodeURIComponent(msg)}`;
  } catch (e) {
    showToast(`Could not toss ${name}: ${e.message}`, true);
  }
}

function showToast(msg, isError = false) {
  const toast = document.getElementById('expToast');
  toast.className = `toast align-items-center border-0 text-bg-${isError ? 'danger' : 'success'}`;
  document.getElementById('expToastMsg').textContent = msg;
  new bootstrap.Toast(toast, { delay: 3000 }).show();
}

// Whether a suggestion can be turned into a full recipe on the Cook page
// (AI generates it; Mealie stores it).
const CAN_COOK = EXPIRING_DATA.can_cook;
const AI_CONFIGURED = EXPIRING_DATA.ai_configured;

async function loadUseItUp() {
  const card = document.getElementById('use-it-up-card');
  const body = document.getElementById('use-it-up-body');
  const btn = document.getElementById('btn-use-it-up');
  card.classList.remove('d-none');
  card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  body.innerHTML = '<div class="text-secondary"><span class="spinner-border spinner-border-sm me-2"></span>Thinking of ways to use these up…</div>';
  btn.disabled = true;
  try {
    const r = await fetch('mealie/use-it-up', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({days: EXPIRING_DATA.days}),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || d.message || r.statusText);
    const sug = d.suggestions || [];
    const tips = d.tips || [];
    let htmlOut = '';
    if (d.items && d.items.length) {
      htmlOut += `<div class="small text-secondary mb-2">Recipe ideas for: ${d.items.map(PR.esc).join(', ')}</div>`;
    }
    if (sug.length) {
      htmlOut += '<div class="row g-2 row-cols-1 row-cols-md-2 row-cols-lg-3 mb-3">' + sug.map(s => `
        <div class="col"><div class="card h-100"><div class="card-body py-2 d-flex flex-column">
          <div class="fw-semibold">${PR.esc(s.name)}</div>
          ${s.description ? `<div class="small text-secondary mt-1">${PR.esc(s.description)}</div>` : ''}
          ${(s.uses||[]).length ? `<div class="small text-success mt-1"><i class="bi bi-check2 me-1"></i>${s.uses.map(PR.esc).join(', ')}</div>` : ''}
          ${CAN_COOK ? `<div class="mt-auto pt-2">
            <a class="btn btn-sm btn-outline-primary" href="ui/cook?generate=${encodeURIComponent(s.name)}">
              <i class="bi bi-stars me-1"></i>Cook this
            </a>
          </div>` : ''}
        </div></div></div>`).join('') + '</div>';
    } else if (d.message) {
      htmlOut += `<div class="text-secondary mb-2">${PR.esc(d.message)}</div>`;
    } else if (!AI_CONFIGURED) {
      htmlOut += '<div class="text-secondary mb-2">Recipe ideas need an AI provider (see Settings). The quick tips below always work.</div>';
    } else {
      htmlOut += '<div class="text-secondary mb-2">No recipe ideas came back this time. Try again in a moment.</div>';
    }
    if (tips.length) {
      htmlOut += '<div class="small fw-semibold text-secondary mb-1"><i class="bi bi-lightbulb me-1"></i>Quick tips</div>'
        + '<ul class="small mb-0">' + tips.map(t => `<li>${PR.esc(t)}</li>`).join('') + '</ul>';
    }
    body.innerHTML = htmlOut || '<div class="text-secondary">No ideas right now.</div>';
    card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  } catch (e) {
    body.innerHTML = `<div class="alert alert-danger mb-0">Could not load ideas: ${PR.esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}
