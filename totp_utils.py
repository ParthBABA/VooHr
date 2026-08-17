"""TOTP (Time-based One-Time Password) utilities for VooHr.

Provides secret generation, provisioning URI creation, QR code rendering,
and time-window-aware code verification using the pyotp library.
"""

import io
import base64

import pyotp
import qrcode


def generate_secret() -> str:
    """Generate a new random TOTP secret (32 bytes, base32-encoded)."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str, issuer_name: str = "VooHr") -> str:
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
