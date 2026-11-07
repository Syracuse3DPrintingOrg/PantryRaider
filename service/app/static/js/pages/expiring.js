// Expiring page (templates/expiring.html).
const EXPIRING_DATA = JSON.parse(document.getElementById('expiring-data').textContent);

// The window picker reloads the page for the chosen number of days.
function setExpiringDays(days) {
  window.location.search = '?days=' + encodeURIComponent(days);
}

// Print a food label for one expiring item (FoodAssistant-fb8x). The server
// pulls its name and dates from Grocy, so only the product id is sent. Those
// are the product's, though, so a row of one best-by date sends its own date,
// added date and stock entry, the way the Inventory page's rows do.
async function printLabel(productId, name, row) {
  const label = { product_id: productId };
  if (row) {
    label.name = name;
    label.best_by = row.best_by;
    label.added = row.added || '';
    if (row.stock_entry_id) label.stock_entry_id = row.stock_entry_id;
  }
  try {
    const r = await fetch('printing/label', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(label),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(d.detail || d.error || r.statusText);
    showToast(`Label sent for ${name}.`);
  } catch (e) {
    showToast(`Print failed: ${e.message}`, true);
  }
}

// The page again with a message on top, which refreshes the rows, the badges
// and the summary counts the same way marking an item consumed does.
function reloadWith(msg, msgType = 'success') {
  location = `ui/expiring?days=${EXPIRING_DATA.days}&msg=${encodeURIComponent(msg)}`
    + (msgType === 'success' ? '' : `&msg_type=${msgType}`);
}

// A row is the stock of one best-by date in one place. Once the server
// answers 404 (that stock is gone) or 409 (it changed since the page loaded,
// or Grocy cannot act on it alone), the row no longer shows what is there, so
// the page is read again with the reason on top, like the Inventory page's
// rowIsStale.
function failed(r, d) {
  const err = new Error(d.detail || d.message || r.statusText);
  err.stale = r.status === 404 || r.status === 409;
  return err;
}

async function postToss(productId, body) {
  const r = await fetch(`expiring/toss/${productId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  return { r, d };
}

// "Sniff test passed" (FoodAssistant-fxnr): the item is still good, so push
// its best-by out 1, 3, or 5 days (from today when the item is already past
// its date). A row sends its place and date, so only that stock moves, never
// the same item kept good in the freezer.
async function sniffTest(productId, name, extraDays, locationId, bestBy) {
  const body = { days: extraDays };
  if (bestBy) Object.assign(body, { location_id: locationId, best_before_date: bestBy });
  try {
    const r = await fetch(`expiring/extend/${productId}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw failed(r, d);
    reloadWith(d.new_best_by
      ? `Sniff test passed: ${name} is now best by ${d.new_best_by}.`
      : `Sniff test passed, but ${name} had no best-by date to extend.`);
  } catch (e) {
    if (e.stale) reloadWith(`Could not update ${name}: ${e.message}`, 'danger');
    else showToast(`Could not update ${name}: ${e.message}`, true);
  }
}

// "Toss it" (FoodAssistant-64eg): the item went bad, so it leaves stock as
// spoiled rather than eaten. Grocy remembers the difference in its stock log,
// which feeds the Waste summary at the bottom of this page. Confirm first,
// like marking an item consumed: both remove it from stock. A row sends its
// place and date, so only that stock goes, and the question says how much and
// which: "Toss 500 g Flour (best by Oct 25)?". It also sends the amount it
// showed, so stock that changed since the page loaded is left alone.
//
// `row` is {location_id, best_before_date, words, what, place} for a row of
// one date in one place, {words} for a row of one date with no place (Grocy
// then picks which stock goes), and absent for a product row.
async function tossIt(productId, name, amount, row) {
  row = row || {};
  const title = row.words ? `Toss ${row.words}?` : `Toss all ${name}?`;
  const note = row.words && !row.best_before_date
    ? 'This records it as spoiled and removes it from your stock. Grocy picks which ones go, opened and soonest first.'
    : 'This records it as spoiled and removes it from your stock.';
  if (!(await PR.confirmBox(note, { title, ok: 'Toss', danger: true }))) return;
  const body = { amount: amount };
  if (row.best_before_date) {
    Object.assign(body, {
      location_id: row.location_id, best_before_date: row.best_before_date,
      expected_amount: amount,
    });
  }
  try {
    let { r, d } = await postToss(productId, body);
    if (r.status === 409 && d.place_toss) {
      // An opened one in the same place is in the way, and Grocy always
      // takes that first. Ask once more, saying plainly what that means.
      // Nothing was tossed, so cancelling leaves the page as it is.
      const place = row.place ? `the ${row.place}` : 'this place';
      const ok = await PR.confirmBox(
        `There is an opened ${name} in ${place}, and Grocy always tosses that first, so it can't toss only the ones on this row. `
        + `You can toss ${row.what} from ${place} in Grocy's order instead: `
        + 'the opened one goes first, then the soonest dates, so some of what goes may not be from this row.',
        { title: `Toss ${row.what} from ${place} instead?`, ok: `Toss from ${place}`, danger: true });
      if (!ok) return;
      ({ r, d } = await postToss(productId, { ...body, place_wide: true }));
      if (r.ok && d.ok) {
        reloadWith(`Tossed ${row.what} from ${place}, opened one first, and recorded it as spoiled.`);
        return;
      }
    }
    if (!r.ok || !d.ok) throw failed(r, d);
    if (d.complete === false) {
      // Something changed the row between the read and the toss, so Grocy
      // only reached part of it.
      reloadWith(`Tossed ${+Number(d.consumed).toFixed(2)} of ${+Number(d.requested).toFixed(2)} ${name}. `
        + 'Grocy would use other stock for the rest.', 'warning');
      return;
    }
    reloadWith(`${name} tossed and recorded as spoiled.`);
  } catch (e) {
    if (e.stale) reloadWith(`Could not toss ${name}: ${e.message}`, 'danger');
    else showToast(`Could not toss ${name}: ${e.message}`, true);
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
