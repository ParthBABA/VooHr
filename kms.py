"""Google Cloud KMS envelope-encryption helpers.

We're back on Google Cloud KMS for field-level envelope encryption: the org's
`iam.disableServiceAccountKeyCreation` restriction was lifted and a new
service-account key is now available, so per-record Data Encryption Keys (DEKs)
are wrapped/unwrapped by Cloud KMS instead of a local AES master key.

The DEK is generated per record in field_encryption.py; only that 32-byte DEK
is sent to Cloud KMS. Credentials are resolved at import time from, in order:

1. GOOGLE_CREDENTIALS_JSON — the full service-account key JSON as one env var.
   Paste the entire file contents into this variable (Railway stores it as a
   secret string, no file needed at runtime).
2. Application Default Credentials (local dev): set GOOGLE_APPLICATION_CREDENTIALS
   to a local key file path, or run `gcloud auth application-default login`.

If neither is configured the app refuses to start with a clear RuntimeError.

SECURITY: this service-account key must be treated as a secret. If it is ever
exposed (committed to git, pasted in a chat, logged, etc.) rotate it
immediately in Google Cloud Console and update GOOGLE_CREDENTIALS_JSON.
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()

from google.cloud import kms_v1  # noqa: E402


def _resolve_credentials():
    """Return KMS credentials from GOOGLE_CREDENTIALS_JSON, else ADC."""
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_info(json.loads(creds_json))

    # Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS / gcloud login)
    from google.auth import default as adc_default

    credentials, _project_id = adc_default()
    return credentials


try:
    _credentials = _resolve_credentials()
    _client = kms_v1.KeyManagementServiceClient(credentials=_credentials)
    _key_name = _client.crypto_key_path(
        os.environ["GCP_PROJECT_ID"],
        os.environ.get("GCP_KMS_LOCATION", "asia-south1"),
        os.environ.get("GCP_KMS_KEY_RING", "voovr-asia"),
        os.environ.get("GCP_KMS_KEY", "voovr-field-encryption-key"),
    )
except Exception as exc:
    raise RuntimeError(
        "Google Cloud KMS is not configured. Set GOOGLE_CREDENTIALS_JSON to the full "
        "service-account JSON as one env var (Railway: Project → Variables), or configure "
        "Application Default Credentials locally (GOOGLE_APPLICATION_CREDENTIALS)."
    ) from exc


def wrap_data_key(dek_bytes: bytes) -> bytes:
    """Send a 32-byte DEK to Cloud KMS and return the wrapped ciphertext."""
    response = _client.encrypt(request={"name": _key_name, "plaintext": dek_bytes})
    return response.ciphertext


def unwrap_data_key(wrapped_bytes: bytes) -> bytes:
    """Ask Cloud KMS to unwrap a previously wrapped DEK."""
    response = _client.decrypt(request={"name": _key_name, "ciphertext": wrapped_bytes})
    return response.plaintext
