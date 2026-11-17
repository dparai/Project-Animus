/* Settings page: add a passkey, and turn this device's reminder notifications on or off. */
(function () {
  var page = document.getElementById('settings');
  if (!page) { return; }
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  var api = window.AnimusPasskey;

  // ---- passkeys ----
  var add = document.getElementById('passkey-add');
  if (add && api && api.supported()) {
    var none = document.getElementById('passkey-none');
    var note = document.getElementById('passkey-msg');
    add.hidden = false; if (none) { none.hidden = true; }
    add.addEventListener('click', function () {
      add.disabled = true; note.hidden = true;
      api.send('/settings/passkeys/options', {}, csrf)
        .then(function (r) { return r.json().then(function (j) { if (!r.ok) { throw new Error(j.message || 'x'); } return j; }); })
        .then(function (j) {
          return api.create(j.options).then(function (credential) {
            return api.send('/settings/passkeys/verify', { handle: j.handle, credential: credential }, csrf);
          });
        })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (j && j.ok) { window.location.replace('/settings?done=passkey'); return; }
          throw new Error((j && j.message) || 'x');
        })
        .catch(function (err) {
          add.disabled = false;
          if (err && err.name === 'NotAllowedError') { return; }  // cancelled
          note.textContent = (err && err.message && err.message !== 'x') ? err.message : 'That didn\'t work. Try again.';
          note.hidden = false;
        });
    });
  }

  // ---- reminder notifications on this device ----
  var on = document.getElementById('push-on');
  var off = document.getElementById('push-off');
  var test = document.getElementById('push-test');
  var hint = document.getElementById('push-note');
  var key = page.getAttribute('data-push-key');
  if (!on || !key) { return; }
  if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
    return;  // the hint about the Home Screen stays visible
  }
  function bytes(text) {
    var s = text.replace(/-/g, '+').replace(/_/g, '/');
    while (s.length % 4) { s += '='; }
    var raw = atob(s), out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) { out[i] = raw.charCodeAt(i); }
    return out;
  }
  function say(text) { hint.textContent = text; }
  function state(active) { on.hidden = active; off.hidden = !active; test.hidden = !active; }
  function tell(endpoint) { return api.send('/push/subscribe', { endpoint: endpoint }, csrf); }

  navigator.serviceWorker.ready.then(function (reg) {
    reg.pushManager.getSubscription().then(function (sub) {
      var known = page.getAttribute('data-subscribed') === '1';
      if (sub && !known) { tell(sub.endpoint); }  // the server forgot this device: tell it again
      state(!!sub);
      say(sub ? 'Notifications are on for this device.' : 'Notifications are off for this device.');
    });
    on.addEventListener('click', function () {
      on.disabled = true;
      Notification.requestPermission().then(function (answer) {
        if (answer !== 'granted') { throw new Error('denied'); }
        return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: bytes(key) });
      }).then(function (sub) {
        return tell(sub.endpoint).then(function (r) { if (!r.ok) { sub.unsubscribe(); throw new Error('refused'); } });
      }).then(function () {
        on.disabled = false; state(true); say('Notifications are on for this device.');
      }).catch(function (err) {
        on.disabled = false;
        say(err && err.message === 'denied' ? 'Notifications are blocked for Animus in this browser\'s settings.' : 'That didn\'t work on this device.');
      });
    });
    off.addEventListener('click', function () {
      reg.pushManager.getSubscription().then(function (sub) { return sub ? sub.unsubscribe() : null; })
        .then(function () { return api.send('/push/unsubscribe', {}, csrf); })
        .then(function () { state(false); say('Notifications are off for this device.'); });
    });
    test.addEventListener('click', function () {
      test.disabled = true;
      api.send('/push/test', {}, csrf).then(function (r) { return r.json(); }).then(function (j) {
        test.disabled = false;
        say(j && j.sent ? 'Sent. It should arrive in a few seconds.' : 'The test could not be sent. Turn notifications off and on again.');
      }).catch(function () { test.disabled = false; });
    });
  });
})();
