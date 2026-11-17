/* Passkey helpers: turn the server's options into what the browser wants and the browser's
   answer into plain JSON. No data is kept here. */
(function () {
  function toBytes(text) {
    var s = text.replace(/-/g, '+').replace(/_/g, '/');
    while (s.length % 4) { s += '='; }
    var raw = atob(s), out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) { out[i] = raw.charCodeAt(i); }
    return out.buffer;
  }
  function toText(buffer) {
    var bytes = new Uint8Array(buffer), raw = '';
    for (var i = 0; i < bytes.length; i++) { raw += String.fromCharCode(bytes[i]); }
    return btoa(raw).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function ids(list) {
    return (list || []).map(function (c) { return { type: c.type, id: toBytes(c.id), transports: c.transports }; });
  }
  function supported() {
    return !!(window.PublicKeyCredential && navigator.credentials && navigator.credentials.create);
  }
  function create(options) {
    var o = Object.assign({}, options);
    o.challenge = toBytes(options.challenge);
    o.user = Object.assign({}, options.user, { id: toBytes(options.user.id) });
    o.excludeCredentials = ids(options.excludeCredentials);
    return navigator.credentials.create({ publicKey: o }).then(function (c) {
      var r = c.response;
      return {
        id: c.id, rawId: toText(c.rawId), type: c.type,
        authenticatorAttachment: c.authenticatorAttachment || undefined,
        clientExtensionResults: {},
        response: {
          clientDataJSON: toText(r.clientDataJSON), attestationObject: toText(r.attestationObject),
          transports: r.getTransports ? r.getTransports() : undefined
        }
      };
    });
  }
  function get(options) {
    var o = Object.assign({}, options);
    o.challenge = toBytes(options.challenge);
    o.allowCredentials = ids(options.allowCredentials);
    return navigator.credentials.get({ publicKey: o }).then(function (c) {
      var r = c.response;
      return {
        id: c.id, rawId: toText(c.rawId), type: c.type,
        authenticatorAttachment: c.authenticatorAttachment || undefined,
        clientExtensionResults: {},
        response: {
          clientDataJSON: toText(r.clientDataJSON), authenticatorData: toText(r.authenticatorData),
          signature: toText(r.signature), userHandle: r.userHandle ? toText(r.userHandle) : undefined
        }
      };
    });
  }
  function send(url, data, csrf) {
    var headers = { 'Content-Type': 'application/json', 'X-Requested-With': 'fetch' };
    if (csrf) { headers['X-CSRF-Token'] = csrf; }
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: headers, body: JSON.stringify(data || {}) });
  }
  /* One whole check: ask the server for a challenge, let the phone sign it, send it back.
     Resolves with the server's JSON answer ({ok: true, ...} on success). */
  function check(base, csrf, extra) {
    return send(base + '/options', {}, csrf)
      .then(function (r) { if (!r.ok) { throw new Error('unavailable'); } return r.json(); })
      .then(function (j) {
        return get(j.options).then(function (credential) {
          return send(base + '/verify', Object.assign({ handle: j.handle, credential: credential }, extra || {}), csrf);
        });
      })
      .then(function (r) { return r.json().catch(function () { return { ok: false }; }); });
  }
  window.AnimusPasskey = { supported: supported, create: create, get: get, send: send, check: check };
})();
