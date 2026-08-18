from datetime import datetime, timezone

from authlib.integrations.base_client.errors import MismatchingStateError
from authlib.integrations.flask_client import OAuth
from bson import ObjectId
from flask import Blueprint, current_app, redirect, session, url_for

from blind_index import blind_index
from extensions import get_db
from field_encryption import decrypt_fields, encrypt_fields
from login_flow import _login_result_for_user, _record_active_session

oauth = OAuth()
auth_bp = Blueprint("auth", __name__)


def register_google_oauth(app):
    oauth.init_app(app)
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
        client_kwargs={"scope": "openid email profile"},
    )


@auth_bp.route("/google/register")
def google_register():
    """Step 2 of the sign-up flow: verify identity with Google after the
    org-details form (onboarding.html) has already been POSTed to
    /api/onboarding/org and stashed in the session.
    """
    if "pending_org" not in session:
        return redirect("/onboarding.html?error=missing_org")
    session["oauth_flow"] = "register"
    redirect_uri = current_app.config.get("GOOGLE_REDIRECT_URI") or url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/google/signin")
def google_signin():
    """Returning-user sign-in from signin.html."""
    session["oauth_flow"] = "signin"
    redirect_uri = current_app.config.get("GOOGLE_REDIRECT_URI") or url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/google/callback")
def google_callback():
    try:
        token = oauth.google.authorize_access_token()
    except MismatchingStateError:
        # Session cookie was lost between the /signin redirect and this
        # callback — typically because the user opened the app on a
        # different host (e.g. 127.0.0.1 vs localhost), so the browser
        # didn't send the cookie back. Send them back to sign in cleanly
        # instead of a raw 500.
        return redirect("/signin.html?error=session_expired")
    userinfo = token.get("userinfo") or {}
    email = userinfo.get("email")
    if not email:
        return redirect("/signin.html?error=no_account")

    name = userinfo.get("name") or email.split("@")[0]
    picture = userinfo.get("picture")
    google_id = userinfo.get("sub")

    db = get_db()
    flow = session.pop("oauth_flow", "signin")

    if flow == "register":
        pending_org = session.get("pending_org")
        if not pending_org:
            return redirect("/onboarding.html?error=session_expired")

        email_hash = blind_index(email)
        existing_user = db.users.find_one({"email_hash": email_hash})
        if existing_user:
            # This email already has an account — don't silently create a
            # second org/session for it. Send them to sign in instead.
            # (pending_org is left in the session so they can retry with a
            # different Google account without redoing the org form.)
            return redirect("/email-verify.html?error=already_registered")

        session.pop("pending_org", None)

        org_doc = {
            "name": pending_org["orgName"],
            "industry": pending_org["industry"],
            "company_size": pending_org["companySize"],
            "created_at": datetime.now(timezone.utc),
        }
        org_id = db.organizations.insert_one(org_doc).inserted_id

        # Encrypt PII fields with envelope encryption (AES-256-GCM + Cloud KMS)
        encrypted_fields, wrapped_dek = encrypt_fields({
            "name": name,
            "email": email,
        })

        user_doc = {
            "google_id": google_id,
            "email_hash": email_hash,        # blind index for lookups
            "encrypted": encrypted_fields,    # { name: "<base64>", email: "<base64>" }
            "wrapped_dek": wrapped_dek,       # KMS-wrapped DEK
            "picture": picture,
            "org_id": org_id,
            "role": "admin",
            "created_at": datetime.now(timezone.utc),
            "last_login": datetime.now(timezone.utc),
        }
        user_id = db.users.insert_one(user_doc).inserted_id

        session.permanent = True
        session["user_id"] = str(user_id)
        session["org_id"] = str(org_id)
        session["just_registered"] = True
        _record_active_session(db, user_id)
        return redirect("/onboarding-complete.html")

    # --- sign-in flow ---
    email_hash = blind_index(email)
    user = db.users.find_one({"email_hash": email_hash})
    if not user:
        return redirect("/signin.html?error=no_account")

    db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_login": datetime.now(timezone.utc)}},
    )

    # Decrypt PII for the session
    pii = decrypt_fields(user.get("encrypted"), user.get("wrapped_dek", ""))
    session.permanent = True
    session["user_id"] = str(user["_id"])
    session["org_id"] = str(user["org_id"])
    session["user_name"] = pii.get("name", "")
    session["user_email"] = pii.get("email", "")
    _record_active_session(db, user["_id"])

    # TOTP gate: redirect to verification or forced setup instead of
    # straight to the dashboard when needed.
    result = _login_result_for_user(db, user)
    return redirect(result["redirect"])


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return {"ok": True}
