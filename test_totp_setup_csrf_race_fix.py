"""Regression tests for the TOTP setup CSRF startup race fix.

Root cause being regression-tested: verify-totp-gate.html used to fire
POST /auth/totp/setup synchronously during page parse, while static/csrf.js
was still retrieving the session CSRF token asynchronously.  The token was
still null when the request was dispatched, so the fetch interceptor skipped
the X-CSRF-Token header and the server returned 403 "CSRF validation failed"
before the user could even see the QR code.

The fix (two files only):
  * static/csrf.js now exposes window.csrfTokenReady — a promise that
    resolves with the session token once populated and rejects cleanly if
    retrieval fails (a no-op .catch keeps non-awaiting pages console-clean).
  * verify-totp-gate.html chains its automatic setup POST onto that promise,
    so the interceptor always has the token available when the request fires.

No backend behaviour changed: /auth/totp/setup still requires the header;
the interceptor's semantics for every other page are untouched.
"""

import json
import os
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))
_CSRF_JS = os.path.join(_ROOT, "static", "csrf.js")
_GATE_HTML = os.path.join(_ROOT, "static", "verify-totp-gate.html")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── static/csrf.js: token-ready exposure ──────────────────────────────

class TestCsrfJsTokenReady:
    def test_exposes_csrf_token_ready_promise(self):
        src = _read(_CSRF_JS)
        assert "window.csrfTokenReady" in src
        # The promise must be built from the token endpoint fetch chain.
        assert 'fetch("/api/csrf-token")' in src

    def test_resolves_with_populated_token(self):
        src = _read(_CSRF_JS)
        # Resolves with the token once stored...
        assert "csrfToken = data.csrf_token" in src
        assert "return csrfToken" in src

    def test_rejects_cleanly_when_token_unavailable(self):
        src = _read(_CSRF_JS)
        assert 'throw new Error("CSRF token unavailable")' in src

    def test_rejections_marked_handled_for_non_awaiting_pages(self):
        # Without this, any page that loads csrf.js while the token endpoint
        # fails (e.g. logged-out pages) would log an unhandled rejection.
        src = _read(_CSRF_JS)
        assert "window.csrfTokenReady.catch" in src

    def test_interceptor_behaviour_unchanged(self):
        src = _read(_CSRF_JS)
        assert "var _origFetch = window.fetch" in src
        assert "window.fetch = function" in src
        # Null-token guard preserved (no header when token unavailable).
        assert "csrfToken &&" in src
        # Safe methods still untouched; state-changing methods get the header.
        assert '"X-CSRF-Token"' in src
        assert '_origFetch.call(this, input, init)' in src


# ── verify-totp-gate.html: setup POST waits for the token ────────────

class TestGatePageSetupChaining:
    def test_setup_post_chained_off_csrf_token_ready(self):
        src = _read(_GATE_HTML)
        assert "(window.csrfTokenReady || Promise.resolve(null))" in src
        # The setup fetch must live inside the .then callback, not fire bare.
        then_idx = src.index("(window.csrfTokenReady || Promise.resolve(null))")
        setup_idx = src.index("fetch('/auth/totp/setup'", then_idx)
        closing_then = src.index(".then(function(r){return r.json();})", then_idx)
        assert then_idx < setup_idx < closing_then

    def test_no_bare_top_level_setup_fetch_remains(self):
        src = _read(_GATE_HTML)
        # The ONLY occurrence of the setup fetch must be the chained one.
        occurrences = [
            i for i in range(len(src))
            if src.startswith("fetch('/auth/totp/setup'", i)
        ]
        assert len(occurrences) == 1
        chained_at = src.index("(window.csrfTokenReady || Promise.resolve(null))")
        assert occurrences[0] > chained_at

    def test_request_shape_preserved(self):
        src = _read(_GATE_HTML)
        assert "method:'POST'" in src
        assert "headers:{'Content-Type':'application/json'}" in src

    def test_response_and_error_handling_preserved(self):
        src = _read(_GATE_HTML)
        assert "if(d.error){setErr(d.error);return;}" in src
        assert "qrImg.src=d.qr" in src
        assert "secretKey.textContent=d.secret" in src
        assert "Failed to load QR code. Please refresh the page." in src

    def test_no_retry_or_delay_hacks_added(self):
        src = _read(_GATE_HTML)
        # The fix must be synchronisation, not retries/delays/exemptions.
        region = src[src.index("Step 1: Fetch TOTP setup"):src.index("Step 2: Verify code")]
        assert "setTimeout" not in region.replace(
            "setTimeout(function(){window.location.href='/dashboard';},1200)", "")
        assert "retry" not in region.lower()


# ── Functional race test: execute the REAL shipped files ─────────────

