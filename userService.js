const User = require('../models/User');
const { encryptFields, decryptFields } = require('../utils/fieldEncryption');
const { blindIndex } = require('../utils/hash');

/** Creates a new User, encrypting name/email before they ever reach Mongoose. */
async function createUser({ googleId, name, email, avatar, role, orgId }) {
  const { encrypted, wrappedDek } = await encryptFields({ name, email });

  return User.create({
    googleId,
    emailHash: blindIndex(email),
    encryptedFields: encrypted,
    wrappedDek,
    avatar,
    role,
    orgId,
  });
}

async function findByGoogleId(googleId) {
  return User.findOne({ googleId });
}

async function findByEmail(email) {
  return User.findOne({ emailHash: blindIndex(email) });
}

/** Decrypts a User document's name/email into a plain object for API responses. */
async function toDecrypted(userDoc) {
  if (!userDoc) return null;
  const { name, email } = await decryptFields(userDoc.encryptedFields, userDoc.wrappedDek);
  return {
    id: userDoc._id,
    googleId: userDoc.googleId,
    name,
    email,
    avatar: userDoc.avatar,
    role: userDoc.role,
    orgId: userDoc.orgId,
    lastLoginAt: userDoc.lastLoginAt,
  };
}

module.exports = { createUser, findByGoogleId, findByEmail, toDecrypted };
