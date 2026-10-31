/* The browser microphone: records clips, keeps a live level, keeps the screen
 * awake, and comes back after a call or an app switch takes the microphone.
 *
 * Plain ES5 with no build step, like display.html, so any page can load it
 * with <script src="mic.js">. What a page does with a clip is its own business:
 *
 *   var mic = ScrobsterMic.create({
 *     onClip: function (blob) { return ScrobsterMic.upload("api/match", blob); },
 *     onLevel: function (db) {},            // peak dBFS each frame, null when off
 *     onState: function (state, info) {},   // see STATES below
 *     onDevices: function (list, currentId) {}
 *   });
 *   mic.start();   // from a tap or click: the browser asks for permission then
 *   mic.stop();
 *
 * onClip returns a promise. Its rejection decides the next step: an error
 * with `status` 401 stops the microphone, 429 waits `retryAfter` seconds, and
 * a network error or a 5xx waits longer each time. ScrobsterMic.upload()
 * makes such errors.
 *
 * STATES, with what `info` holds:
 *   starting      asking for the microphone
 *   listening     recording; {recovered: true} when it came back by itself
 *   sending       a clip is with onClip
 *   interrupted   the microphone was taken away; {error} when reopening failed
 *   waiting       not recording for a while; {reason: "busy"|"network"|"server",
 *                 seconds, until (ms since 1970), error}
 *   error         one clip failed for another reason; {error}. It goes on.
 *   blocked       permission refused, or no microphone; {error}. It stopped.
 *   signedout     the server answered 401. It stopped.
 *   stopped
 *   wake          {held: true|false, unsupported, error}: the screen lock
 */
