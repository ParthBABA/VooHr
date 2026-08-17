import logging
import os

from bson import ObjectId
from flask import Flask, jsonify, request, send_from_directory, redirect, render_template, session

from api import api_bp
from auth import auth_bp, register_google_oauth
from auth_email import auth_email_bp
from config import Config
from employees import employees_bp
from employees import _session_is_active
from extensions import get_db, init_db
from notifications import notifications_bp
from sessions import sessions_bp
from totp_routes import totp_bp

logger = logging.getLogger(__name__)


def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config.from_object(Config)
    # Without this, Flask's debug mode re-raises exceptions straight to the
    # interactive Werkzeug debugger (an HTML page) instead of letting our
    # errorhandler below turn it into JSON for API callers.
    app.config["PROPAGATE_EXCEPTIONS"] = False

    # Startup sanity check so production logs show whether the Brevo email
    # config was loaded. Only presence is logged, never the key value.
    logger.info(
        "Email config: BREVO_API_KEY=%s BREVO_SENDER_EMAIL=%s BREVO_SENDER_NAME=%s",
        "set" if os.environ.get("BREVO_API_KEY") else "MISSING",
        os.environ.get("BREVO_SENDER_EMAIL") or "MISSING",
        os.environ.get("BREVO_SENDER_NAME") or "(default VooHr)",
    )

    init_db(app)
    register_google_oauth(app)

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(auth_email_bp, url_prefix="/auth")
    app.register_blueprint(totp_bp, url_prefix="/auth")
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(employees_bp, url_prefix="/api")
    app.register_blueprint(sessions_bp, url_prefix="/api")
    app.register_blueprint(notifications_bp, url_prefix="/api")

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
    _TOTP_EXEMPT = frozenset({_TOTP_ENROLL_ROUTE, _TOTP_LOGIN_ROUTE})

    _PROTECTED_PAGES = frozenset({
        "/dashboard",
        "/directory",
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

    # Clean URL routes for static pages
    @app.route("/")
    def index():
        user_id = session.get("user_id")
        session_token = session.get("session_token")
        is_logged_in = bool(user_id and session_token and _session_is_active(user_id, session_token))
        app.logger.info("Root route: user_id=%s session_token=%s is_logged_in=%s", user_id, session_token, is_logged_in)
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
        return send_from_directory(app.static_folder, "dashboard.html")

    @app.route("/directory")
    def directory():
        return send_from_directory(app.static_folder, "directory.html")

    @app.route("/dictation")
    def dictation():
        return send_from_directory(app.static_folder, "dictation.html")

    @app.route("/workspace")
    def workspace():
        return send_from_directory(app.static_folder, "conversation-workspace.html")

    @app.route("/settings")
    def settings():
        return send_from_directory(app.static_folder, "settings.html")

    @app.route("/settings/security/setup-totp")
    def setup_totp():
        return send_from_directory(app.static_folder, "verify-totp-gate.html")

    @app.route("/auth/totp/verify-login")
    def totp_verify_login_page():
        return send_from_directory(app.static_folder, "verify-totp-login.html")

    @app.route("/risk-drift")
    def risk_drift():
        return send_from_directory(app.static_folder, "risk-drift.html")

    @app.route("/sync")
    def sync():
        return send_from_directory(app.static_folder, "sync.html")

    @app.route("/sync/room")
    def sync_room():
        return send_from_directory(app.static_folder, "sync_room.html")

    @app.route("/privacy")
    def privacy():
        return send_from_directory(app.static_folder, "privacy-policy.html")

    @app.route("/terms")
    def terms():
        return send_from_directory(app.static_folder, "terms-of-service.html")

    @app.route("/forgot-password")
    def forgot_password():
        return send_from_directory(app.static_folder, "forgot-password.html")

    # Redirect legacy .html paths to clean URLs
    @app.route("/signin.html")
    def signin_html_redirect():
        return redirect("/signin")

    @app.route("/signup.html")
    def signup_html_redirect():
        return redirect("/signup")

    @app.route("/email-verify.html")
    def email_verify_html_redirect():
        return redirect("/verify-email")

    @app.route("/otp-verify.html")
    def otp_verify_html_redirect():
        return redirect("/verify-otp")

    @app.route("/onboarding.html")
    def onboarding_html_redirect():
        return redirect("/onboarding")

    @app.route("/onboarding-complete.html")
    def welcome_html_redirect():
        return redirect("/welcome")

    @app.route("/dashboard.html")
    def dashboard_html_redirect():
        return redirect("/dashboard")

    @app.route("/directory.html")
    def directory_html_redirect():
        return redirect("/directory")

    @app.route("/dictation.html")
    def dictation_html_redirect():
        return redirect("/dictation")

    @app.route("/conversation-workspace.html")
    def workspace_html_redirect():
        return redirect("/workspace")

    @app.route("/settings.html")
    def settings_html_redirect():
        return redirect("/settings")

    @app.route("/risk-drift.html")
    def risk_drift_html_redirect():
        return redirect("/risk-drift")

    @app.route("/sync.html")
    def sync_html_redirect():
        return redirect("/sync")

    @app.route("/sync_room.html")
    def sync_room_html_redirect():
        return redirect("/sync/room")

    @app.route("/privacy-policy.html")
    def privacy_html_redirect():
        return redirect("/privacy")

    @app.route("/terms-of-service.html")
    def terms_html_redirect():
        return redirect("/terms")

    @app.route("/forgot-password.html")
    def forgot_password_html_redirect():
        return redirect("/forgot-password")

    # Custom 404 page for non-API routes
    @app.errorhandler(404)
    def handle_404(e):
        if request.path.startswith("/api"):
            return jsonify({"error": "not_found"}), 404
        return send_from_directory(app.static_folder, "404.html"), 404

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
    app.run(debug=True, port=port)