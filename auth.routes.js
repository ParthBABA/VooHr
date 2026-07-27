const express = require('express');
const crypto = require('crypto');

const userService = require('../services/userService');
const Organization = require('../models/Organization');
const { buildAuthUrl, exchangeCodeForProfile } = require('../config/google');
const {
  signStateToken,
  verifyStateToken,
  signSessionToken,
  verifyPendingOrgToken,
} = require('../utils/jwt');
const { COOKIE_NAME } = require('../middleware/requireAuth');

const router = express.Router();
const CLIENT_URL = process.env.CLIENT_URL || 'http://localhost:5500';

const PENDING_ORG_COOKIE = 'voovr_pending_org';

const sessionCookieOptions = {
  httpOnly: true,
  secure: process.env.NODE_ENV === 'production',
  sameSite: 'lax',
  maxAge: 7 * 24 * 60 * 60 * 1000, // 7 days, matches session JWT expiry
};

/* ────────────────────────────────────────────────────────────────
   GET /auth/google/signin
   Entry point for signin.html's "Continue with Google" button.
   No org involved — this account must already exist.
──────────────────────────────────────────────────────────────── */
router.get('/google/signin', (req, res) => {
  const state = signStateToken({
    flow: 'signin',
    nonce: crypto.randomBytes(8).toString('hex'),
  });
  res.redirect(buildAuthUrl(state));
});

/* ────────────────────────────────────────────────────────────────
   GET /auth/google/register
   Entry point for email-verify.html's "Continue with Google" button.
   Reads the org id from the pending-org cookie set by
   POST /api/onboarding/org — the frontend never has to carry it.
──────────────────────────────────────────────────────────────── */
router.get('/google/register', (req, res) => {
  const pendingToken = req.cookies ? req.cookies[PENDING_ORG_COOKIE] : null;

  if (!pendingToken) {
    // Cookie missing or expired (>15 min since the onboarding form was submitted)
    return res.redirect(`${CLIENT_URL}/email-verify.html?error=session_expired`);
  }

  let orgId;
  try {
    ({ orgId } = verifyPendingOrgToken(pendingToken));
  } catch (err) {
    return res.redirect(`${CLIENT_URL}/email-verify.html?error=session_expired`);
  }

  const state = signStateToken({
    flow: 'register',
    orgId,
    nonce: crypto.randomBytes(8).toString('hex'),
  });
  res.redirect(buildAuthUrl(state));
});

/* ────────────────────────────────────────────────────────────────
   GET /auth/google/callback
   Shared by both flows; `state` (signed, verified below) says which one.
──────────────────────────────────────────────────────────────── */
router.get('/google/callback', async (req, res) => {
  const { code, error: googleError, state } = req.query;

  if (googleError) {
    return res.redirect(`${CLIENT_URL}/signin.html?error=session_expired`);
  }

  let statePayload;
  try {
    statePayload = verifyStateToken(state);
  } catch (err) {
    return res.redirect(`${CLIENT_URL}/signin.html?error=session_expired`);
  }

  const { flow, orgId } = statePayload;
  const fallbackPage = flow === 'register' ? 'email-verify.html' : 'signin.html';

  let profile;
  try {
    profile = await exchangeCodeForProfile(code);
  } catch (err) {
    console.error('[auth] token exchange failed:', err.message);
    return res.redirect(`${CLIENT_URL}/${fallbackPage}?error=session_expired`);
  }

  const { sub: googleId, email, name, picture, email_verified: emailVerified } = profile;

  if (!emailVerified) {
    return res.redirect(`${CLIENT_URL}/${fallbackPage}?error=session_expired`);
  }

  try {
    if (flow === 'signin') {
      return await handleSigninFlow({ googleId, email, res });
    }
    return await handleRegisterFlow({ googleId, email, name, picture, orgId, res });
  } catch (err) {
    console.error('[auth] callback failed:', err.message);
    return res.redirect(`${CLIENT_URL}/${fallbackPage}?error=session_expired`);
  }
});

/* ── Sign-in: user must already exist ── */
async function handleSigninFlow({ googleId, email, res }) {
  const user = await userService.findByGoogleId(googleId);

  if (!user) {
    return res.redirect(`${CLIENT_URL}/signin.html?error=no_account`);
  }

  user.lastLoginAt = new Date();
  await user.save();

  const token = signSessionToken(user);
  res.cookie(COOKIE_NAME, token, sessionCookieOptions);
  return res.redirect(`${CLIENT_URL}/dashboard.html`);
}

/* ── Register: org must already exist (created in the onboarding step),
   this creates/attaches the admin user and marks the org verified ── */
async function handleRegisterFlow({ googleId, email, name, picture, orgId, res }) {
  // Pending-org cookie is single-use regardless of outcome below
  res.clearCookie(PENDING_ORG_COOKIE);

  const org = await Organization.findById(orgId);
  if (!org) {
    return res.redirect(`${CLIENT_URL}/email-verify.html?error=session_expired`);
  }

  const existing = await userService.findByGoogleId(googleId);
  if (existing && existing.orgId) {
    // This Google account is already tied to an organization somewhere
    return res.redirect(`${CLIENT_URL}/email-verify.html?error=already_registered`);
  }

  let user = existing;
  if (!user) {
    user = await userService.createUser({
      googleId,
      email,
      name,
      avatar: picture,
      role: 'admin', // first Google account to verify an org becomes its admin
      orgId: org._id,
    });
  } else {
    user.orgId = org._id;
    await user.save();
  }

  if (!org.verified) {
    org.verified = true;
    org.domain = email.split('@')[1] || null;
    org.createdBy = org.createdBy || user._id;
    await org.save();
  }

  const token = signSessionToken(user);
  res.cookie(COOKIE_NAME, token, sessionCookieOptions);
  return res.redirect(`${CLIENT_URL}/onboarding-complete.html`);
}

/* POST /auth/logout */
router.post('/logout', (req, res) => {
  res.clearCookie(COOKIE_NAME);
  res.clearCookie(PENDING_ORG_COOKIE);
  res.status(200).json({ ok: true });
});

module.exports = router;
