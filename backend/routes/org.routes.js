const express = require('express');
const Organization = require('../models/Organization');
const { signPendingOrgToken } = require('../utils/jwt');

const router = express.Router();

const PENDING_ORG_COOKIE = 'voovr_pending_org';
const pendingOrgCookieOptions = {
  httpOnly: true,
  secure: process.env.NODE_ENV === 'production',
  sameSite: 'lax',
  maxAge: 15 * 60 * 1000, // 15 minutes — just long enough to finish Google verification
};

// POST /api/onboarding/org
// Called from onboarding.html's inline form handler. Creates a draft
// (unverified) org, then remembers it via a short-lived httpOnly cookie —
// NOT via a response field — since the frontend no longer carries org_id
// through localStorage or the URL. /auth/google/register reads this cookie.
router.post('/', async (req, res, next) => {
  try {
    const { orgName, industry, companySize } = req.body || {};

    if (!orgName || !industry || !companySize) {
      return res.status(400).json({ error: 'missing_fields' });
    }

    const org = await Organization.create({
      name: orgName.trim(),
      industry,
      companySize,
    });

    const pendingToken = signPendingOrgToken({ orgId: org._id.toString() });
    res.cookie(PENDING_ORG_COOKIE, pendingToken, pendingOrgCookieOptions);

    res.status(201).json({ ok: true });
  } catch (err) {
    next(err);
  }
});

module.exports = router;
