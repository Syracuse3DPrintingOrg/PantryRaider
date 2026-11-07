// Settings, Security: signed-in browsers, trusted devices, and when
// two-factor asks for its code (routers/setup/signins.py, setup/totp.py).

function _signinsResult(ok, text) {
  const el = document.getElementById('signout-everywhere-result');
  if (!el) return;
  el.innerHTML = '<span class="' + (ok ? 'text-success' : 'text-danger') + '"><i class="bi '
    + (ok ? 'bi-check-circle-fill' : 'bi-x-circle-fill') + ' me-1"></i>' + PR.esc(text || '') + '</span>';
}

function _renderSignins(d) {
  const list = document.getElementById('signins-list');
  if (!list) return;
  const rows = d.sessions || [];
  if (!rows.length) {
    list.textContent = 'Only this browser.';
  } else {
    list.innerHTML = '<ul class="list-unstyled mb-0">' + rows.map(r =>
      '<li class="d-flex align-items-center gap-2 mb-1">'
      + '<i class="bi ' + (r.role === 'kitchen screen' ? 'bi-display' : 'bi-laptop') + '"></i>'
      + '<span class="text-body">' + PR.esc(r.browser) + '</span>'
      + '<span class="text-secondary">' + PR.esc([r.role, r.current ? 'this browser' : r.last_seen].filter(Boolean).join(', ')) + '</span>'
      + (r.current ? '' : '<button type="button" class="btn btn-link btn-sm text-danger p-0 ms-auto" data-on-click="revokeSignin('
        + PR.escAttrJs(r.id) + ')">Sign out</button>')
      + '</li>').join('') + '</ul>';
  }
  const trusted = document.getElementById('signins-trusted');
  if (trusted) {
    trusted.classList.toggle('d-none', !d.trusted);
    trusted.innerHTML = PR.esc(d.trusted === 1 ? '1 device skips the two-factor code.'
                                              : d.trusted + ' devices skip the two-factor code.')
      + ' <button type="button" class="btn btn-link btn-sm p-0 align-baseline" data-on-click="forgetTrusted()">Ask them again</button>';
  }
}

async function loadSignins() {
  if (!document.getElementById('signins-list')) return;
  try {
    const r = await fetch('setup/signins', {credentials: 'same-origin'});
    _renderSignins(await r.json());
  } catch (e) {
    document.getElementById('signins-list').textContent = 'Could not load the list.';
  }
}

async function revokeSignin(id) {
  const d = await postJson('setup/signins/revoke', {id});
  if (!d.ok) { _signinsResult(false, d.error); return; }
  _renderSignins(d);
  _signinsResult(true, d.message);
}

async function forgetTrusted() {
  const d = await postJson('setup/signins/forget-trusted', {});
  if (!d.ok) { _signinsResult(false, d.error || 'That did not work.'); return; }
  _renderSignins(d);
  _signinsResult(true, d.message);
}

async function saveTotpScope(sel) {
  const el = document.getElementById('totp-scope-result');
  const credential = ((document.getElementById('totp-disable-code') || {}).value || '').trim();
  const d = await postJson('setup/totp/scope', {scope: sel.value, credential});
  if (el) {
    el.innerHTML = '<span class="small ' + (d.ok ? 'text-success' : 'text-danger') + '">'
      + PR.esc(d.ok ? d.message : d.error) + '</span>';
  }
  if (!d.ok) sel.value = sel.value === 'outside' ? 'always' : 'outside';
}

document.addEventListener('DOMContentLoaded', loadSignins);
