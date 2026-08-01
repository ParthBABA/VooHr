import os

from flask import Flask, jsonify, request, send_from_directory

from api import api_bp
from auth import auth_bp, register_google_oauth
from config import Config
from employees import employees_bp
from extensions import init_db
from notifications import notifications_bp
from sessions import sessions_bp


def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config.from_object(Config)
    # Without this, Flask's debug mode re-raises exceptions straight to the
    # interactive Werkzeug debugger (an HTML page) instead of letting our
    # errorhandler below turn it into JSON for API callers.
    app.config["PROPAGATE_EXCEPTIONS"] = False

    init_db(app)
    register_google_oauth(app)

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(employees_bp, url_prefix="/api")
    app.register_blueprint(sessions_bp, url_prefix="/api")
    app.register_blueprint(notifications_bp, url_prefix="/api")

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "login.html")

    # Safety net: any unhandled exception (or 404/500) under /api/* must come
    # back as JSON, never Flask/Werkzeug's HTML error/debugger page. Without
    # this, frontend `fetch(...).then(r => r.json())` calls blow up with
    # "Unexpected token '<', <!doctype ... is not valid JSON" whenever a bug
    # slips through a route's own try/except (or the route itself 404s).
    @app.errorhandler(Exception)
    def handle_api_exception(e):
        if not request.path.startswith("/api"):
            raise e
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify({"error": e.name.lower().replace(" ", "_")}), e.code
        app.logger.exception("Unhandled exception on %s", request.path)
        return jsonify({"error": "internal_server_error"}), 500

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)