_HARNESS_JS = r"""
/* Executes the real static/csrf.js and the real inline scripts extracted
   from verify-totp-gate.html in a sandbox with instrumented fetch, then
   prints one JSON line: {"ok":bool,"detail":str,"calls":[...],...} */
const fs = require("fs");
const vm = require("vm");
const STATIC_DIR = process.argv[2];
const csrfSrc = fs.readFileSync(STATIC_DIR + "/csrf.js", "utf8");
const gateHtml = fs.readFileSync(STATIC_DIR + "/verify-totp-gate.html", "utf8");
const inlineScripts = [...gateHtml.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);

function makeSandbox(instrFetch) {
  const elements = {};
  const el = () => ({ style: {}, addEventListener() {}, textContent: "", src: "" });
  class Headers {
    constructor(init) { this._h = Object.assign({}, init || {}); }
    set(k, v) { this._h[k] = v; }
    get(k) { return this._h[k]; }
  }
  const sandbox = {
    window: { fetch: instrFetch },
    Headers,
    setTimeout, clearTimeout, Promise, console,
    document: {
      getElementById: id => (elements[id] = elements[id] || el()),
      querySelectorAll: sel => sel === ".otp-seg" ? [el(), el(), el(), el(), el(), el()] : [],
      addEventListener() {},
      documentElement: { classList: { add() {} } },
    },
    navigator: {},
    location: { href: "" },
  };
  sandbox.globalThis = sandbox;
  Object.defineProperty(sandbox, "fetch", { get() { return sandbox.window.fetch; } });
  vm.createContext(sandbox);
  return { sandbox, elements };
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function runScenario({ tokenDelay, tokenOk }) {
  const calls = [];
  const instrFetch = (url, init) => {
    init = init || {};
    const method = (init.method || "GET").toUpperCase();
    const headers = init.headers || {};
    calls.push({
      method, url,
      hasHeader: Object.prototype.hasOwnProperty.call(headers, "X-CSRF-Token"),
      token: headers["X-CSRF-Token"],
    });
    if (url === "/api/csrf-token") {
      return new Promise(res => setTimeout(() => res({
        ok: tokenOk,
        json: async () => tokenOk ? { csrf_token: "TESTTOKEN123" } : null,
      }), tokenDelay));
    }
    if (url === "/auth/totp/setup") {
      return Promise.resolve({ ok: true, json: async () => ({
        ok: true, qr: "data:image/png;base64,QR", secret: "SECRET234", uri: "otpauth://x" }) });
    }
    return Promise.resolve({ ok: true, json: async () => ({}) });
  };
  const { sandbox, elements } = makeSandbox(instrFetch);
  vm.runInContext(csrfSrc, sandbox);
  for (const s of inlineScripts) vm.runInContext(s, sandbox);
  await sleep(tokenDelay + 80);
  await new Promise(r => setImmediate(r));
  return { calls, sandbox, elements };
}

(async () => {
  /* Race scenario: slow token — setup must wait, header must be attached. */
  const a = await runScenario({ tokenDelay: 60, tokenOk: true });
  assert(a.calls[0].url === "/api/csrf-token" && a.calls[0].method === "GET",
     "token fetch must be dispatched first");
  const setups = a.calls.filter(c => c.url === "/auth/totp/setup");
  assert(setups.length === 1, "exactly one setup POST");
  assert(setups[0].hasHeader && setups[0].token === "TESTTOKEN123",
     "setup POST must carry X-CSRF-Token equal to the session token");
  assert(a.calls.findIndex(c => c.url === "/api/csrf-token") <
         a.calls.findIndex(c => c.url === "/auth/totp/setup"),
     "setup POST must occur after token resolution");
  assert(a.elements["qrImg"].src === "data:image/png;base64,QR", "QR rendered");
  assert(a.elements["qrLoading"].style.display === "none", "loading hidden");
  assert(a.elements["secretKey"].textContent === "SECRET234", "secret rendered");

  /* Failure scenario: token unavailable — no doomed bare POST, existing
     error box used. */
  const b = await runScenario({ tokenDelay: 20, tokenOk: false });
  assert(b.calls.filter(c => c.url === "/auth/totp/setup").length === 0,
     "no setup POST when token unavailable");
  assert(b.elements["gateError"].textContent ===
         "Failed to load QR code. Please refresh the page.",
     "existing error handling used on failure");

  /* Interceptor scenario: normal user-triggered POSTs still get the header. */
  const c = await runScenario({ tokenDelay: 10, tokenOk: true });
  await c.sandbox.window.csrfTokenReady;
  await c.sandbox.fetch("/auth/totp/verify-setup", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  });
  const v = c.calls.filter(x => x.url === "/auth/totp/verify-setup")[0];
  assert(v && v.hasHeader && v.token === "TESTTOKEN123",
     "interceptor unchanged for normal POSTs");

  function assert(cond, msg) { if (!cond) throw new Error(msg); }
})().then(
  () => { console.log(JSON.stringify({ ok: true })); },
  e => { console.log(JSON.stringify({ ok: false, detail: String(e && e.message || e) })); process.exit(1); }
);
"""


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="Node.js not available for JS execution")
class TestFunctionalRaceExecution:
    def test_real_files_pass_race_harness(self, tmp_path):
        harness = tmp_path / "race_harness.js"
        harness.write_text(_HARNESS_JS, encoding="utf-8")
        proc = subprocess.run(
            ["node", str(harness), os.path.join(_ROOT, "static")],
            capture_output=True, text=True, timeout=60,
        )
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        assert lines, f"harness produced no output; stderr={proc.stderr}"
        result = json.loads(lines[-1])
        assert result["ok"] is True, (
            f"race harness failed: {result.get('detail')} "
            f"(stderr={proc.stderr})"
        )
