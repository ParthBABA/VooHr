const { verifySessionToken } = require('../utils/jwt');

const COOKIE_NAME = 'voovr_session';

function requireAuth(req, res, next) {
  const token = req.cookies ? req.cookies[COOKIE_NAME] : null;

  if (!token) {
    return res.status(401).json({ error: 'not_authenticated' });
  }

  try {
    req.auth = verifySessionToken(token); // { sub, orgId, role, email }
    next();
  } catch (err) {
    res.clearCookie(COOKIE_NAME);
    return res.status(401).json({ error: 'session_expired' });
  }
}

module.exports = { requireAuth, COOKIE_NAME };
