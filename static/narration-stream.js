// ── Narration playback ────────────────────────────────────────────────────
// /api/tts/synthesize returns ONE complete WAV file (whole text synthesized
// and concatenated on the server). We fetch the full body, decode it as a
// single AudioBuffer, and play it as one contiguous AudioBufferSourceNode.
// Decoding once (rather than re-wrapping streamed micro-frames in WAV headers
// and scheduling them back-to-back) avoids the seam/header artifacts that
// showed up as crackling/static. Behavioral contract with callers:
//
//   createNarrationStream() -> {
//     play(body) : POST /api/tts/synthesize then play the returned audio.
//                  Resolves once playback has been scheduled to start;
//                  rejects on pre-playback failure.
//     stop()     : stop playback and disconnect the current stream.
//     onDone     : callback fired when playback has finished.
//     onError    : callback fired on transport/decode errors after playback
//                  has started.
//   }
//
// Only one instance plays at a time; the module keeps a singleton so starting
// a new narration stops any that is currently playing.
(function (global) {
  'use strict';

  var SAMPLE_RATE = 24000; // matches the backend's linear16 sample rate

  var activePlayer = null;

  // Build a 44-byte WAV header around raw linear16 PCM in case the response is
  // raw PCM (no container). If the payload is already a complete WAV (starts
  // with RIFF/WAVE), we pass it through untouched.
  function buildWavHeader(dataLength, sampleRate) {
    var buffer = new ArrayBuffer(44);
    var view = new DataView(buffer);
    function writeString(offset, str) {
      for (var i = 0; i < str.length; i++) {
        view.setUint8(offset + i, str.charCodeAt(i));
      }
    }
    var numChannels = 1;
    var bitsPerSample = 16;
    var byteRate = sampleRate * numChannels * (bitsPerSample / 8);
    var blockAlign = numChannels * (bitsPerSample / 8);

    writeString(0, 'RIFF');
    view.setUint32(4, 36 + dataLength, true);
    writeString(8, 'WAVE');
    writeString(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);          // PCM
    view.setUint16(22, numChannels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, byteRate, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, bitsPerSample, true);
    writeString(36, 'data');
    view.setUint32(40, dataLength, true);
    return buffer;
  }

  function toWavIfNeeded(bytes) {
    var arr = new Uint8Array(bytes);
    var isWav = arr.length > 12 &&
      arr[0] === 0x52 && arr[1] === 0x49 && arr[2] === 0x46 && arr[3] === 0x46 &&
      arr[8] === 0x57 && arr[9] === 0x41 && arr[10] === 0x56 && arr[11] === 0x45;
    if (isWav) return bytes;
    var wav = new Uint8Array(44 + arr.length);
    wav.set(new Uint8Array(buildWavHeader(arr.length, SAMPLE_RATE)), 0);
    wav.set(arr, 44);
    return wav.buffer;
  }

  function createNarrationStream() {
    var ctx = null;
    var stopped = false;
    var player;

    function stopStream() {
      stopped = true;
      if (ctx) {
        try { ctx.close(); } catch (e) {}
        ctx = null;
      }
      if (activePlayer === player) activePlayer = null;
    }

    function initiateCtx() {
      if (!ctx) {
        var AC = global.AudioContext || global.webkitAudioContext;
        if (!AC) throw new Error('Web Audio not supported in this browser');
        ctx = new AC();
      }
      return ctx;
    }

    function play(body) {
      stopStream();

      if (activePlayer && activePlayer !== player) {
        activePlayer.stop();
      }
      activePlayer = player;
      stopped = false;

      var audioCtx;
      try {
        audioCtx = initiateCtx();
        if (audioCtx.state === 'suspended') audioCtx.resume().catch(function () {});
      } catch (e) {
        return Promise.reject(e);
      }

      var gotStarted = false;
      var startedResolve, startedReject;
      var startedPromise = new Promise(function (res, rej) {
        startedResolve = res;
        startedReject = rej;
      });

      function fail(err) {
        if (stopped) return;
        if (gotStarted) {
          if (player.onError) player.onError(err);
        } else {
          gotStarted = true;
          startedReject(err);
        }
      }

      function playBuffer(buf) {
        if (stopped) return;
        var src = audioCtx.createBufferSource();
        src.buffer = buf;
        src.connect(audioCtx.destination);
        src.onended = function () {
          if (!stopped && player.onDone) player.onDone();
        };
        src.start(0);
      }

      fetch('/api/tts/synthesize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      })
        .then(function (r) {
          if (!r.ok) {
            return r.json().then(function (e) {
              throw new Error(e.error || 'Narration failed');
            });
          }
          return r.arrayBuffer();
        })
        .then(function (buffer) {
          if (stopped) return;
          if (!buffer || buffer.byteLength === 0) {
            fail(new Error('No audio received'));
            return;
          }
          var wav = toWavIfNeeded(buffer);
          return audioCtx.decodeAudioData(wav).then(function (decoded) {
            if (stopped) return;
            if (!gotStarted) { gotStarted = true; startedResolve(true); }
            playBuffer(decoded);
          });
        })
        .catch(function (err) { fail(err); });

      return startedPromise;
    }

    player = {
      play: play,
      stop: stopStream,
      onDone: null,
      onError: null
    };

    return player;
  }

  global.createNarrationStream = createNarrationStream;
})(window);
