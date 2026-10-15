// Settings, Security: keys, this device's own programs and home network
// access (0.21). Everything here saves on change through /api/credentials and
// /setup/security, outside the pane's Save Security button. Every string that
// comes from the server lands as text, never as markup, and every URL is
// relative so the page works behind the Home Assistant Ingress base tag.
(function () {
  'use strict';

  var state = null;
  var copyText_ = {};

  function el(id) { return document.getElementById(id); }

  function text(node, value) {
    if (node) node.textContent = value == null ? '' : String(value);
  }

  function show(node, on) {
    if (node) node.classList.toggle('d-none', !on);
  }

  function result(id, ok, msg) {
    if (typeof setResult === 'function') { setResult(id, ok, msg); return; }
    text(el(id), msg);
  }

  function joinLabels(labels) {
    labels = labels || [];
    if (labels.length <= 1) return labels.join('');
    return labels.slice(0, -1).join(', ') + ' and ' + labels[labels.length - 1];
  }

  function when(iso) {
    if (!iso) return 'Never';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString();
  }

  async function postJSON(url, body) {
    var r = await fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})
    });
    var data = {};
    try { data = await r.json(); } catch (e) { data = {}; }
    return {ok: r.ok, status: r.status, data: data};
  }

  function errorText(data, fallback) {
    if (!data) return fallback;
    if (data.error) return data.error;
    if (data.detail) return typeof data.detail === 'string' ? data.detail : fallback;
    if (data.errors) {
      var parts = [];
      Object.keys(data.errors).forEach(function (k) { parts.push(data.errors[k]); });
      if (parts.length) return parts.join(' ');
    }
    return fallback;
  }

  // --- Security state -------------------------------------------------------

  function renderWarnings() {
    var s = state;
    // The server's own sign-in rule, so these warnings show exactly when a
    // password alone would be refused from here (older answers: the origin).
    var outside = (typeof s.sign_in_outside === 'boolean') ? s.sign_in_outside
      : (s.origin === 'proxied' || s.origin === 'public');
    var needed = false;
    try { needed = new URLSearchParams(location.search).get('twofactor') === 'needed'; } catch (e) {}
    var tf = el('twofactor-next');
    if (tf) {
      tf.textContent = '';
      if (needed || (s.password_set && !s.two_factor_on && outside)) {
        var a = document.createElement('div');
        a.className = 'alert alert-warning py-2 small';
        a.textContent = copyText_.TWO_FACTOR_NEXT_MSG || '';
        tf.appendChild(a);
      }
    }
    var oo = el('outside-order');
    if (oo) {
      oo.textContent = '';
      if (s.open && !s.two_factor_on && outside) {
        var b = document.createElement('div');
        b.className = 'alert alert-warning py-2 small';
        b.textContent = copyText_.OUTSIDE_ORDER_MSG || '';
        oo.appendChild(b);
      }
    }
    var kw = el('keyless-devices-warning');
    if (kw) {
      var msg = '';
      if (!s.password_set && (s.keyless_devices || []).length) {
        msg = (copyText_.KEYLESS_DEVICES_WARNING || '').replace('{labels}', joinLabels(s.keyless_devices));
      } else if (s.password_set && (s.keyless_refused || []).length) {
        msg = (copyText_.KEYLESS_REFUSED_WARNING || '').replace('{labels}', joinLabels(s.keyless_refused));
      }
      text(kw, msg);
      show(kw, !!msg);
    }
    var ku = el('kiosk-url-warning');
    text(ku, s.kiosk_url_warning || '');
    show(ku, !!s.kiosk_url_warning);
    ['same-host-proxy-warning', 'same-host-proxy-warning-pw'].forEach(function (id, i) {
      var node = el(id);
      // One copy inside Home network access (no password), one below it otherwise.
      var on = !!s.same_host_proxy && (i === 0 ? s.open : !s.open);
      text(node, on ? copyText_.SAME_HOST_PROXY_WARNING : '');
      show(node, on);
    });
    var gw = el('gateway-hint');
    text(gw, s.gateway_hint || '');
    show(gw, !!s.gateway_hint);
  }

  function renderHomeAccess() {
    var s = state;
    show(el('home-access-panel'), !!s.open);
    text(el('open-notice'), copyText_.OPEN_NOTICE || '');
    var box = el('family-switches');
    if (box) {
      box.textContent = '';
      Object.keys(s.families || {}).forEach(function (fam) {
        var info = s.families[fam] || {};
        var row = document.createElement('div');
        row.className = 'form-check form-switch mb-1';
        var input = document.createElement('input');
        input.className = 'form-check-input';
        input.type = 'checkbox';
        input.id = 'family-' + fam;
        input.checked = !!info.required;
        input.addEventListener('change', function (ev) {
          ev.stopPropagation();
          var body = {require_key_families: {}};
          body.require_key_families[fam] = input.checked;
          saveSettings(body, 'home-access-result');
        });
        var label = document.createElement('label');
        label.className = 'form-check-label';
        label.htmlFor = input.id;
        label.textContent = info.label || fam;
        row.appendChild(input);
        row.appendChild(label);
        if (info.keyless && info.keyless.last) {
          var note = document.createElement('div');
          note.className = 'text-secondary small';
          note.textContent = 'Used without a key, last ' + when(info.keyless.last) +
            (info.keyless.peer ? ' from ' + info.keyless.peer : '');
          row.appendChild(note);
        }
        box.appendChild(row);
      });
    }
    var cidrs = el('sec-lan-cidrs');
    if (cidrs) {
      cidrs.disabled = !s.lan_cidrs_editable;
      text(el('sec-lan-cidrs-note'), s.lan_cidrs_editable ? '' : copyText_.LAN_CIDRS_REMOTE_MSG);
    }
    var grace = el('grace-sightings');
    if (grace) {
      grace.textContent = '';
      var om = s.open_mode || {};
      var seen = om.seen || [];
      if (om.grace_until && (seen.length || om.other_count)) {
        var head = document.createElement('div');
        head.className = 'fw-semibold';
        head.textContent = 'Reached from outside your home network (answered until ' + when(om.grace_until) + '):';
        grace.appendChild(head);
        var ul = document.createElement('ul');
        ul.className = 'mb-1';
        seen.forEach(function (row) {
          var li = document.createElement('li');
          li.textContent = row.host + ' (' + (row.kind === 'name' ? 'an unknown name' : 'through a proxy') +
            ', ' + row.count + ' times, last ' + when(row.last) + ')';
          ul.appendChild(li);
        });
        if (om.other_count) {
          var li2 = document.createElement('li');
          li2.textContent = om.other_count + ' more from other addresses';
          ul.appendChild(li2);
        }
        grace.appendChild(ul);
      }
    }
    var sat = el('satellites-needing-update');
    if (sat) {
      var list = (s.satellites_needing_update || []).map(function (d) {
        return d.name + (d.version ? ' (' + d.version + ')' : '');
      });
      text(sat, list.length ? 'Satellites that still need these keys: ' + list.join(', ') : '');
    }
  }

  function renderPrograms() {
    var s = state;
    var lp = s.local_programs || {};
    var loop = lp.loopback || {};
    var trustOn = loop.state === 'legacy' || (loop.retrust_until && new Date(loop.retrust_until) > new Date());
    show(el('local-programs-panel'), !!(lp.bridge || trustOn));
    var list = el('local-programs-list');
    if (list) {
      list.textContent = '';
      [['screen', 'The kitchen screen'], ['streamdeck', 'The Stream Deck'], ['gadgets', 'The sensor reader']]
        .forEach(function (pair) {
          var info = lp[pair[0]] || {};
          if (info.installed === false) return;
          var row = document.createElement('div');
          row.textContent = pair[1] + ': ' + (info.proven ? 'signs in with its own key' : 'not signed in with its own key yet');
          list.appendChild(row);
        });
      var other = lp.other_callers || {};
      if (other.last) {
        var row2 = document.createElement('div');
        row2.textContent = 'Other programs on this device connect without a key (last ' + when(other.last) + '). Create a key for each one above.';
        list.appendChild(row2);
      }
    }
    var waiting = ['screen', 'streamdeck', 'gadgets'].some(function (k) {
      var info = lp[k] || {};
      return info.installed && !info.proven;
    });
    text(el('local-trust-text'), !trustOn ? copyText_.LOCAL_TRUST_OFF
      : (waiting ? copyText_.LOCAL_TRUST_ON + ' ' + copyText_.LOCAL_TRUST_WAITING : copyText_.LOCAL_TRUST_ON));
    var conf = el('local-trust-confirm');
    text(conf, loop.confirm_needed ? copyText_.LOCAL_TRUST_CONFIRM : '');
    show(conf, !!loop.confirm_needed);
  }

  async function loadState() {
    try {
      var r = await fetch('setup/security/state', {credentials: 'same-origin'});
      if (!r.ok) return;
      state = await r.json();
      copyText_ = state.copy || {};
      renderWarnings();
      renderHomeAccess();
      renderPrograms();
    } catch (e) { /* the pane keeps its static parts */ }
  }

  async function saveSettings(body, resultId) {
    var res = await postJSON('setup/security/settings', body);
    result(resultId, res.ok, res.ok ? 'Saved.' : errorText(res.data, 'That was not saved.'));
    if (res.ok) loadState();
    return res.ok;
  }

  // --- Keys -----------------------------------------------------------------

  function scopeLabel(scope) {
    var labels = (copyText_.SCOPE_LABELS) || {};
    return labels[scope] || scope;
  }

  function cell(row, value) {
    var td = document.createElement('td');
    td.textContent = value == null ? '' : String(value);
    row.appendChild(td);
    return td;
  }

  async function loadCredentials() {
    var body = el('credentials-rows');
    if (!body) return;
    try {
      var r = await fetch('api/credentials', {credentials: 'same-origin'});
      var data = await r.json();
      if (!r.ok) { body.textContent = ''; return; }
      body.textContent = '';
      (data.credentials || []).forEach(function (c) {
        var tr = document.createElement('tr');
        cell(tr, c.name);
        cell(tr, c.scope_label || scopeLabel(c.scope));
        cell(tr, when(c.last_used));
        cell(tr, c.last_ip || '');
        var status = cell(tr, c.status === 'retiring' ? 'No longer needed'
          : (c.service_keys_withheld ? 'Service keys held back' : 'Active'));
        if (c.service_keys_withheld) {
          status.title = 'A device using this key went back to a version older than 0.21, '
            + 'and older versions need your AI, recipe and Home Assistant keys on the device itself.';
        }
        var actions = cell(tr, '');
        actions.className = 'set-keys-actions';
        if (c.service_keys_withheld) {
          var share = document.createElement('button');
          share.type = 'button';
          share.className = 'btn btn-outline-secondary btn-sm me-1';
          share.textContent = 'Let this key receive service keys again';
          share.addEventListener('click', function () { shareServiceKeys(c); });
          actions.appendChild(share);
        }
        if (c.suggested_scope) {
          var limit = document.createElement('button');
          limit.type = 'button';
          limit.className = 'btn btn-outline-secondary btn-sm me-1';
          limit.textContent = 'Limit to ' + scopeLabel(c.suggested_scope);
          limit.addEventListener('click', function () { limitKey(c, c.suggested_scope); });
          actions.appendChild(limit);
        }
        if (c.source !== 'env') {
          var rev = document.createElement('button');
          rev.type = 'button';
          rev.className = 'btn btn-outline-danger btn-sm';
          rev.textContent = 'Revoke';
          rev.addEventListener('click', function () { revokeKey(c); });
          actions.appendChild(rev);
        }
        body.appendChild(tr);
      });
      if (!(data.credentials || []).length) {
        var tr2 = document.createElement('tr');
        var td = cell(tr2, 'No keys yet.');
        td.colSpan = 6;
        td.className = 'text-secondary small';
        body.appendChild(tr2);
      }
    } catch (e) { /* leave the table as it is */ }
  }

  async function revokeKey(c) {
    if (!window.confirm('Revoke ' + c.name + '? Anything using it stops connecting.')) return;
    var res = await postJSON('api/credentials/' + encodeURIComponent(c.id) + '/revoke', {});
    result('credentials-result', res.ok, res.ok ? 'Revoked.' : errorText(res.data, 'That key was not revoked.'));
    loadCredentials();
  }

  // A Bandit put back on an older version gets its service keys again
  // (FoodAssistant-xym8y); the server words the result.
  async function shareServiceKeys(c) {
    var res = await postJSON('api/credentials/' + encodeURIComponent(c.id) + '/share-service-keys', {});
    result('credentials-result', res.ok,
           res.ok ? (res.data.message || 'Saved.') : errorText(res.data, 'That was not changed.'));
    loadCredentials();
  }

  async function limitKey(c, scope) {
    var res = await postJSON('api/credentials/' + encodeURIComponent(c.id) + '/scope', {scope: scope});
    var box = el('credentials-result');
    if (!res.ok) { result('credentials-result', false, errorText(res.data, 'That was not changed.')); return; }
    if (box) {
      box.textContent = '';
      var span = document.createElement('span');
      span.className = 'text-success small me-2';
      span.textContent = c.name + ' is limited to ' + scopeLabel(scope) + '.';
      var undo = document.createElement('button');
      undo.type = 'button';
      undo.className = 'btn btn-link btn-sm p-0';
      undo.textContent = 'Undo';
      undo.addEventListener('click', async function () {
        await postJSON('api/credentials/' + encodeURIComponent(c.id) + '/scope', {scope: res.data.previous});
        box.textContent = '';
        loadCredentials();
      });
      box.appendChild(span);
      box.appendChild(undo);
    }
    loadCredentials();
  }

  async function createKey() {
    var name = (el('cred-name') || {}).value || '';
    var scope = (el('cred-scope') || {}).value || '';
    var pw = el('cred-password') ? el('cred-password').value : '';
    var res = await postJSON('api/credentials', {name: name.trim(), scope: scope, password: pw});
    var out = el('cred-created');
    if (!out) return;
    out.textContent = '';
    if (!res.ok) { result('cred-created', false, errorText(res.data, 'The key was not created.')); return; }
    if (el('cred-password')) el('cred-password').value = '';
    var wrap = document.createElement('div');
    wrap.className = 'alert alert-warning py-2 small';
    var note = document.createElement('div');
    note.textContent = copyText_.KEY_SHOWN_ONCE || 'Copy this key now. It is not shown again.';
    var code = document.createElement('code');
    code.className = 'd-block my-1 text-break';
    code.textContent = res.data.key;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-sm btn-outline-dark';
    btn.textContent = 'Copy';
    btn.addEventListener('click', function () {
      if (typeof copyText === 'function') copyText(res.data.key, btn);
    });
    wrap.appendChild(note);
    wrap.appendChild(code);
    wrap.appendChild(btn);
    out.appendChild(wrap);
    loadCredentials();
  }

  // --- Wiring ---------------------------------------------------------------

  function bindOnce() {
    var pane = el('pane-security');
    if (!pane || pane.dataset.credBound === '1') return;
    pane.dataset.credBound = '1';
    var create = el('cred-create-btn');
    if (create) create.addEventListener('click', createKey);
    var retire = el('local-trust-retire');
    if (retire) retire.addEventListener('click', async function () {
      var unproven = [];
      var lp = (state && state.local_programs) || {};
      ['screen', 'streamdeck', 'gadgets'].forEach(function (k) {
        if ((lp[k] || {}).installed && !(lp[k] || {}).proven) unproven.push(k);
      });
      var msg = 'Require every program on this device to sign in with its own key?';
      if (unproven.length) msg += ' Some have not signed in with their own key yet and will stop connecting.';
      if (!window.confirm(msg)) return;
      var res = await postJSON('setup/security/local-trust', {action: 'retire'});
      // The server names the programs that have not signed in on their own.
      var warnings = (res.ok && res.data && Array.isArray(res.data.warnings)) ? res.data.warnings : [];
      result('local-trust-result', res.ok,
             res.ok ? ['Saved.'].concat(warnings).join(' ') : errorText(res.data, 'That was not saved.'));
      loadState();
    });
    var retrust = el('local-trust-retrust');
    if (retrust) retrust.addEventListener('click', async function () {
      var res = await postJSON('setup/security/local-trust', {action: 'retrust'});
      result('local-trust-result', res.ok, res.ok ? 'Saved.' : errorText(res.data, 'That was not saved.'));
      loadState();
    });
    var everywhere = el('signout-everywhere-btn');
    if (everywhere) everywhere.addEventListener('click', async function () {
      var res = await postJSON('setup/security/signout-everywhere', {});
      result('signout-everywhere-result', res.ok,
             res.ok ? 'Every other browser is signed out.' : errorText(res.data, 'That did not work.'));
    });
    var signout = el('kiosk-signout-btn');
    if (signout) signout.addEventListener('click', async function () {
      var res = await postJSON('setup/security/kiosk-signout', {});
      result('security-settings-result', res.ok,
             res.ok ? 'The kitchen screen signs in again the next time it starts.' : errorText(res.data, 'That did not work.'));
    });
    var bindings = [
      ['sec-kiosk-admin', 'kiosk_screen_is_admin', 'checked', 'security-settings-result'],
      ['sec-legacy-mirror', 'legacy_mirror', 'checked', 'security-settings-result'],
      ['sec-auto-retire', 'legacy_key_auto_retire', 'checked', 'security-settings-result'],
      ['sec-sat-keys', 'satellite_legacy_credentials', 'value', 'security-settings-result'],
      ['sec-lan-cidrs', 'lan_cidrs', 'value', 'home-access-result'],
      ['sec-home-host-names', 'home_host_names', 'value', 'home-access-result']
    ];
    bindings.forEach(function (b) {
      var node = el(b[0]);
      if (!node) return;
      node.addEventListener('change', function (ev) {
        ev.stopPropagation();
        var body = {};
        body[b[1]] = node[b[2]];
        saveSettings(body, b[3]);
      });
    });
  }

  function refresh() {
    bindOnce();
    loadState();
    loadCredentials();
  }

  document.addEventListener('DOMContentLoaded', function () {
    var pane = el('pane-security');
    if (!pane) return;
    var pill = document.querySelector('.side-menu [data-bs-target="#pane-security"]');
    if (pill) pill.addEventListener('shown.bs.tab', refresh);
    if (pane.classList.contains('active')) refresh();
  });

  window.loadSecurityCredentials = refresh;
})();
