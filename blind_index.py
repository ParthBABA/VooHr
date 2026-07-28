"""Deterministic HMAC blind index for searchable encrypted fields.

Same input always produces the same hash, which allows MongoDB queries
like db.users.find({"email_hash": blind_index("alice@example.com")})
even though the actual `email` field is randomly encrypted.

This leaks "these two records have the same email" to anyone with raw
DB access — an acceptable, standard trade-off for a lookup index.
"""

import hashlib
import hmac as _hmac


def _get_secret():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    return os.environ.get("HASH_INDEX_SECRET") or os.environ.get("JWT_SECRET", "")


def blind_index(value: str) -> str:
    """Return a hex HMAC-SHA256 of the lowercased, stripped value."""
    secret = _get_secret()
    if not secret:
        raise RuntimeError("HASH_INDEX_SECRET is not set — cannot create blind index.")
    return _hmac.new(
        secret.encode(),
        value.strip().lower().encode(),
        hashlib.sha256,
    ).hexdigest()
