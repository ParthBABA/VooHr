import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()


class Config:
    # Flask session signing key. Set a long random value in production.
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    # Keep users signed in across browser restarts. Session cookies are
    # browser-session-only by default (and get wiped when the browser
    # closes), so without a permanent session the app "forgets" the Google
    # login on the next visit.
    PERMANENT_SESSION_LIFETIME = timedelta(days=int(os.environ.get("SESSION_LIFETIME_DAYS", "30")))

    # MongoDB
    MONGODB_URI = os.environ.get("MONGODB_URI")
    MONGODB_DB = os.environ.get("MONGODB_DB", "voohr")

    # Google OAuth (create credentials at https://console.cloud.google.com/apis/credentials)
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    # Exact callback URL Google must redirect back to. When unset, it is
    # derived from the request host (url_for _external=True), which breaks if
    # the browser uses a different host than the Google Console whitelist
    # (e.g. 127.0.0.1 vs localhost, or Render's URL).
    GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI") or ""

    # Google Cloud KMS (field-level envelope encryption)
    GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
    GCP_KMS_LOCATION = os.environ.get("GCP_KMS_LOCATION", "asia-south1")
    GCP_KMS_KEY_RING = os.environ.get("GCP_KMS_KEY_RING", "voovr-keyring")
    GCP_KMS_KEY = os.environ.get("GCP_KMS_KEY", "voovr-field-encryption-key")

    # Blind-index & JWT secrets
    HASH_INDEX_SECRET = os.environ.get("HASH_INDEX_SECRET")
    JWT_SECRET = os.environ.get("JWT_SECRET")

    # Provider configuration
    # STT: DeepSeek has no audio/transcription API, so audio -> text uses
    # OpenAI Whisper. LLM: DeepSeek's chat API analyzes the transcript
    # (summary/sentiment/risks) once we have text.
    STT_PROVIDER = os.environ.get("STT_PROVIDER", "openai")
    LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "deepseek")
    STORAGE_PROVIDER = os.environ.get("STORAGE_PROVIDER", "local")
    VISION_PROVIDER = os.environ.get("VISION_PROVIDER", "openai")

    # OpenAI (used for Whisper speech-to-text)
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_KEY", "")
    OPENAI_ANALYSIS_MODEL = os.environ.get("OPENAI_ANALYSIS_MODEL", "gpt-4o")
    OPENAI_STT_MODEL = os.environ.get("OPENAI_STT_MODEL", "whisper-1")
    OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")

    # DeepSeek (used for transcript analysis)
    DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSSEK_API", "")
    DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_ANALYSIS_MODEL = os.environ.get("DEEPSEEK_ANALYSIS_MODEL", "deepseek-chat")

    # V2 Behavioural Intelligence Framework toggle
    USE_V2_FRAMEWORK = os.environ.get("USE_V2_FRAMEWORK", "false").lower() == "true"

    # Audio storage
    AUDIO_STORAGE_PATH = os.environ.get("AUDIO_STORAGE_PATH", "static/audio/sessions")

    # Cookie/session behaviour
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Set to True once served over HTTPS in production.
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
