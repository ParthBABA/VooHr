import os

from flask import Flask, send_from_directory

from api import api_bp
from auth import auth_bp, register_google_oauth
from config import Config
from extensions import init_db


def create_app():
    app = Flask(__name__, static_folder="static", static_url_path="")
    app.config.from_object(Config)

    init_db(app)
    register_google_oauth(app)

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(api_bp, url_prefix="/api")

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "login.html")

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, port=port)
