import base64
import hashlib
import imghdr
import logging
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session
from pymongo.errors import PyMongoError

from email_service import send_support_email
from extensions import check_rate_limit, client_ip, get_db, record_rate_limit_event

logger = logging.getLogger(__name__)

support_bp = Blueprint("support", __name__)

ALLOWED_TOPICS = {
    "General question",
    "Bug or technical problem",
    "Analysis accuracy or failed analysis",
    "Account or sign-in",
    "Privacy or data request",
    "Something else",
}

# Image validation
ALLOWED_MAGIC_BYTES = {
    "jpeg": b"\xff\xd8\xff",
    "png": b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a",
    "webp": b"RIFF",  # We also check for 'WEBP' at byte 8
    "gif": b"GIF8",
}

MAX_ATTACHMENTS = 4
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB per file
MAX_TOTAL_SIZE = 15 * 1024 * 1024  # 15 MB total

def _validate_image(file_storage) -> dict | None:
    """Validate magic bytes and size, return base64 dict for Brevo or None."""
    data = file_storage.read(MAX_FILE_SIZE + 1)
    if len(data) > MAX_FILE_SIZE:
        return None
    if not data:
        return None

    # Magic byte check
    is_valid = False
    for ext, magic in ALLOWED_MAGIC_BYTES.items():
        if ext == "webp":
            if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
                is_valid = True
                break
        else:
            if data.startswith(magic):
                is_valid = True
                break
                
    if not is_valid:
        return None
        
    filename = file_storage.filename or "attachment"
    # Basic extension validation against our allowed types to prevent spoofing
    # in the filename sent in the email.
    if not any(filename.lower().endswith(f".{ext}") for ext in ("jpg", "jpeg", "png", "webp", "gif")):
        filename += ".bin"

    return {
        "name": filename,
        "content": base64.b64encode(data).decode("ascii")
    }

@support_bp.route("/support/contact", methods=["POST"])
def contact_submit():
    # 1. Rate Limiting (per IP and per email)
    ip = client_ip()
    db = get_db()
    
    email = (request.form.get("email") or "").strip()
    
    # Check IP limits: 5 per hour, 20 per day
    ip_hour_key = f"contact_ip_1h:{ip}"
    ip_day_key = f"contact_ip_24h:{ip}"
    email_day_key = f"contact_email_24h:{email}" if email else None
    
    ok1, retry1 = check_rate_limit(db, ip_hour_key, 5, 3600)
    ok2, retry2 = check_rate_limit(db, ip_day_key, 20, 86400)
    ok3, retry3 = True, 0
    if email_day_key:
        ok3, retry3 = check_rate_limit(db, email_day_key, 10, 86400)
        
    if not (ok1 and ok2 and ok3):
        return jsonify({"error": "Too many requests. Please try again later."}), 429

    # 2. Honeypot check
    website = (request.form.get("website") or "").strip()
    if website:
        # Silently succeed for bots
        return jsonify({"ok": True}), 200
        
    # 3. Server-side Validation
    topic = (request.form.get("topic") or "").strip()
    subject = (request.form.get("subject") or "").strip()
    message = (request.form.get("message") or "").strip()
    
    if not email or "@" not in email:
        return jsonify({"error": "Valid email is required."}), 400
    if topic not in ALLOWED_TOPICS:
        return jsonify({"error": "Invalid topic selected."}), 400
    if len(subject) > 120:
        return jsonify({"error": "Subject is too long."}), 400
    if len(message) < 10 or len(message) > 4000:
        return jsonify({"error": "Message must be between 10 and 4000 characters."}), 400
        
    # Strip control chars from message (except basic formatting)
    message = "".join(c for c in message if c.isprintable() or c in "\n\r\t")
    
    # 4. Handle Attachments
    files = request.files.getlist("attachments")
    if len(files) > MAX_ATTACHMENTS:
        return jsonify({"error": f"Maximum {MAX_ATTACHMENTS} attachments allowed."}), 400
        
    attachments = []
    total_size = 0
    for f in files:
        if not f.filename:
            continue
        att = _validate_image(f)
        if not att:
            return jsonify({"error": f"File {f.filename} is too large or not a valid image."}), 400
        # Calculate approximate byte size of base64
        size_bytes = (len(att["content"]) * 3) / 4
        total_size += size_bytes
        if total_size > MAX_TOTAL_SIZE:
            return jsonify({"error": "Total attachment size exceeds 15MB limit."}), 400
        attachments.append(att)

    # 5. Identity
    org_id = None
    user_id = None
    if session.get("user_id"):
        user_id = session.get("user_id")
        user = db.users.find_one({"_id": user_id}) if hasattr(db.users, "find_one") else None
        if user:
            org_id = user.get("org_id")

    # 6. Database persistence
    ip_hash = hashlib.sha256(ip.encode()).hexdigest()
    doc = {
        "topic": topic,
        "email": email,
        "subject": subject,
        "message": message,
        "has_attachments": len(attachments) > 0,
        "org_id": org_id,
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc),
        "user_agent": request.headers.get("User-Agent", "")[:200],
        "ip_hash": ip_hash,
        "status": "new",
    }
    
    try:
        db.support_requests.insert_one(doc)
    except PyMongoError as e:
        logger.error("Failed to save support request: %s", e)
        return jsonify({"error": "Internal database error."}), 500

    # 7. Send email
    success = send_support_email(email, topic, subject, message, attachments)
    if not success:
        return jsonify({"error": "Failed to send email. Please try again or use the support email address."}), 500

    # Record rate limits
    record_rate_limit_event(db, ip_hour_key, 3600)
    record_rate_limit_event(db, ip_day_key, 86400)
    if email_day_key:
        record_rate_limit_event(db, email_day_key, 86400)

    return jsonify({"ok": True}), 200
