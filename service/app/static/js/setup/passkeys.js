// Settings, Security: the Passkeys row (routers/setup/passkeys.py).

function _passkeyResult(ok, text) {
  const el = document.getElementById('passkeys-result');
  if (!el) return;
  el.innerHTML = '<span class="' + (ok ? 'text-success' : 'text-danger') + '"><i class="bi '
    + (ok ? 'bi-check-circle-fill' : 'bi-x-circle-fill') + ' me-1"></i>' + PR.esc(text || '') + '</span>';
}

function _passkeyDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toLocaleDateString();
}

function _renderPasskeys(d) {
  const list = document.getElementById('passkeys-list');
  const add = document.getElementById('passkeys-add');
  const notHere = document.getElementById('passkeys-not-here');
  if (!list) return;
  const keys = d.passkeys || [];
  if (!keys.length) {
    list.textContent = 'No passkeys yet.';
  } else {
    list.innerHTML = '<ul class="list-unstyled mb-0">' + keys.map(k =>
      '<li class="d-flex align-items-center gap-2 mb-1">'
      + '<i class="bi bi-fingerprint"></i>'
      + '<span class="text-body">' + PR.esc(k.name) + '</span>'
      + '<span class="text-secondary">' + PR.esc(k.rp_id)
      + (k.last_used ? ', last used ' + PR.esc(_passkeyDate(k.last_used))
                     : ', added ' + PR.esc(_passkeyDate(k.created))) + '</span>'
      + '<button type="button" class="btn btn-link btn-sm text-danger p-0 ms-auto" data-on-click="removePasskey('
      + PR.escAttrJs(k.id) + ')">Remove</button></li>').join('') + '</ul>';
  }
  const usable = d.here && window.PRPasskey && PRPasskey.supported();
  if (add) add.classList.toggle('d-none', !usable || !d.password_set);
  if (notHere) {
    notHere.classList.toggle('d-none', usable && d.password_set);
    notHere.textContent = !d.password_set
      ? 'Set a password for this kitchen first.'
      : 'To add a passkey, open this kitchen over HTTPS by its web address, such as through Forager remote access or your own domain.';
  }
}

async function loadPasskeys() {
  if (!document.getElementById('passkeys-list')) return;
  try {
    const r = await fetch('setup/passkeys', {credentials: 'same-origin'});
    _renderPasskeys(await r.json());
  } catch (e) {
    document.getElementById('passkeys-list').textContent = 'Could not load passkeys.';
  }
}

async function addPasskey() {
  const name = (document.getElementById('passkey-name').value || '').trim();
  const credential = document.getElementById('passkey-credential').value || '';
  const begin = await postJson('setup/passkeys/register/begin', {name, credential});
  if (!begin.ok) { _passkeyResult(false, begin.error); return; }
  let cred;
  try {
    cred = await PRPasskey.create(begin.options);
  } catch (e) {
    if (e && e.name === 'InvalidStateError') _passkeyResult(false, 'That device already has a passkey for this kitchen.');
    else if (!PRPasskey.cancelled(e)) _passkeyResult(false, 'That did not work. Try adding the passkey again.');
    return;
  }
  const done = await postJson('setup/passkeys/register/finish', {credential: cred});
  if (!done.ok) { _passkeyResult(false, done.error); return; }
  document.getElementById('passkey-credential').value = '';
  document.getElementById('passkey-name').value = '';
  _renderPasskeys(done);
  _passkeyResult(true, done.message);
}

async function removePasskey(id) {
  const ok = await PR.confirmBox('Remove this passkey? It will stop signing in to this kitchen, and every other browser is signed out.');
  if (!ok) return;
  const d = await postJson('setup/passkeys/remove', {id});
  if (!d.ok) { _passkeyResult(false, d.error); return; }
  _renderPasskeys(d);
  _passkeyResult(true, d.message);
}

document.addEventListener('DOMContentLoaded', loadPasskeys);
