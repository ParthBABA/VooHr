const { OAuth2Client } = require('google-auth-library');

const REQUIRED_ENV = ['GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_CALLBACK_URL'];
REQUIRED_ENV.forEach((key) => {
  if (!process.env[key]) {
    console.warn(`[google] warning: ${key} is not set — Google sign-in will fail until it is.`);
  }
});

const oauthClient = new OAuth2Client(
  process.env.GOOGLE_CLIENT_ID,
  process.env.GOOGLE_CLIENT_SECRET,
  process.env.GOOGLE_CALLBACK_URL
);

const SCOPES = ['openid', 'email', 'profile'];

/**
 * Builds the URL we redirect the browser to for Google's consent screen.
 * `state` carries flow-specific data (flow=signin|register, orgId, csrf token)
 * through the redirect round-trip, since we are not using server sessions.
 */
function buildAuthUrl(state) {
  return oauthClient.generateAuthUrl({
    access_type: 'online',
    scope: SCOPES,
    state,
    prompt: 'select_account',
  });
}

/**
 * Exchanges the ?code= from Google's callback for tokens, then verifies
 * the ID token and returns the verified profile payload.
 */
async function exchangeCodeForProfile(code) {
  const { tokens } = await oauthClient.getToken(code);

  const ticket = await oauthClient.verifyIdToken({
    idToken: tokens.id_token,
    audience: process.env.GOOGLE_CLIENT_ID,
  });

  const payload = ticket.getPayload();
  // payload: { sub, email, email_verified, name, picture, hd, ... }
  return payload;
}

module.exports = { oauthClient, buildAuthUrl, exchangeCodeForProfile };
