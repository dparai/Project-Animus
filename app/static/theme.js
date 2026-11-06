/* Colours that follow the day: the glow shifts with the hour and, gently, with your latest mood. */
(function () {
  var root = document.documentElement;
  var SLOTS = {
    morning: ['#ffb86b', '#ff8a5c'],
    day: ['#ffa24d', '#ff6a4d'],
    evening: ['#ff7b54', '#c25aa6'],
    night: ['#d98a35', '#5a6bd8']
  };
  var mood = '';
  function slot(h) {
    if (h >= 5 && h < 10) { return 'morning'; }
    if (h >= 10 && h < 17) { return 'day'; }
    if (h >= 17 && h < 21) { return 'evening'; }
    return 'night';
  }
  function apply() {
    var name = slot(new Date().getHours());
    var pair = SLOTS[name];
    var a = pair[0], b = pair[1], o = '0.42';
    if (mood === 'rough' || mood === 'low' || mood === 'sad') { b = '#6a78d8'; o = '0.34'; }
    else if (mood === 'good') { o = '0.5'; }
    root.style.setProperty('--glow-a', a);
    root.style.setProperty('--glow-b', b);
    root.style.setProperty('--glow-o', o);
    root.setAttribute('data-slot', name);
  }
  function cached() {
    try {
      var raw = sessionStorage.getItem('animus-mood');
      if (!raw) { return null; }
      var v = JSON.parse(raw);
      return v && Date.now() - v.t < 30 * 60 * 1000 ? v.m : null;
    } catch (e) { return null; }
  }
  var known = cached();
  if (known !== null) { mood = known; }
  apply();
  if (known === null && window.fetch) {
    fetch('/theme-state', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (j) {
        mood = (j && typeof j.mood === 'string') ? j.mood : '';
        try { sessionStorage.setItem('animus-mood', JSON.stringify({ m: mood, t: Date.now() })); } catch (e) {}
        apply();
      })
      .catch(function () {});
  }
  setInterval(apply, 10 * 60 * 1000);
})();
