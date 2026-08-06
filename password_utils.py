"""Password hashing helpers (argon2id) with a server-side pepper.

Passwords are hashed, never reversibly encrypted. Each hash is argon2id of
`raw + PASSWORD_PEPPER`: the pepper is a static server secret mixed in before
hashing, so a database leak alone cannot be brute-forced offline without also
having the pepper. This is separate from the per-field KMS encryption used for
reversible PII (name/email) — a password must never be recoverable.
"""

import os

import argon2

_hasher = argon2.PasswordHasher()


def _get_pepper() -> str:
    """Return the PASSWORD_PEPPER secret or raise (mirrors blind_index's
    behaviour of refusing to operate without its secret)."""
    pepper = os.environ.get("PASSWORD_PEPPER")
    if not pepper:
        raise RuntimeError("PASSWORD_PEPPER is not set — cannot hash passwords.")
    return pepper


def hash_password(raw: str) -> str:
    """Argon2id-hash a raw password mixed with the server-side pepper."""
    return _hasher.hash(raw + _get_pepper())


def verify_password(raw: str, stored_hash: str) -> bool:
    """Verify a raw password against a stored argon2id hash. Never raises on
    a mismatch — returns False instead."""
    try:
        return _hasher.verify(stored_hash, raw + _get_pepper())
    except argon2.exceptions.VerifyMismatchError:
        return False


def password_strength_ok(raw: str) -> bool:
    """True if the password is at least 8 chars and mixes letters & digits."""
    if len(raw) < 8:
        return False
    has_letter = any(c.isalpha() for c in raw)
    has_digit = any(c.isdigit() for c in raw)
    return has_letter and has_digit
