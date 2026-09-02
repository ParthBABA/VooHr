// ── AI Narration mini-player ───────────────────────────────────────────────
// Renders a small, native-quality <audio>-based player inside the section's
// .wsx-narr control (replacing the mic button) whenever the user picks a
// language. The backend returns ONE complete WAV for the whole text, so we
// load it as a Blob into an <audio> element and let the browser decode/play
// it natively — no Web Audio re-wrapping, hence clean playback.
//
// Behavioral contract with callers:
//
//   createNarrationStream() -> {
//     play(body, wrap) : fetch /api/tts/synthesize and show the mini-player
//                        inside `wrap` (a .wsx-narr element). Resolves once
//                        the audio is loaded and playing; rejects on failure.
//     stop()           : stop playback, hide the mini-player, restore the
//                        default mic-button control for the active wrap.
//     onDone           : callback fired when playback finishes naturally.
//     onError          : callback fired on transport/decode errors.
//   }
//
// Only one mini-player shows at a time (module singleton), so starting a new
// narration closes any that is currently open.
(function (global) {
  'use strict';

  var activePlayer = null;
  var activeWrap = null;

  function fmtTime(sec) {
    if (!isFinite(sec) || sec < 0) sec = 0;
    var m = Math.floor(sec / 60);
    var s = Math.floor(sec % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function iconPlay() {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '12');
    svg.setAttribute('height', '12');
    svg.setAttribute('fill', 'currentColor');
    var p = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    p.setAttribute('points', '6 3 20 12 6 21 6 3');
    svg.appendChild(p);
    return svg;
  }

  function iconPause() {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '12');
    svg.setAttribute('height', '12');
    svg.setAttribute('fill', 'currentColor');
    var r1 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    r1.setAttribute('x', '5'); r1.setAttribute('y', '4');
    r1.setAttribute('width', '5'); r1.setAttribute('height', '16');
    r1.setAttribute('rx', '1');
    var r2 = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    r2.setAttribute('x', '14'); r2.setAttribute('y', '4');
    r2.setAttribute('width', '5'); r2.setAttribute('height', '16');
    r2.setAttribute('rx', '1');
    svg.appendChild(r1); svg.appendChild(r2);
    return svg;
  }

  function iconClose() {
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '12');
    svg.setAttribute('height', '12');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    var l1 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    l1.setAttribute('x1', '6'); l1.setAttribute('y1', '6');
    l1.setAttribute('x2', '18'); l1.setAttribute('y2', '18');
    var l2 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    l2.setAttribute('x1', '18'); l2.setAttribute('y1', '6');
    l2.setAttribute('x2', '6'); l2.setAttribute('y2', '18');
    svg.appendChild(l1); svg.appendChild(l2);
    return svg;
  }

  function createNarrationStream() {
    var player;
    var objUrl = null;
    var audio = null;
    var root = null;

    function cleanup() {
      if (audio) {
        try { audio.pause(); } catch (e) {}
        try { audio.removeAttribute('src'); audio.load(); } catch (e) {}
        audio = null;
      }
      if (objUrl) { try { URL.revokeObjectURL(objUrl); } catch (e) {} objUrl = null; }
      if (root && root.parentNode) { root.parentNode.removeChild(root); }
      root = null;
    }

    function restoreWrap() {
      if (activeWrap) {
        var btn = activeWrap.querySelector('.wsx-narr-btn');
        var sel = activeWrap.querySelector('.wsx-narr-select');
        if (btn) btn.style.display = '';
        if (sel) sel.style.display = '';
        activeWrap = null;
      }
    }

    function stopPlayback() {
      if (activePlayer !== player) return;
      cleanup();
      restoreWrap();
      activePlayer = null;
    }

    function play(body, wrapEl) {
      // Close any narration that is currently open first.
      if (activePlayer && activePlayer !== player) activePlayer.stop();
      if (activePlayer === player) stopPlayback();

      var wrap = wrapEl || null;
      if (!wrap || !wrap.querySelector('.wsx-narr-btn')) {
        return Promise.reject(new Error('Narration target not found'));
      }

      // Swap the mic button + dropdown for the mini-player.
      var btn = wrap.querySelector('.wsx-narr-btn');
      var sel = wrap.querySelector('.wsx-narr-select');
      btn.style.display = 'none';
      sel.style.display = 'none';

      activeWrap = wrap;
      activePlayer = player;

      root = document.createElement('div');
      root.className = 'wsx-mini-player';

      var playBtn = document.createElement('button');
      playBtn.type = 'button';
      playBtn.className = 'icon-btn wsx-mp-play';
      playBtn.title = 'Play / Pause';
      playBtn.appendChild(iconPlay());

      var range = document.createElement('input');
      range.type = 'range';
      range.className = 'wsx-mp-range';
      range.min = '0';
      range.max = '1000';
      range.value = '0';
      range.step = '1';
      range.title = 'Seek';

      var timeEl = document.createElement('span');
      timeEl.className = 'wsx-mp-time';
      timeEl.textContent = '0:00';

      var cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'icon-btn wsx-mp-cancel';
      cancelBtn.title = 'Close';
      cancelBtn.appendChild(iconClose());

      root.appendChild(playBtn);
      root.appendChild(range);
      root.appendChild(timeEl);
      root.appendChild(cancelBtn);
      wrap.appendChild(root);

      audio = document.createElement('audio');
      audio.preload = 'auto';

      var seeking = false;
      range.addEventListener('input', function () {
        if (!audio || !audio.duration) return;
        seeking = true;
        var t = (parseFloat(range.value) / 1000) * audio.duration;
        try { audio.currentTime = t; } catch (e) {}
      });
      range.addEventListener('change', function () { seeking = false; });
      audio.addEventListener('timeupdate', function () {
        if (seeking || !audio.duration) return;
        range.value = String((audio.currentTime / audio.duration) * 1000);
        timeEl.textContent = fmtTime(audio.currentTime) + ' / ' + fmtTime(audio.duration);
      });
      audio.addEventListener('loadedmetadata', function () {
        timeEl.textContent = '0:00 / ' + fmtTime(audio.duration);
      });
      audio.addEventListener('play', function () {
        playBtn.replaceChild(iconPause(), playBtn.firstChild);
      });
      audio.addEventListener('pause', function () {
        playBtn.replaceChild(iconPlay(), playBtn.firstChild);
      });
      audio.addEventListener('ended', function () {
        range.value = '1000';
        timeEl.textContent = fmtTime(audio.duration) + ' / ' + fmtTime(audio.duration);
        playBtn.replaceChild(iconPlay(), playBtn.firstChild);
        if (player.onDone) player.onDone();
        stopPlayback();
      });
      audio.addEventListener('error', function () {
        if (player.onError) player.onError(new Error('Audio playback failed'));
        stopPlayback();
      });

      playBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        if (audio.paused) {
          audio.play().catch(function () {});
        } else {
          audio.pause();
        }
      });

      cancelBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        stopPlayback();
      });

      return fetch('/api/tts/synthesize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
        .then(function (r) {
          if (!r.ok) {
            return r.json().then(function (err) {
              throw new Error(err.error || 'Narration failed');
            });
          }
          return r.blob();
        })
        .then(function (blob) {
          if (activePlayer !== player) throw new Error('Playback stopped');
          if (!blob || blob.size === 0) throw new Error('No audio received');
          objUrl = URL.createObjectURL(blob);
          audio.src = objUrl;
          audio.load();
          // Play right away (tolerate autoplay-block by leaving it to the
          // play/pause button rather than tearing the player down).
          audio.play().catch(function () {});
        })
        .catch(function (err) {
          stopPlayback();
          if (player.onError) player.onError(err);
          throw err;
        });
    }

    player = {
      play: play,
      stop: stopPlayback,
      onDone: null,
      onError: null
    };

    return player;
  }

  global.createNarrationStream = createNarrationStream;
})(window);
