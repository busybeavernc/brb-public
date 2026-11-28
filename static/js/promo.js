/* BRB promo video: phones get the vertical cut, wider screens the 16:9 cut.
   Loads nothing until the video is near the screen; plays muted when data-autoplay is set,
   pauses off-screen, and the sound button restarts it from the top with sound. */
(function () {
  var TALL = window.matchMedia('(max-width: 760px)');
  var still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  document.querySelectorAll('[data-promo]').forEach(function (box) {
    var v = box.querySelector('video');
    var btn = box.querySelector('.promo-sound');
    var play = box.querySelector('.promo-play');
    var tall = TALL.matches;
    var auto = box.hasAttribute('data-autoplay') && !still;
    var loaded = false;

    box.classList.add(tall ? 'is-tall' : 'is-wide');
    v.poster = box.getAttribute(tall ? 'data-poster-tall' : 'data-poster-wide');

    function load() {
      if (loaded) return;
      loaded = true;
      var kind = tall ? 'tall' : 'wide';
      ['webm', 'mp4'].forEach(function (ext) {
        var src = box.getAttribute('data-' + kind + '-' + ext);
        if (!src) return;
        var s = document.createElement('source');
        s.src = src; s.type = 'video/' + ext;
        v.appendChild(s);
      });
      v.load();
    }

    function setSound(on) {
      v.muted = !on;
      box.classList.toggle('has-sound', on);
      if (btn) {
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        btn.querySelector('span').textContent = on ? 'Mute' : (tall ? 'Tap for sound' : 'Play with sound');
      }
    }

    function startWithSound() {
      load();
      setSound(true);
      v.currentTime = 0;
      box.classList.add('started');
      v.play().catch(function () {});
    }

    if (btn) btn.addEventListener('click', function () {
      if (v.muted || v.paused) startWithSound();
      else setSound(false);
    });
    if (play) play.addEventListener('click', startWithSound);
    v.addEventListener('click', function () {
      if (!box.classList.contains('started')) { startWithSound(); return; }
      if (v.paused) v.play().catch(function () {}); else v.pause();
    });
    setSound(false);

    if (!('IntersectionObserver' in window)) { if (auto) { load(); box.classList.add('started'); v.play().catch(function () {}); } return; }
    new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          if (auto) {
            load();
            box.classList.add('started');
            v.play().catch(function () {});
          }
        } else if (!v.paused) {
          v.pause();
        }
      });
    }, { rootMargin: '200px 0px', threshold: 0.25 }).observe(box);
  });
})();
