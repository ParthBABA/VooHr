"""Local envelope-encryption helper (replaces Google Cloud KMS).

GCP KMS needed a service-account key, and this org has
`iam.disableServiceAccountKeyCreation` enforced, blocking that. Instead we
wrap/unwrap the per-record DEK with a single master key kept in the
MASTER_ENCRYPTION_KEY env var (base64, 32 raw bytes) — same trust model as
PASSWORD_PEPPER / HASH_INDEX_SECRET elsewhere in this app: one server-side
secret, never written to the database.

Generate a key once with:
    python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
and put the output in MASTER_ENCRYPTION_KEY on Render (and in .env locally).
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12  # 96-bit nonce for AES-GCM


def _master_key() -> bytes:
    from dotenv import load_dotenv
    load_dotenv()

    key_b64 = os.environ.get("MASTER_ENCRYPTION_KEY")
    if not key_b64:
        raise RuntimeError(
            "MASTER_ENCRYPTION_KEY is not set — cannot wrap/unwrap data keys. "
            "Generate one with: python -c \"import secrets, base64; "
            "print(base64.b64encode(secrets.token_bytes(32)).decode())\""
        )
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise RuntimeError("MASTER_ENCRYPTION_KEY must decode to exactly 32 bytes.")
    return key


def wrap_data_key(dek_bytes: bytes) -> bytes:
    """Encrypt a 32-byte DEK with the local master key. Returns nonce + ciphertext."""
    aesgcm = AESGCM(_master_key())
    nonce = os.urandom(_NONCE_LEN)
    ct = aesgcm.encrypt(nonce, dek_bytes, None)
    return nonce + ct


def unwrap_data_key(wrapped_bytes: bytes) -> bytes:
    """Decrypt a DEK previously wrapped with wrap_data_key."""
    aesgcm = AESGCM(_master_key())
    nonce = wrapped_bytes[:_NONCE_LEN]
    ct = wrapped_bytes[_NONCE_LEN:]
    return aesgcm.decrypt(nonce, ct, None)