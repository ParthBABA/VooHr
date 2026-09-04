"""Google Cloud KMS envelope-encryption helpers.

We're back on Google Cloud KMS for field-level envelope encryption: the org's
`iam.disableServiceAccountKeyCreation` restriction was lifted and a new
service-account key is now available, so per-record Data Encryption Keys (DEKs)
are wrapped/unwrapped by Cloud KMS instead of a local AES master key.

The DEK is generated per record in field_encryption.py; only that 32-byte DEK
is sent to Cloud KMS. Credentials are resolved lazily — on the first
wrap_data_key / unwrap_data_key call — from, in order:

1. GOOGLE_CREDENTIALS_JSON — the full service-account key JSON as one env var.
   Paste the entire file contents into this variable (Railway stores it as a
   secret string, no file needed at runtime).
2. Application Default Credentials (local dev): set GOOGLE_APPLICATION_CREDENTIALS
   to a local key file path, or run `gcloud auth application-default login`.

The client is built and cached on first use. Importing this module does not
require live credentials, so modules that import it can be loaded (and their
tests collected) without GCP being configured; only an actual wrap/unwrap call
will raise if no credentials are available.

SECURITY: this service-account key must be treated as a secret. If it is ever
exposed (committed to git, pasted in a chat, logged, etc.) rotate it
immediately in Google Cloud Console and update GOOGLE_CREDENTIALS_JSON.
"""

import json
import os
import threading

from dotenv import load_dotenv

load_dotenv()

from google.cloud import kms_v1  # noqa: E402

_client = None
_key_name = None
_client_lock = threading.Lock()


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


def _get_client():
    """Build (and cache) the KMS client and key path on first use.

    Raises a clear RuntimeError if credentials are not configured, but only when
    an actual wrap/unwrap is requested — never at import time.
    """
    global _client, _key_name

    if _client is not None:
        return _client, _key_name

    with _client_lock:
        if _client is not None:  # double-checked inside the lock
            return _client, _key_name

        try:
            credentials = _resolve_credentials()
            client = kms_v1.KeyManagementServiceClient(credentials=credentials)
            key_name = client.crypto_key_path(
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

        _client = client
        _key_name = key_name
        return _client, _key_name


def wrap_data_key(dek_bytes: bytes) -> bytes:
    """Send a 32-byte DEK to Cloud KMS and return the wrapped ciphertext."""
    client, key_name = _get_client()
    response = client.encrypt(request={"name": key_name, "plaintext": dek_bytes})
    return response.ciphertext


def unwrap_data_key(wrapped_bytes: bytes) -> bytes:
    """Ask Cloud KMS to unwrap a previously wrapped DEK."""
    client, key_name = _get_client()
    response = client.decrypt(request={"name": key_name, "ciphertext": wrapped_bytes})
    return response.plaintext
