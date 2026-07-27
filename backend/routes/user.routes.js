const express = require('express');
const User = require('../models/User');
const userService = require('../services/userService');
const { requireAuth } = require('../middleware/requireAuth');

const router = express.Router();

// GET /api/me — used by onboarding-complete.html and app.js.
// Response is flat: { name, email, role, avatar, organization } to match
// what onboarding-complete.html reads (data.name, data.organization.name).
router.get('/me', requireAuth, async (req, res, next) => {
  try {
    const userDoc = await User.findById(req.auth.sub).populate(
      'orgId',
      'name industry companySize verified'
    );
    if (!userDoc) return res.status(401).json({ error: 'not_authenticated' });

    const decrypted = await userService.toDecrypted(userDoc);

    res.json({
      name: decrypted.name,
      email: decrypted.email,
      role: decrypted.role,
      avatar: decrypted.avatar,
      organization: userDoc.orgId
        ? {
            name: userDoc.orgId.name,
            industry: userDoc.orgId.industry,
            companySize: userDoc.orgId.companySize,
            verified: userDoc.orgId.verified,
          }
        : null,
    });
  } catch (err) {
    next(err);
  }
});

module.exports = router;
