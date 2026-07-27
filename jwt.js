const jwt = require('jsonwebtoken');

const SESSION_SECRET = process.env.JWT_SECRET;
const STATE_SECRET = process.env.OAUTH_STATE_SECRET || process.env.JWT_SECRET;

if (!SESSION_SECRET) {
  console.warn('[jwt] warning: JWT_SECRET is not set — sessions will fail.');
}

/* ── Session token (issued after login, stored in an httpOnly cookie) ── */

function signSessionToken(user) {
  return jwt.sign(
    {
      sub: user._id.toString(),
      orgId: user.orgId ? user.orgId.toString() : null,
      role: user.role,
    },
    SESSION_SECRET,
    { expiresIn: '7d' }
  );
}

function verifySessionToken(token) {
  return jwt.verify(token, SESSION_SECRET);
}

/* ── OAuth "state" token (short-lived, carries flow + orgId through the
     Google redirect round-trip so we don't need server-side session storage) ── */

function signStateToken(payload) {
  return jwt.sign(payload, STATE_SECRET, { expiresIn: '10m' });
}

function verifyStateToken(token) {
  return jwt.verify(token, STATE_SECRET);
}

/* ── Pending-org token (short-lived, stored in its own httpOnly cookie
     right after the onboarding form is submitted). This is what lets
     /auth/google/register know which org to attach the new user to,
     without the frontend having to pass org_id around itself. ── */

function signPendingOrgToken(payload) {
  return jwt.sign(payload, STATE_SECRET, { expiresIn: '15m' });
}

function verifyPendingOrgToken(token) {
  return jwt.verify(token, STATE_SECRET);
}

module.exports = {
  signSessionToken,
  verifySessionToken,
  signStateToken,
  verifyStateToken,
  signPendingOrgToken,
  verifyPendingOrgToken,
};
