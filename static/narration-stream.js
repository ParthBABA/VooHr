// ── Low-latency streaming narration playback ─────────────────────────────
// The /api/tts/synthesize endpoint streams raw linear16 PCM audio chunks
// (audio/wav). Instead of waiting for the whole response and loading it into
// an <audio> element, we decode each arriving chunk as a WAV and schedule it
// on a Web Audio AudioContext so playback starts as soon as the first chunk
// lands. Behavioral contract with callers:
//
//   createNarrationStream() -> {
//     play(body) : POST /api/tts/synthesize then stream-play. Resolves once
//                  the first audio chunk has been decoded and scheduled for
//                  playback; rejects on pre-playback failure.
//     stop()     : stop playback and disconnect the current stream.
//     onDone     : callback fired when all scheduled audio has finished.
//     onError    : callback fired on transport/decode errors after playback
//                  has started.
//   }
//
// Only one instance plays at a time; the module keeps a singleton so starting
// a new narration stops any that is currently playing.
(function (global) {
  'use strict';

  var SAMPLE_RATE = 24000; // matches Deepgram streaming default used by backend

  var activePlayer = null;

  // Build a minimal 44-byte WAV header around raw linear16 PCM so the browser
  // can decode the chunk via decodeAudioData without a full file container.
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

  function decodeChunk(pcmBytes, sampleRate, ctx) {
    var pcm = new Uint8Array(pcmBytes);
    var wav = new Uint8Array(44 + pcm.length);
    wav.set(new Uint8Array(buildWavHeader(pcm.length, sampleRate)), 0);
    wav.set(pcm, 44);
    return ctx.decodeAudioData(wav.buffer);
  }

  function createNarrationStream() {
    var ctx = null;
    var stopped = false;
    var reader = null;
    var runId = 0;
    var player;

    function stopStream() {
      stopped = true;
      runId++;
      if (reader) { try { reader.cancel(); } catch (e) {} reader = null; }
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
      // Always stop any in-flight narration before starting a new stream so
      // audio never overlaps even when the same player is reused.
      stopStream();

      if (activePlayer && activePlayer !== player) {
        activePlayer.stop();
      }
      activePlayer = player;
      stopped = false;

      var audioCtx;
      try {
        audioCtx = initiateCtx();
        // AudioContext starts suspended until a user gesture; resume when able.
        if (audioCtx.state === 'suspended') audioCtx.resume().catch(function () {});
      } catch (e) {
        return Promise.reject(e);
      }

      var myRun = runId;
      var startedResolve, startedReject;
      var startedPromise = new Promise(function (res, rej) {
        startedResolve = res;
        startedReject = rej;
      });
      var gotStarted = false;

      function fail(err) {
        if (myRun !== runId || stopped) return; // stale run: ignore
        if (gotStarted) {
          if (player.onError) player.onError(err);
        } else {
          gotStarted = true;
          startedReject(err);
        }
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
          if (!r.body || !r.body.getReader) {
            throw new Error('Streaming not supported by this browser');
          }
          reader = r.body.getReader();
          return pumpChunks(reader, audioCtx, player, myRun, function onFirstBuffer() {
            if (myRun !== runId || stopped) return;
            if (!gotStarted) { gotStarted = true; startedResolve(true); }
          }, function onEmpty() {
            fail(new Error('No audio received'));
          }, fail);
        })
        .catch(function (err) { fail(err); });

      return startedPromise;
    }

    // Read chunks from the response body, decode each into an AudioBuffer,
    // and schedule them contiguously on the audio context. Each chunk is
    // played back-to-back so there are no gaps between the streamed frames.
    function pumpChunks(rdr, audioCtx, plr, myRun, onFirstBuffer, onEmpty, onStreamError) {
      var queueTime = 0;
      var first = true;
      var pending = 0;

      function isStale() { return stopped || myRun !== runId; }

      function schedule(buf) {
        if (isStale()) return;
        var now = audioCtx.currentTime;
        if (first) {
          queueTime = now + 0.02; // ~20ms lead-in
          first = false;
        }
        var src = audioCtx.createBufferSource();
        src.buffer = buf;
        src.connect(audioCtx.destination);
        src.start(queueTime);
        queueTime += buf.duration;
        pending++;
        src.onended = function () {
          pending--;
          if (pending <= 0 && !isStale() && plr.onDone) plr.onDone();
        };
      }

      function pump() {
        if (isStale()) return Promise.resolve();
        return rdr.read().then(function (result) {
          if (isStale()) return;
          if (result.done) {
            if (first) onEmpty();
            return;
          }
          var bytes = result.value;
          if (bytes && bytes.byteLength) {
            return decodeChunk(bytes, SAMPLE_RATE, audioCtx).then(function (buf) {
              if (isStale()) return;
              schedule(buf);
              onFirstBuffer();
              return pump();
            });
          }
          return pump();
        });
      }

      return pump().catch(function (err) {
        if (isStale()) return;
        if (onStreamError) onStreamError(err);
      });
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