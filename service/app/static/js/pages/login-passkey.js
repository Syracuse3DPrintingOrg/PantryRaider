// "Sign in with a passkey" on the sign-in page (templates/login.html).
// The button stays hidden unless this browser can use passkeys here and
// the page said a passkey was added for this address.

(function () {
  var box = document.getElementById('passkey-signin');
  var btn = document.getElementById('passkey-signin-btn');
  var msg = document.getElementById('passkey-signin-msg');
  if (!box || !btn || !window.PRPasskey || !PRPasskey.supported()) return;
  box.classList.remove('d-none');

  function post(fields) {
    return fetch('ui/login', {method: 'POST', credentials: 'same-origin',
                              body: new URLSearchParams(fields)})
      .then(function (r) { return r.json(); });
  }

  function say(text) {
    msg.textContent = text || '';
    msg.classList.toggle('d-none', !text);
  }

  btn.addEventListener('click', async function () {
    say('');
    btn.disabled = true;
    try {
      var begin = await post({mode: 'passkey_begin'});
      if (!begin.ok) {
        say(begin.error || 'No passkey was added for this address yet. Sign in with your password, then add one in Settings, Security.');
        return;
      }
      var cred;
      try {
        cred = await PRPasskey.get(begin.options);
      } catch (e) {
        if (!PRPasskey.cancelled(e)) say('That passkey did not work here. Try again or use your password.');
        return;
      }
      var done = await post({mode: 'passkey_finish', credential: JSON.stringify(cred)});
      if (done.ok) { location.href = done.next || 'ui/'; return; }
      say(done.error || 'That passkey did not work here. Try again or use your password.');
    } catch (e) {
      say('Could not reach this kitchen. Check the connection and try again.');
    } finally {
      btn.disabled = false;
    }
  });
})();
