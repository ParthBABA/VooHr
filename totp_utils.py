"""TOTP (Time-based One-Time Password) utilities for VooVr.

Provides secret generation, provisioning URI creation, QR code rendering,
time-window-aware code verification, and backup/recovery code generation
using the pyotp library and standard-library SHA-256.
"""

import hashlib
import hmac
import io
import base64
import secrets
import string

import pyotp
import qrcode


def generate_secret() -> str:
    """Generate a new random TOTP secret (32 bytes, base32-encoded)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str, issuer_name: str = "VooVr") -> str:
    """Return an otpauth:// URI that authenticator apps can parse to add the
    account.  Used both for QR codes and for copy-to-clipboard flows.
    """
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer_name)


def verify_code(secret: str, code: str, valid_window: int = 1) -> bool:
    """Check a 6-digit TOTP code against the secret.

    *valid_window* extends the check to adjacent time steps (default ±1 step
    = ±30 s) to tolerate slight clock drift.  Returns True only on a match.
    """
    if not code or len(code) != 6 or not code.isdigit():
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(code, valid_window=valid_window)


def qr_code_data_url(uri: str) -> str:
    """Render the provisioning URI as a QR code and return a data: URI that
    can be embedded directly in an <img> tag (PNG, no server round-trip
    needed on the frontend).
    """
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


# ── Backup / recovery codes ───────────────────────────────────────────

_BACKUP_CODE_COUNT = 10
_BACKUP_CODE_CHARS = string.ascii_uppercase + string.digits  # A-Z + 0-9
_BACKUP_CODE_LEN = 8  # characters per half (displayed as XXXX-XXXX)


def generate_backup_codes(count: int = _BACKUP_CODE_COUNT) -> list[str]:
    """Generate *count* random backup codes formatted as 'XXXX-XXXX'.

    Returns a list of **plaintext** strings.  The caller is responsible for
    hashing them before storage and showing them to the user exactly once.
    """
    codes = []
    for _ in range(count):
        left = "".join(secrets.choice(_BACKUP_CODE_CHARS) for _ in range(_BACKUP_CODE_LEN))
        right = "".join(secrets.choice(_BACKUP_CODE_CHARS) for _ in range(_BACKUP_CODE_LEN))
        codes.append(f"{left}-{right}")
    return codes


def hash_backup_code(code: str) -> str:
    """Return a hex-encoded SHA-256 digest of a backup code (lowercase).

    The code is normalised to uppercase and stripped of surrounding whitespace
    before hashing so that user input variations are tolerated.
    """
    normalised = code.strip().upper()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def verify_backup_code(code: str, stored_hashes: list[str]) -> int | None:
    """Check a submitted code against a list of stored hashes.

    Returns the **index** of the matching hash (for marking it as used) or
    ``None`` if no match is found.  The comparison is constant-time via
    ``hmac.compare_digest`` to prevent timing side-channels.
    """
    candidate = hash_backup_code(code)
    for i, h in enumerate(stored_hashes):
        if h.get("used"):
            continue
        if hmac.compare_digest(candidate, h["code_hash"]):
            return i
    return None
