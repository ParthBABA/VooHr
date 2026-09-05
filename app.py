import hmac
import logging
import os
import secrets

from werkzeug.exceptions import RequestEntityTooLarge

from bson import ObjectId
from flask import Flask, jsonify, request, send_from_directory, redirect, render_template, session

from api import api_bp
from audit_log import audit_bp
from auth import auth_bp, register_google_oauth
from auth_email import auth_email_bp
from config import Config
from conversation_memory import conversation_memory_bp
from employees import employees_bp
from employees import _session_is_active
from employees import TOTPRequired
from extensions import get_db, init_db, check_rate_limit, record_rate_limit_event, client_ip
from meetings import meetings_bp
from notifications import notifications_bp
from reminders import reminders_bp
from sessions import sessions_bp
from totp_routes import totp_bp
from tts import tts_bp
from surveys import surveys_bp

logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config.from_object(Config)
    # Without this, Flask's debug mode re-raises exceptions straight to the
    # interactive Werkzeug debugger (an HTML page) instead of letting our
    # errorhandler below turn it into JSON for API callers.
    app.config["PROPAGATE_EXCEPTIONS"] = False

    # ── CSRF protection ─────────────────────────────────────────────────
    # Every state-changing request (POST/PUT/PATCH/DELETE) from an
    # authenticated session must carry a valid X-CSRF-Token header.
    # The token is a secrets.token_urlsafe(32) stored in the Flask session
    # and delivered to the frontend via GET /api/csrf-token.  The frontend
    # JS (static/csrf.js) monkey-patches window.fetch to attach it
    # automatically.

    @app.route("/api/csrf-token")
    def _csrf_token_endpoint():
        """Return the CSRF token for the current authenticated session."""
        if not session.get("user_id"):
            return jsonify({"error": "not_authenticated"}), 401

        # Re-delivering an existing session-bound token is free: it mints no
        # new secret and reveals nothing the caller doesn't already hold, so
        # it must never consume rate-limit budget or be answered with 429.
        # Only actual token GENERATION is rate-limited — otherwise normal
        # navigation (every page load fetches /api/csrf-token via csrf.js)
        # exhausts the per-IP budget, the endpoint starts returning 429, and
        # state-changing requests such as TOTP verification are sent without
        # X-CSRF-Token, failing with "CSRF validation failed".
        if "_csrf_token" not in session:
            # Rate-limit CSRF token generation to prevent abuse.
            ip = client_ip()
            db = get_db()
            key = f"csrf_token:{ip}"
            allowed, retry_after = check_rate_limit(db, key, 30, 900)
            if not allowed:
                return jsonify({
                    "error": "Too many requests. Please try again later.",
                    "retry_after": retry_after,
                }), 429
            record_rate_limit_event(db, key, ttl_seconds=900)
            session["_csrf_token"] = secrets.token_urlsafe(32)

        return jsonify({"csrf_token": session["_csrf_token"]})

    @app.route("/health")
    @app.route("/api/health")
    def _health_check():
        """Liveness-only health check.

        Returns 200 immediately — no DB ping, no secret leakage.
        Used by load balancers and Render health checks.
        """
        return jsonify({"status": "ok"}), 200

    @app.before_request
    def _csrf_protect():
        """Validate CSRF token on state-changing requests from authenticated
        sessions.  Safe methods and unauthenticated requests are exempt."""
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        # Only enforce for authenticated sessions
        if not session.get("user_id"):
            return None
        token = request.headers.get("X-CSRF-Token")
        if not token:
            return jsonify({"error": "CSRF validation failed"}), 403
        expected = session.get("_csrf_token")
        if not expected:
            return jsonify({"error": "CSRF validation failed"}), 403
        if not hmac.compare_digest(token, expected):
            return jsonify({"error": "CSRF validation failed"}), 403
        return None

    # Startup sanity check so production logs show whether the Brevo email
    # config was loaded. Only presence is logged, never the key value.
    logger.info(
        "Email config: BREVO_API_KEY=%s BREVO_SENDER_EMAIL=%s BREVO_SENDER_NAME=%s",
        "set" if os.environ.get("BREVO_API_KEY") else "MISSING",
        os.environ.get("BREVO_SENDER_EMAIL") or "MISSING",
        os.environ.get("BREVO_SENDER_NAME") or "(default VooVr)",
    )

    # Startup sanity check for the analysis provider's API key, mirroring the
    # email check above. A missing key is loud at boot (warning in the logs)
    # instead of only surfacing as a confusing runtime failure on the first
    # real analysis request. Only presence is logged, never the key value.
    _llm_provider = app.config.get("LLM_PROVIDER", "deepseek")
    if _llm_provider == "deepseek":
        _llm_key_set = bool((os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSSEK_API") or "").strip())
    elif _llm_provider == "openai":
        _llm_key_set = bool((os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY") or "").strip())
    else:
        _llm_key_set = False
    if _llm_key_set:
        logger.info("LLM config: provider=%s api key=set", _llm_provider)
    else:
        logger.warning("LLM config: provider=%s api key=MISSING — analysis requests will fail", _llm_provider)

    init_db(app)
    register_google_oauth(app)

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(auth_email_bp, url_prefix="/auth")
    app.register_blueprint(totp_bp, url_prefix="/auth")
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(audit_bp, url_prefix="/api")
    app.register_blueprint(employees_bp, url_prefix="/api")
    app.register_blueprint(sessions_bp, url_prefix="/api")
    app.register_blueprint(notifications_bp, url_prefix="/api")
    app.register_blueprint(reminders_bp, url_prefix="/api")
    app.register_blueprint(meetings_bp, url_prefix="/api")
    app.register_blueprint(conversation_memory_bp, url_prefix="/api")
    app.register_blueprint(surveys_bp, url_prefix="/api")
    app.register_blueprint(tts_bp, url_prefix="/api")

    @app.errorhandler(TOTPRequired)
    def _handle_totp_required(exc):
        return jsonify({"error": "TOTP verification required"}), 403

    @app.errorhandler(RequestEntityTooLarge)
    def _handle_payload_too_large(exc):
        return jsonify({"error": "Request payload is too large."}), 413

    # ── Admin TOTP guard ────────────────────────────────────────────────
    # Every admin session must satisfy TWO conditions before accessing
    # protected pages:
    #   a) TOTP must be enabled on the account (first-time enrollment)
    #   b) The current session must have presented a valid code (per-session
    #      re-verification on every new login)
    # Both checks are centralized here so no individual registration/login
    # redirect target can bypass them.

    _TOTP_ENROLL_ROUTE = "/settings/security/setup-totp"
    _TOTP_LOGIN_ROUTE  = "/auth/totp/verify-login"
    _TOTP_EXEMPT = frozenset({
        _TOTP_ENROLL_ROUTE,
        _TOTP_LOGIN_ROUTE,
        "/auth/totp/verify-login-backup",
    })

    _PROTECTED_PAGES = frozenset({
        "/dashboard",
        "/dictation",
        "/workspace",
        "/settings",
        "/risk-drift",
        "/sync",
        "/sync/room",
    })

    def _admin_totp_required():
        """Return 'enroll', 'verify', or None.

        'enroll'  — TOTP not yet enabled on this account (first-time setup).
        'verify'  — TOTP enabled but this session hasn't presented a code.
        None      — no guard needed (not an admin, TOTP satisfied, or not logged in).
        """
        user_id = session.get("user_id")
        session_token = session.get("session_token")
        if not user_id or not session_token:
            return None
        try:
            uid = ObjectId(user_id)
        except Exception:
            return None
        if not _session_is_active(user_id, session_token):
            return None
        db = get_db()
        user = db.users.find_one({"_id": uid}, {"role": 1, "totp_enabled": 1})
        if not user or user.get("role") != "admin":
            return None
        if user.get("totp_enabled") is not True:
            return "enroll"
        if session.get("totp_verified_session") != session_token:
            return "verify"
        return None

    @app.before_request
    def _enforce_admin_totp():
        if request.path in _TOTP_EXEMPT or request.path not in _PROTECTED_PAGES:
            return None
        reason = _admin_totp_required()
        if reason == "enroll":
            return redirect(_TOTP_ENROLL_ROUTE + "?forced=1")
        if reason == "verify":
            return redirect(_TOTP_LOGIN_ROUTE + "?next=" + request.path)
        return None

    # ── End admin TOTP guard ────────────────────────────────────────────

    # ── Page-login guard ───────────────────────────────────────────────
    # Shared by every protected page route so the HTML shell is never
    # served to an unauthenticated visitor.  API routes already reject
    # unauthenticated calls via _require_auth() in employees.py; this
    # is the page-level equivalent (better UX + defense-in-depth).

    def _require_page_login():
        """Return a redirect Response if the session is not valid, or None
        if the caller may proceed to render the page.

        Reuses the exact same check the landing page uses for its
        is_logged_in variable and that employees.py uses for API auth.
        """
        user_id = session.get("user_id")
        session_token = session.get("session_token")
        if not user_id or not session_token:
            return redirect("/login?redirect=" + request.path)
        if not _session_is_active(user_id, session_token):
            session.clear()
            return redirect("/login?redirect=" + request.path)
        return None

    # Clean URL routes for static pages
    @app.route("/")
    def index():
        user_id = session.get("user_id")
        session_token = session.get("session_token")
        is_logged_in = bool(user_id and session_token and _session_is_active(user_id, session_token))
        app.logger.info("Root route: is_logged_in=%s", is_logged_in)
        return render_template("login.html", is_logged_in=is_logged_in)

    @app.route("/login")
    def login():
        return send_from_directory(app.static_folder, "login2.html")

    @app.route("/signin")
    def signin():
        return send_from_directory(app.static_folder, "signin.html")

    @app.route("/signup")
    def signup():
        return send_from_directory(app.static_folder, "signup.html")

    @app.route("/verify-email")
    def verify_email():
        return send_from_directory(app.static_folder, "email-verify.html")

    @app.route("/verify-otp")
    def verify_otp():
        return send_from_directory(app.static_folder, "otp-verify.html")

    @app.route("/onboarding")
    def onboarding():
        return send_from_directory(app.static_folder, "onboarding.html")

    @app.route("/welcome")
    def welcome():
        return send_from_directory(app.static_folder, "onboarding-complete.html")

    @app.route("/dashboard")
    def dashboard():
        guard = _require_page_login()
        if guard: return guard
        return send_from_directory(app.static_folder, "dashboard.html")

    @app.route("/dictation")
    def dictation():
        guard = _require_page_login()
        if guard: return guard
        return send_from_directory(app.static_folder, "dictation.html")

    @app.route("/workspace")
    def workspace():
        guard = _require_page_login()
        if guard: return guard
        return send_from_directory(app.static_folder, "conversation-workspace.html")

    @app.route("/settings")
    def settings():
        guard = _require_page_login()
        if guard: return guard
        return send_from_directory(app.static_folder, "settings.html")

    @app.route("/settings/security/setup-totp")
    def setup_totp():
        guard = _require_page_login()
        if guard: return guard
        return send_from_directory(app.static_folder, "verify-totp-gate.html")

    @app.route("/auth/totp/verify-login")
    def totp_verify_login_page():
        guard = _require_page_login()
        if guard: return guard
        return send_from_directory(app.static_folder, "verify-totp-login.html")

    @app.route("/risk-drift")
    def risk_drift():
        guard = _require_page_login()
        if guard: return guard
        return send_from_directory(app.static_folder, "risk-drift.html")

    @app.route("/sync")
    def sync():
        guard = _require_page_login()
        if guard: return guard
        return send_from_directory(app.static_folder, "sync.html")

    @app.route("/sync/room")
    def sync_room():
        guard = _require_page_login()
        if guard: return guard
        return send_from_directory(app.static_folder, "sync_room.html")

    @app.route("/meeting-tracker")
    def meeting_tracker():
        guard = _require_page_login()
        if guard: return guard
        return send_from_directory(app.static_folder, "meeting_tracker.html")

    @app.route("/invite-error")
    def invite_error():
        return send_from_directory(app.static_folder, "invite-error.html")

    @app.route("/privacy")
    def privacy():
        return send_from_directory(app.static_folder, "privacy-policy.html")

    @app.route("/terms")
    def terms():
        return send_from_directory(app.static_folder, "terms-of-service.html")

    @app.route("/forgot-password")
    def forgot_password():
        return send_from_directory(app.static_folder, "forgot-password.html")

    # Redirect legacy .html paths to clean URLs, preserving any query string
    # so backend redirects like /signin.html?error=no_account work end-to-end.
    def _html_redirect(target):
        qs = request.query_string.decode()
        return redirect(f"{target}?{qs}" if qs else target)

    @app.route("/signin.html")
    def signin_html_redirect():
        return _html_redirect("/signin")

    @app.route("/signup.html")
    def signup_html_redirect():
        return _html_redirect("/signup")

    @app.route("/email-verify.html")
    def email_verify_html_redirect():
        return _html_redirect("/verify-email")

    @app.route("/otp-verify.html")
    def otp_verify_html_redirect():
        return _html_redirect("/verify-otp")

    @app.route("/onboarding.html")
    def onboarding_html_redirect():
        return _html_redirect("/onboarding")

    @app.route("/onboarding-complete.html")
    def welcome_html_redirect():
        return _html_redirect("/welcome")

    @app.route("/dashboard.html")
    def dashboard_html_redirect():
        return _html_redirect("/dashboard")

    @app.route("/dictation.html")
    def dictation_html_redirect():
        return _html_redirect("/dictation")

    @app.route("/conversation-workspace.html")
    def workspace_html_redirect():
        return _html_redirect("/workspace")

    @app.route("/settings.html")
    def settings_html_redirect():
        return _html_redirect("/settings")

    @app.route("/risk-drift.html")
    def risk_drift_html_redirect():
        return _html_redirect("/risk-drift")

    @app.route("/sync.html")
    def sync_html_redirect():
        return _html_redirect("/sync")

    @app.route("/sync_room.html")
    def sync_room_html_redirect():
        return _html_redirect("/sync/room")

    @app.route("/privacy-policy.html")
    def privacy_html_redirect():
        return _html_redirect("/privacy")

    @app.route("/terms-of-service.html")
    def terms_html_redirect():
        return _html_redirect("/terms")

    @app.route("/forgot-password.html")
    def forgot_password_html_redirect():
        return _html_redirect("/forgot-password")

    # Custom 404 page for non-API routes
    @app.errorhandler(404)
    def handle_404(e):
        if request.path.startswith("/api"):
            return jsonify({"error": "not_found"}), 404
        return send_from_directory(app.static_folder, "404.html"), 404

    # ── User-Agent Client Hints opt-in ──────────────────────────────────
    # Chromium only sends high-entropy hints such as
    # Sec-CH-UA-Platform-Version after the origin opts in via Accept-CH.
    # That hint is the ONLY reliable way to tell Windows 11 from Windows 10
    # (both report "Windows NT 10.0" in the plain User-Agent), and it is
    # captured server-side at login time by login_flow._record_active_session
    # to power precise Active Sessions device labels.  Advertising it on
    # every response means a browser that has loaded any page once will
    # include the hints on its next login request.  Purely additive header;
    # no request handling changes.
    @app.after_request
    def _advertise_client_hints(response):
        response.headers.setdefault(
            "Accept-CH",
            "Sec-CH-UA-Platform, Sec-CH-UA-Platform-Version",
        )
        return response

    # Safety net: any unhandled exception (or 404/500) under /api/* must come
    # back as JSON, never Flask/Werkzeug's HTML error/debugger page. Without
    # this, frontend `fetch(...).then(r => r.json())` calls blow up with
    # "Unexpected token '<', <!doctype ... is not valid JSON" whenever a bug
    # slips through a route's own try/except (or the route itself 404s).
    @app.errorhandler(Exception)
    def handle_api_exception(e):
        from werkzeug.exceptions import HTTPException
        if not request.path.startswith("/api"):
            # Let non-API routes (static files, favicon, etc.) keep their
            # normal HTTP status — re-raising here would turn a plain 404
            # into a 500 because we're already inside the exception handler.
            if isinstance(e, HTTPException):
                return e
            raise e
        if isinstance(e, HTTPException):
            return jsonify({"error": e.name.lower().replace(" ", "_")}), e.code
        app.logger.exception("Unhandled exception on %s", request.path)
        return jsonify({"error": "internal_server_error"}), 500

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true", port=port)