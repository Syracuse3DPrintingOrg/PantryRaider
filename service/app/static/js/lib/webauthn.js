// Passkey helpers shared by the sign-in page and Settings, Security.
//
// The server sends WebAuthn options as JSON with base64url byte fields
// (py_webauthn's options_to_json). Newer browsers turn those into the
// objects navigator.credentials wants with parse*OptionsFromJSON and back
// with toJSON(); the fallbacks below do the same by hand for older ones.

window.PRPasskey = (function () {
  function supported() {
    return !!(window.isSecureContext && window.PublicKeyCredential &&
              navigator.credentials && navigator.credentials.get);
  }

  function b64uToBuf(s) {
    s = String(s).replace(/-/g, '+').replace(/_/g, '/');
    while (s.length % 4) s += '=';
    var bin = atob(s), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }

  function bufToB64u(buf) {
    var bytes = new Uint8Array(buf), bin = '';
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function descriptors(list) {
    return (list || []).map(function (d) {
      return Object.assign({}, d, {id: b64uToBuf(d.id)});
    });
  }

  function creationOptions(o) {
    if (PublicKeyCredential.parseCreationOptionsFromJSON) {
      return PublicKeyCredential.parseCreationOptionsFromJSON(o);
    }
    return Object.assign({}, o, {
      challenge: b64uToBuf(o.challenge),
      user: Object.assign({}, o.user, {id: b64uToBuf(o.user.id)}),
      excludeCredentials: descriptors(o.excludeCredentials),
    });
  }

  function requestOptions(o) {
    if (PublicKeyCredential.parseRequestOptionsFromJSON) {
      return PublicKeyCredential.parseRequestOptionsFromJSON(o);
    }
    return Object.assign({}, o, {
      challenge: b64uToBuf(o.challenge),
      allowCredentials: descriptors(o.allowCredentials),
    });
  }

  function toJSON(cred) {
    if (typeof cred.toJSON === 'function') {
      try { return cred.toJSON(); } catch (e) { /* fall through */ }
    }
    var r = cred.response, resp = {clientDataJSON: bufToB64u(r.clientDataJSON)};
    if (r.attestationObject) {
      resp.attestationObject = bufToB64u(r.attestationObject);
      if (r.getTransports) resp.transports = r.getTransports();
    } else {
      resp.authenticatorData = bufToB64u(r.authenticatorData);
      resp.signature = bufToB64u(r.signature);
      if (r.userHandle) resp.userHandle = bufToB64u(r.userHandle);
    }
    return {id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
            response: resp, clientExtensionResults: cred.getClientExtensionResults(),
            authenticatorAttachment: cred.authenticatorAttachment || undefined};
  }

  // A cancelled or timed-out prompt is not an error worth a red message.
  function cancelled(e) {
    return e && (e.name === 'NotAllowedError' || e.name === 'AbortError');
  }

  async function create(options) {
    return toJSON(await navigator.credentials.create({publicKey: creationOptions(options)}));
  }

  async function get(options) {
    return toJSON(await navigator.credentials.get({publicKey: requestOptions(options)}));
  }

  return {supported: supported, create: create, get: get, cancelled: cancelled};
})();
