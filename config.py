import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Flask session signing key. Set a long random value in production.
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    # MongoDB
    MONGODB_URI = os.environ.get("MONGODB_URI")
    MONGODB_DB = os.environ.get("MONGODB_DB", "voohr")

    # Google OAuth (create credentials at https://console.cloud.google.com/apis/credentials)
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

    # Cookie/session behaviour
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Set to True once served over HTTPS in production.
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
