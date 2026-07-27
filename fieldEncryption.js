const crypto = require('crypto');
const { wrapDataKey, unwrapDataKey } = require('../config/kms');

const ALGO = 'aes-256-gcm';

/**
 * Encrypts a plain object of string fields (e.g. { name, email }).
 * Generates one fresh Data Encryption Key (DEK) per call, encrypts each
 * field locally with it (fast, no network call per field), then sends
 * ONLY the 32-byte DEK to Cloud KMS to be wrapped. This is envelope
 * encryption — the pattern Google recommends for field-level encryption,
 * since it means one KMS API call per document instead of one per field.
 *
 * Returns { encrypted: { name: '<base64>', email: '<base64>' }, wrappedDek: '<base64>' }
 */
async function encryptFields(fields) {
  const dek = crypto.randomBytes(32);
  const encrypted = {};

  for (const [key, value] of Object.entries(fields)) {
    if (value === undefined || value === null || value === '') continue;

    const iv = crypto.randomBytes(12);
    const cipher = crypto.createCipheriv(ALGO, dek, iv);
    const ciphertext = Buffer.concat([cipher.update(String(value), 'utf8'), cipher.final()]);
    const authTag = cipher.getAuthTag();

    // Store iv + authTag + ciphertext together so decryption is self-contained
    encrypted[key] = Buffer.concat([iv, authTag, ciphertext]).toString('base64');
  }

  const wrappedDek = await wrapDataKey(dek);
  dek.fill(0); // wipe the plaintext DEK from memory once it's safely wrapped

  return { encrypted, wrappedDek: wrappedDek.toString('base64') };
}

/**
 * Reverses encryptFields(). Unwraps the DEK via KMS (1 call), then
 * decrypts every field locally with it.
 */
async function decryptFields(encrypted, wrappedDekBase64) {
  if (!wrappedDekBase64) return {};

  const wrappedDek = Buffer.from(wrappedDekBase64, 'base64');
  const dek = await unwrapDataKey(wrappedDek);

  const decrypted = {};
  for (const [key, value] of Object.entries(encrypted || {})) {
    if (!value) continue;

    const buf = Buffer.from(value, 'base64');
    const iv = buf.subarray(0, 12);
    const authTag = buf.subarray(12, 28);
    const ciphertext = buf.subarray(28);

    const decipher = crypto.createDecipheriv(ALGO, dek, iv);
    decipher.setAuthTag(authTag);
    const plaintext = Buffer.concat([decipher.update(ciphertext), decipher.final()]);
    decrypted[key] = plaintext.toString('utf8');
  }

  return decrypted;
}

module.exports = { encryptFields, decryptFields };
