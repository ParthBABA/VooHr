const crypto = require('crypto');

const SECRET = process.env.HASH_INDEX_SECRET || process.env.JWT_SECRET;

if (!SECRET) {
  console.warn('[hash] warning: HASH_INDEX_SECRET is not set — email lookups will fail.');
}

/**
 * Deterministic HMAC of a value, used ONLY as a lookup index — never
 * displayed. Same input always produces the same hash, which is what lets
 * us do User.findOne({ emailHash }) and enforce a unique index on it, even
 * though the actual `email` field is randomly (non-deterministically)
 * encrypted and therefore useless for direct querying.
 *
 * This does leak "these two records have the same email" to anyone with
 * raw DB access — an acceptable, standard tradeoff for a lookup index.
 */
function blindIndex(value) {
  return crypto
    .createHmac('sha256', SECRET)
    .update(String(value).trim().toLowerCase())
    .digest('hex');
}

module.exports = { blindIndex };
