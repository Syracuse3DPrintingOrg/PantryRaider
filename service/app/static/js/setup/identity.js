// Settings, Security: the Identity provider row (routers/setup/identity.py).

function _oidcResult(ok, text) {
  const el = document.getElementById('oidc-result');
  if (!el) return;
  el.innerHTML = text ? '<span class="' + (ok ? 'text-success' : 'text-danger') + '"><i class="bi '
    + (ok ? 'bi-check-circle-fill' : 'bi-x-circle-fill') + ' me-1"></i>' + PR.esc(text) + '</span>' : '';
}

function _renderOidc(d) {
  const status = document.getElementById('oidc-status');
  if (!status || !d || !d.ok) return;
  document.getElementById('oidc-issuer').value = d.issuer || '';
  document.getElementById('oidc-client-id').value = d.client_id || '';
  document.getElementById('oidc-name').value = d.name || '';
  const secret = document.getElementById('oidc-client-secret');
  secret.value = '';
  secret.placeholder = d.has_secret ? 'Saved. Leave blank to keep it.' : '';
  document.getElementById('oidc-callback').textContent = d.callback || '';
  document.getElementById('oidc-clear-btn').classList.toggle('d-none', !d.configured);
  const onThisAddress = !d.host || d.host === d.here;
  if (!d.configured) {
    status.textContent = 'Off.';
  } else if (!d.linked) {
    status.textContent = 'Set up. Link your account to start using it.';
  } else {
    status.textContent = 'Linked to ' + (d.account || 'your account') + ' on ' + d.host
      + (d.only ? '. It is the only way to sign in.' : '.');
  }
  document.getElementById('oidc-link').classList.toggle('d-none', !d.configured || d.linked || !d.password_set);
  document.getElementById('oidc-linked').classList.toggle('d-none', !d.linked);
  document.getElementById('oidc-only').checked = !!d.only;
  if (d.linked && !onThisAddress) {
    _oidcResult(false, 'Sign-in with ' + d.display_name + ' works at https://' + d.host + '.');
  }
}

async function loadOidc() {
  if (!document.getElementById('oidc-status')) return;
  try {
    const r = await fetch('setup/oidc', {credentials: 'same-origin'});
    _renderOidc(await r.json());
  } catch (e) {
    document.getElementById('oidc-status').textContent = 'Could not load this setting.';
  }
  let outcome = '';
  try { outcome = new URLSearchParams(location.search).get('oidc') || ''; } catch (e) {}
  if (outcome === 'linked') _oidcResult(true, 'Your account is linked. You can sign in with it now.');
  else if (outcome === 'failed') _oidcResult(false, 'Linking did not finish. Try again.');
}

async function saveOidc() {
  const body = {
    issuer: (document.getElementById('oidc-issuer').value || '').trim(),
    client_id: (document.getElementById('oidc-client-id').value || '').trim(),
    client_secret: document.getElementById('oidc-client-secret').value || '',
    name: (document.getElementById('oidc-name').value || '').trim(),
  };
  const d = await postJson('setup/oidc/config', body);
  if (!d.ok) { _oidcResult(false, d.error); return; }
  _renderOidc(d);
  _oidcResult(true, d.configured ? 'Saved.' : 'Turned off.');
}

async function clearOidc() {
  const ok = await PR.confirmBox('Turn off sign-in with your identity provider? Your linked account is forgotten too.');
  if (!ok) return;
  const d = await postJson('setup/oidc/config', {issuer: '', client_id: '', client_secret: '', name: ''});
  if (!d.ok) { _oidcResult(false, d.error); return; }
  _renderOidc(d);
  _oidcResult(true, 'Turned off.');
}

async function linkOidc() {
  const credential = document.getElementById('oidc-credential').value || '';
  const d = await postJson('setup/oidc/link', {credential});
  if (!d.ok) { _oidcResult(false, d.error); return; }
  // A navigation, not a form post, so the page's form-action policy allows it.
  window.location.assign(d.url);
}

async function unlinkOidc() {
  const ok = await PR.confirmBox('Unlink your account? It will stop signing in to this kitchen.');
  if (!ok) return;
  const d = await postJson('setup/oidc/unlink', {});
  if (!d.ok) { _oidcResult(false, d.error); return; }
  _renderOidc(d);
  _oidcResult(true, 'Unlinked.');
}

async function setOidcOnly() {
  const box = document.getElementById('oidc-only');
  const on = !!box.checked;
  if (on) {
    const ok = await PR.confirmBox('Make your identity provider the only way to sign in? The password, passkeys and Forager stop working on the sign-in page. From home, the device password still works.');
    if (!ok) { box.checked = false; return; }
  }
  const d = await postJson('setup/oidc/only', {on});
  if (!d.ok) { box.checked = !on; _oidcResult(false, d.error); return; }
  _renderOidc(d);
  _oidcResult(true, on ? 'Only your identity provider signs people in now.' : 'Every way to sign in works again.');
}

document.addEventListener('DOMContentLoaded', loadOidc);