(function (root) {
  "use strict";

  var CLIP_MS = 12000, GAP_MS = 3000;
  // Shazam fingerprints 16 kHz mono, and the server decodes to that. Speech
  // codecs at this rate keep what it needs, at a size that is easy to predict.
  var BITS_PER_SECOND = 48000;
  // In order of preference. Chrome and Firefox record Opus, Safari AAC.
  var MIME_TYPES = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus",
                    "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/webm"];
  var FLOOR_DB = -99;
  // Recovering: the first retry, and the longest wait between two.
  var RETRY_MS = 2000, RETRY_MAX_MS = 30000;
  var NETWORK_MAX_S = 60;

  function supported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia
              && root.MediaRecorder);
  }

  function pickMimeType() {
    if (!root.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
    for (var i = 0; i < MIME_TYPES.length; i++) {
      try { if (MediaRecorder.isTypeSupported(MIME_TYPES[i])) return MIME_TYPES[i]; }
      catch (e) { /* an old browser that throws on a type it does not know */ }
    }
    return "";
  }

  // POST one clip. Resolves with the JSON answer. Rejects with an Error that
  // has `status`, `detail` and `retryAfter` (seconds), or `network: true`.
  function upload(url, blob, opts) {
    opts = opts || {};
    var headers = {"content-type": "application/octet-stream"};
    for (var k in opts.headers || {}) headers[k] = opts.headers[k];
    return fetch(url, {method: "POST", credentials: "same-origin", headers: headers, body: blob})
      .then(function (r) {
        if (r.ok) return r.json();
        return r.json().catch(function () { return {}; }).then(function (j) {
          var detail = j && typeof j.detail === "string" ? j.detail : "";
          var err = new Error(detail || "HTTP " + r.status);
          err.status = r.status;
          err.detail = detail;
          var after = parseInt(r.headers.get("retry-after") || "", 10);
          if (!(after > 0)) {
            var said = /in (\d+)\s*s/.exec(detail);
            after = said ? parseInt(said[1], 10) : 0;
          }
          err.retryAfter = after > 0 ? after : null;
          throw err;
        });
      }, function (e) {
        var err = new Error("the server cannot be reached");
        err.network = true;
        err.cause = e;
        throw err;
      });
  }

  function create(opts) {
    opts = opts || {};
    var clipMs = opts.clipMs || CLIP_MS, gapMs = opts.gapMs != null ? opts.gapMs : GAP_MS;
    var storageKey = opts.storageKey === undefined ? "micDeviceId" : opts.storageKey;
    var bitsPerSecond = opts.audioBitsPerSecond || BITS_PER_SECOND;
    var noop = function () {};
    var onClip = opts.onClip || function () { return Promise.resolve(); };
    var onLevel = opts.onLevel || noop, onState = opts.onState || noop;
    var onDevices = opts.onDevices || noop;

    var on = false;
    var session = 0;        // each start() is a new one; old callbacks see a stale number
    var stream = null;
    var opened = false;     // this session has had the microphone once
    var interrupted = false;
    var muteWaited = false;
    var spoiled = false;    // the clip being recorded lost its microphone
    var recorder = null;
    var phase = "idle";     // "busy", or "paused" with a timer (or none, until visible)
    var pauseKind = null, timer = null;
    var retryMs = RETRY_MS, failures = 0;
    var ctx = null, source = null, analyser = null, raf = 0;
    var lock = null, wakeSaid = null;
    var mimeType = pickMimeType();

    function say(state, info) {
      try { onState(state, info || {}); } catch (e) { if (root.console) console.error(e); }
    }

    function track() { return stream && stream.getAudioTracks()[0] || null; }

    // --- the screen ---
    function sayWake(info) {
      var key = String(info.held) + String(!!info.unsupported);
      if (key === wakeSaid) return;
      wakeSaid = key;
      say("wake", info);
    }
    function wake() {
      if (!on || document.hidden || lock) return;
      if (!navigator.wakeLock || !navigator.wakeLock.request) {
        sayWake({held: false, unsupported: true});
        return;
      }
      var mine = session;
      navigator.wakeLock.request("screen").then(function (l) {
        if (mine !== session || !on) { l.release().catch(noop); return; }
        lock = l;
        sayWake({held: true});
        // The browser lets go when the tab is hidden; visibility asks again.
        l.addEventListener("release", function () {
          if (lock === l) lock = null;
          sayWake({held: false});
        });
      }).catch(function (e) { sayWake({held: false, error: e}); });
    }
    function unwake() {
      if (lock) { var l = lock; lock = null; l.release().catch(noop); }
    }

    // --- the meter ---
    function startMeter() {
      stopMeterSource();
      if (!ctx || !stream || !stream.getAudioTracks().length) return;
      try {
        source = ctx.createMediaStreamSource(stream);
        analyser = ctx.createAnalyser();
        analyser.fftSize = 1024;
        // Not connected to the destination, so nothing is played back.
        source.connect(analyser);
      } catch (e) { stopMeterSource(); return; }
      resumeContext();
      var buf = new Float32Array(analyser.fftSize), mine = analyser;
      var tick = function (now) {
        if (analyser !== mine) return;
        // A reader that throws would otherwise leave the last level on screen
        // looking live, which is the one thing a meter must never do.
        try { mine.getFloatTimeDomainData(buf); }
        catch (e) { stopMeterSource(); return; }
        var peak = 0;
        for (var i = 0; i < buf.length; i++) { var a = Math.abs(buf[i]); if (a > peak) peak = a; }
        onLevel(peak > 0 ? Math.max(FLOOR_DB, 20 * Math.log(peak) / Math.LN10) : FLOOR_DB, now);
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    }
    function stopMeterSource() {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      try { if (source) source.disconnect(); } catch (e) {}
      var had = !!analyser;
      source = analyser = null;
      if (had) onLevel(null);
    }
    function resumeContext() {
      // Safari hands back a suspended context, and an "interrupted" one after
      // a call. Resuming is only allowed from a tap or a visible page.
      if (ctx && ctx.state !== "running" && ctx.state !== "closed" && ctx.resume) {
        try { var p = ctx.resume(); if (p && p.catch) p.catch(noop); } catch (e) {}
      }
    }

    // --- the microphone ---
    function constraints(deviceId) {
      // Voice processing strips the detail Shazam fingerprints. Ask for raw audio.
      var audio = {echoCancellation: false, noiseSuppression: false, autoGainControl: false};
      if (deviceId) audio.deviceId = {exact: deviceId};
      return {audio: audio};
    }
    function saved() {
      try { return storageKey ? localStorage.getItem(storageKey) : null; } catch (e) { return null; }
    }
    function open() {
      var wanted = saved();
      var plain = function () { return navigator.mediaDevices.getUserMedia(constraints(null)); };
      if (!wanted) return plain();
      // Unplugged since: fall back to the default.
      return navigator.mediaDevices.getUserMedia(constraints(wanted)).catch(plain);
    }
    function adopt(s) {
      stream = s;
      var t = track(), mine = s;
      if (t) {
        t.addEventListener("ended", function () { if (stream === mine) lost(); });
        t.addEventListener("mute", function () { if (stream === mine) muted(); });
        t.addEventListener("unmute", function () { if (stream === mine) kick(); });
      }
      startMeter();
      listDevices();
    }
    function drop() {
      stopMeterSource();
      if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
      stream = null;
    }
    function spoil() {
      spoiled = true;
      if (recorder && recorder.state !== "inactive") { try { recorder.stop(); } catch (e) {} }
    }
    function sayInterrupted(info) {
      if (interrupted) return;
      interrupted = true;
      say("interrupted", info);
    }
    // The track ended: another app, a call, or the system took the microphone.
    function lost() {
      if (!on) return;
      spoil();
      drop();
      sayInterrupted({});
      kick();
    }
    function muted() {
      if (!on) return;
      spoil();
      sayInterrupted({muted: true});
    }

    function listDevices() {
      if (!navigator.mediaDevices.enumerateDevices) return;
      navigator.mediaDevices.enumerateDevices().then(function (all) {
        // Labels only exist once the browser has permission.
        var list = all.filter(function (d) { return d.kind === "audioinput" && d.label; });
        var t = track(), settings = t && t.getSettings ? t.getSettings() : {};
        onDevices(list, settings.deviceId || null);
      }).catch(noop);
    }

    function recordClip(s) {
      return new Promise(function (resolve, reject) {
        var chunks = [], rec, options = {audioBitsPerSecond: bitsPerSecond};
        if (mimeType) options.mimeType = mimeType;
        try { rec = new MediaRecorder(s, options); }
        catch (e) {
          // A browser that refuses the options still records with its own.
          try { rec = new MediaRecorder(s); } catch (e2) { return reject(e2); }
        }
        recorder = rec;
        rec.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
        rec.onerror = function (e) { reject(e.error || new Error("the recorder failed")); };
        rec.onstop = function () {
          if (recorder === rec) recorder = null;
          resolve(new Blob(chunks, {type: rec.mimeType || mimeType || "audio/webm"}));
        };
        try { rec.start(); } catch (e) { recorder = null; return reject(e); }
        setTimeout(function () { if (rec.state !== "inactive") rec.stop(); }, clipMs);
      });
    }

    // --- the loop ---
    // ms null: until the page is visible again, or something kicks it.
    function later(s, ms, kind) {
      if (s !== session) return;
      phase = "paused";
      pauseKind = kind || "gap";
      clearTimeout(timer);
      timer = ms == null ? null : setTimeout(function () { cycle(s); }, ms);
    }
    // Something changed that might let a paused recovery go on now.
    function kick() {
      if (on && phase === "paused" && pauseKind === "recover") {
        clearTimeout(timer);
        cycle(session);
      }
    }
    function backoff() {
      var ms = retryMs;
      retryMs = Math.min(RETRY_MAX_MS, retryMs * 2);
      return ms;
    }

    function cycle(s) {
      if (s !== session || !on) return;
      phase = "busy";
      clearTimeout(timer);
      var t = track();
      if (t && t.readyState === "live" && !t.muted) return record(s, stream);
      if (t && t.readyState === "live" && t.muted && !muteWaited) {
        // A call holds it. Wait for it to come back by itself, then reopen.
        muteWaited = true;
        sayInterrupted({muted: true});
        return later(s, document.hidden ? null : backoff(), "recover");
      }
      if (stream) drop();
      if (opened && document.hidden) {
        // Asking while hidden fails, or hangs, on a phone.
        sayInterrupted({});
        return later(s, null, "recover");
      }
      if (!opened) say("starting");
      open().then(function (got) {
        if (s !== session || !on) { got.getTracks().forEach(function (x) { x.stop(); }); return; }
        opened = true;
        adopt(got);
        resumeContext();
        var fresh = track();
        if (fresh && fresh.muted) {
          // Still held by a call: wait for it, and try a new one less often.
          muteWaited = true;
          sayInterrupted({muted: true});
          return later(s, document.hidden ? null : backoff(), "recover");
        }
        say("listening", interrupted ? {recovered: true} : {});
        interrupted = muteWaited = false;
        retryMs = RETRY_MS;
        record(s, got);
      }, function (e) {
        if (s !== session || !on) return;
        if (!opened) {
          say("blocked", {error: e});
          stop(true);
          return;
        }
        // Lost once, still not back. Try again, less often each time, and say so once.
        if (!interrupted) sayInterrupted({error: e});
        later(s, document.hidden ? null : backoff(), "recover");
      });
    }

    function record(s, st) {
      if (interrupted || muteWaited) {
        interrupted = muteWaited = false;
        retryMs = RETRY_MS;
        say("listening", {recovered: true});
      }
      spoiled = false;
      recordClip(st).then(function (blob) {
        if (s !== session || !on) return;
        if (spoiled || st !== stream || !blob.size) {
          // Half a clip, or none: start again as soon as there is a microphone.
          return later(s, 0, "recover");
        }
        say("sending", {size: blob.size, type: blob.type});
        var sent;
        try { sent = Promise.resolve(onClip(blob, {type: blob.type, ms: clipMs})); }
        catch (e) { sent = Promise.reject(e); }
        return sent.then(function () {
          failures = 0;
          later(s, gapMs);
        }, function (e) { failed(s, e); });
      }, function (e) {
        if (s !== session || !on) return;
        say("error", {error: e});
        later(s, gapMs);
      });
    }

    function failed(s, e) {
      if (s !== session || !on) return;
      if (e && e.status === 401) {
        say("signedout", {error: e});
        stop(true);
        return;
      }
      var wait = 0, reason = null;
      if (e && e.status === 429) {
        reason = "busy";
        wait = e.retryAfter || 60;
      } else if (e && (e.network || e.status >= 500)) {
        reason = e.network ? "network" : "server";
        failures += 1;
        wait = Math.min(NETWORK_MAX_S, 5 * Math.pow(2, failures - 1));
      }
      if (!reason) {
        say("error", {error: e});
        return later(s, gapMs);
      }
      say("waiting", {reason: reason, seconds: wait, until: Date.now() + wait * 1000, error: e});
      later(s, wait * 1000, "wait");
    }

    function onVisible() {
      if (!on || document.hidden) return;
      wake();
      resumeContext();
      var t = track();
      // Back from a call or another app: a track that is still muted is
      // reopened, which is what Safari needs after a call.
      if (t && (t.readyState !== "live" || t.muted)) { spoil(); drop(); sayInterrupted({}); }
      kick();
    }
    function onDeviceChange() { if (on) listDevices(); }

    function start() {
      if (on) return;
      if (!supported()) {
        say("blocked", {error: new Error("This browser only allows microphone access over https, or on localhost.")});
        return;
      }
      on = true;
      session += 1;
      opened = interrupted = muteWaited = false;
      retryMs = RETRY_MS;
      failures = 0;
      wakeSaid = null;
      // Made here, inside the tap, so Safari lets it run.
      var Ctx = root.AudioContext || root.webkitAudioContext;
      try { ctx = Ctx ? new Ctx() : null; } catch (e) { ctx = null; }
      if (ctx) ctx.onstatechange = function () { if (!document.hidden) resumeContext(); };
      document.addEventListener("visibilitychange", onVisible);
      if (navigator.mediaDevices.addEventListener) {
        navigator.mediaDevices.addEventListener("devicechange", onDeviceChange);
      }
      wake();
      cycle(session);
    }

    // quiet: stopped by the loop, which has said why already.
    function stop(quiet) {
      if (!on) return;
      on = false;
      session += 1;
      phase = "idle";
      clearTimeout(timer);
      spoil();
      drop();
      unwake();
      document.removeEventListener("visibilitychange", onVisible);
      if (navigator.mediaDevices.removeEventListener) {
        navigator.mediaDevices.removeEventListener("devicechange", onDeviceChange);
      }
      if (ctx) { try { ctx.close(); } catch (e) {} ctx = null; }
      if (quiet !== true) say("stopped");
    }

    // Use another microphone from the next clip. It is remembered in this browser.
    function setDevice(id) {
      try { if (storageKey) localStorage.setItem(storageKey, id); } catch (e) {}
      if (!on || !stream) return;
      spoil();
      drop();
      // Not an interruption: the loop opens the new one at once.
      if (phase === "paused") { clearTimeout(timer); cycle(session); }
    }

    return {
      start: start,
      stop: function () { stop(false); },
      setDevice: setDevice,
      isOn: function () { return on; },
      wakeHeld: function () { return !!lock; },
      stream: function () { return stream; },
      mimeType: function () { return mimeType; }
    };
  }

  root.ScrobsterMic = {
    create: create, upload: upload, supported: supported, pickMimeType: pickMimeType,
    BITS_PER_SECOND: BITS_PER_SECOND, CLIP_MS: CLIP_MS, GAP_MS: GAP_MS
  };
})(window);
