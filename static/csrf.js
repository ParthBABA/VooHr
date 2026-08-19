/**
 * CSRF protection for VooHr.
 *
 * Fetches the session-bound CSRF token from /api/csrf-token (a GET, so no
 * token needed) and monkey-patches window.fetch so every subsequent
 * state-changing request (POST / PUT / PATCH / DELETE) automatically
 * includes the X-CSRF-Token header.
 *
 * Safe methods (GET / HEAD / OPTIONS) are left untouched.
 *
 * If the user is not authenticated the endpoint returns 401, the token
 * variable stays null, and no header is added — which is correct because
 * the server-side guard also skips unauthenticated requests.
 */
(function () {
  var csrfToken = null;

  fetch("/api/csrf-token")
    .then(function (r) {
      return r.ok ? r.json() : null;
    })
    .then(function (data) {
      if (data && data.csrf_token) {
        csrfToken = data.csrf_token;
      }
    })
    .catch(function () {});

  var _origFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    var method = (init.method || (init.body ? "POST" : "GET")).toUpperCase();
    if (
      csrfToken &&
      method !== "GET" &&
      method !== "HEAD" &&
      method !== "OPTIONS"
    ) {
      if (init.headers && typeof init.headers === "object" && !(init.headers instanceof Headers)) {
        init.headers["X-CSRF-Token"] = csrfToken;
      } else if (init.headers instanceof Headers) {
        init.headers.set("X-CSRF-Token", csrfToken);
      } else {
        init.headers = { "X-CSRF-Token": csrfToken };
      }
    }
    return _origFetch.call(this, input, init);
  };
})();
