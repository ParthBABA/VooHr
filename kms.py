"""Google Cloud KMS envelope-encryption helpers.

Uses Application Default Credentials — run `gcloud auth application-default login`
locally and it just works. In production (Cloud Run / GCE) the attached service
account provides credentials automatically.

Client is lazily initialized so import-time doesn't require credentials.
"""

import os

_client = None
_key_name = None


def _get_client():
    global _client, _key_name
    if _client is not None:
        return _client, _key_name

    from dotenv import load_dotenv
    load_dotenv()

    from google.cloud import kms_v1

    # If GCP_SA_KEY_JSON is set, build credentials directly from that JSON
    # string (no file needed — paste the whole service-account key as one
    # env var). Falls back to normal Application Default Credentials
    # (e.g. GOOGLE_APPLICATION_CREDENTIALS pointing at a file) otherwise.
    sa_json = os.environ.get("GCP_SA_KEY_JSON")
    if sa_json:
        import json

        from google.oauth2 import service_account

        info = json.loads(sa_json)
        credentials = service_account.Credentials.from_service_account_info(info)
        _client = kms_v1.KeyManagementServiceClient(credentials=credentials)
    else:
        _client = kms_v1.KeyManagementServiceClient()

    _key_name = _client.crypto_key_path(
        os.environ["GCP_PROJECT_ID"],
        os.environ["GCP_KMS_LOCATION"],
        os.environ["GCP_KMS_KEY_RING"],
        os.environ["GCP_KMS_KEY"],
    )
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