"""Envelope encryption for individual fields (AES-256-GCM + Cloud KMS).

One fresh Data Encryption Key (DEK) is generated per call. Each field is
encrypted locally with that DEK (fast, no network call per field), then
only the 32-byte DEK is sent to Cloud KMS to be wrapped. This is the
pattern Google recommends for field-level encryption.

encrypted_fields structure stored in MongoDB:
{
    "name": "<base64(iv + authTag + ciphertext)>",
    "email": "<base64(iv + authTag + ciphertext)>",
    ...
}
wrapped_dek: "<base64>"  (stored alongside, one per document)
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kms import unwrap_data_key, wrap_data_key

_ALGO_NONCE_LEN = 12  # 96-bit nonce for AES-GCM


def _b64(buf: bytes) -> str:
    return base64.b64encode(buf).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def encrypt_fields(fields: dict[str, str | None]) -> tuple[dict[str, str], str]:
    """Encrypt a dict of string fields.

    Returns (encrypted_dict, wrapped_dek_b64).
    """
    dek = os.urandom(32)
    aesgcm = AESGCM(dek)

    encrypted: dict[str, str] = {}
    for key, value in fields.items():
        if value is None or value == "":
            continue
        nonce = os.urandom(_ALGO_NONCE_LEN)
        ct = aesgcm.encrypt(nonce, value.encode("utf-8"), None)
        # Store nonce (12) + ciphertext (includes 16-byte GCM tag)
        encrypted[key] = _b64(nonce + ct)

    wrapped = wrap_data_key(dek)
    return encrypted, _b64(wrapped)


def decrypt_fields(encrypted: dict[str, str] | None, wrapped_dek_b64: str) -> dict[str, str]:
    """Decrypt a dict of fields previously encrypted with encrypt_fields."""
    if not wrapped_dek_b64 or not encrypted:
        return {}

    dek = unwrap_data_key(_unb64(wrapped_dek_b64))
    aesgcm = AESGCM(dek)

    decrypted: dict[str, str] = {}
    for key, value in encrypted.items():
        if not value:
            continue
        raw = _unb64(value)
        nonce = raw[:_ALGO_NONCE_LEN]
        ct = raw[_ALGO_NONCE_LEN:]
        pt = aesgcm.decrypt(nonce, ct, None)
        decrypted[key] = pt.decode("utf-8")

    return decrypted
