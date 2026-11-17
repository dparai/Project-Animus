/* App lock, page side.

   The server decides whether this device is locked; this script keeps it informed and shows the
   unlock sheet over the page, so anything typed stays where it is.
   - While the page is visible it pings the server (that is what "in use" means).
   - When the page goes to the background it is blurred at once; when it comes back after the
     chosen time (or straight away, for "At once"), the sheet closes over it and the server locks.
   - Any request the server refuses with 423 also brings the sheet up. */
(function () {
  var el = document.getElementById('lock');
  if (!el) { return; }
  var root = document.documentElement;
  var after = parseInt(el.getAttribute('data-after'), 10) || 0;
  var every = (parseInt(el.getAttribute('data-ping'), 10) || 30) * 1000;
  var standalone = el.getAttribute('data-standalone') === '1';
  var next = el.getAttribute('data-next') || '/';
  var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  var form = document.getElementById('lock-form');
  var field = document.getElementById('lock-password');
  var msg = document.getElementById('lock-msg');
  var keyButton = document.getElementById('lock-passkey');
  var keyOr = document.getElementById('lock-or');
  var passkey = window.AnimusPasskey;
  var canKey = !!(keyButton && passkey && passkey.supported());
  var hiddenAt = 0, busy = false, shown = standalone;

  function post(url, body) {
    return fetch(url, {
      method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'X-CSRF-Token': csrf, 'X-Requested-With': 'fetch', 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body || ''
    });
  }
  function say(text) { msg.textContent = text || ''; msg.hidden = !text; }
  function others(inert) {
    Array.prototype.forEach.call(document.body.children, function (node) {
      if (node !== el && node.tagName !== 'SCRIPT') { if (inert) { node.setAttribute('inert', ''); } else { node.removeAttribute('inert'); } }
    });
  }
  function show(tryKey) {
    if (!shown) {
      shown = true;
      var open = document.querySelector('dialog[open]');
      if (open && open.close) { open.close(); }
      el.hidden = false; root.classList.add('is-locked'); others(true); say('');
    }
    if (canKey && tryKey) { withKey(); } else if (!canKey) { field.focus(); }
  }
  function done() {
    if (standalone) { window.location.replace(next); return; }
    shown = false; el.hidden = true; root.classList.remove('is-locked'); others(false);
    field.value = ''; say(''); ping();
  }
  function lockNow(tryKey) { post('/lock').catch(function () {}); show(tryKey); }
  function ping() {
    if (shown || document.visibilityState !== 'visible') { return; }
    post('/lock/ping').then(function (r) { if (r.status === 423) { show(true); } }).catch(function () {});
  }
  function withKey() {
    if (busy) { return; }
    busy = true; say('');
    passkey.check('/unlock/passkey', csrf)
      .then(function (j) { busy = false; if (j && j.ok) { done(); } else { say((j && j.message) || 'That didn\'t work. Try again or use your password.'); } })
      .catch(function () { busy = false; });  // cancelled or not available: the password still works
  }

  if (canKey) {
    keyButton.hidden = false; if (keyOr) { keyOr.hidden = false; }
    keyButton.addEventListener('click', withKey);
    field.required = false;
  }
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (!field.value) { field.focus(); return; }
    var button = document.getElementById('lock-submit');
    button.disabled = true; say('');
    post('/unlock', 'password=' + encodeURIComponent(field.value))
      .then(function (r) { return r.json().catch(function () { return { ok: false }; }); })
      .then(function (j) {
        button.disabled = false;
        if (j && j.ok) { done(); } else { field.value = ''; field.focus(); say((j && j.message) || 'That didn\'t work.'); }
      })
      .catch(function () { button.disabled = false; say('No connection. Try again.'); });
  });

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') {
      if (busy) { return; }  // the phone's own fingerprint sheet is in front
      hiddenAt = Date.now();
      root.classList.add('is-veiled');
      if (after === 0 && !shown) { lockNow(false); }
    } else {
      root.classList.remove('is-veiled');
      if (!shown && hiddenAt && Date.now() - hiddenAt >= after * 1000) { lockNow(true); }
      else if (shown && canKey && !busy) { withKey(); }
      else { ping(); }
      hiddenAt = 0;
    }
  });
  window.addEventListener('pagehide', function () { root.classList.add('is-veiled'); });
  window.addEventListener('pageshow', function () { root.classList.remove('is-veiled'); });
  // Any refused request (a save, a search, a page part) means the server has locked this device.
  document.addEventListener('htmx:afterRequest', function (e) {
    if (e.detail && e.detail.xhr && e.detail.xhr.status === 423) { show(true); }
  });

  if (standalone) { others(false); if (canKey) { withKey(); } else { field.focus(); } }
  else { ping(); setInterval(ping, every); }
})();
