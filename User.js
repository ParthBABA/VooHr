const mongoose = require('mongoose');

const userSchema = new mongoose.Schema(
  {
    googleId: { type: String, required: true, unique: true, index: true },

    // emailHash is a deterministic HMAC used ONLY for lookup/uniqueness —
    // see utils/hash.js. It is never displayed to anyone.
    emailHash: { type: String, required: true, unique: true, index: true },

    // encryptedFields.name / .email are AES-256-GCM ciphertext (base64),
    // each decryptable only via wrappedDek. See utils/fieldEncryption.js.
    encryptedFields: {
      name: { type: String, required: true },
      email: { type: String, required: true },
    },
    wrappedDek: { type: String, required: true }, // Cloud KMS-wrapped data key, base64

    avatar: { type: String, default: null },
    role: { type: String, enum: ['admin', 'manager', 'member'], default: 'member' },
    orgId: { type: mongoose.Schema.Types.ObjectId, ref: 'Organization', default: null },
    lastLoginAt: { type: Date, default: Date.now },
  },
  { timestamps: true }
);

module.exports = mongoose.model('User', userSchema);
