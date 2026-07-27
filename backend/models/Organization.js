const mongoose = require('mongoose');

const organizationSchema = new mongoose.Schema(
  {
    name: { type: String, required: true, trim: true },
    industry: {
      type: String,
      required: true,
      enum: [
        'Technology', 'Finance', 'Healthcare', 'Education', 'Manufacturing',
        'Retail', 'Real Estate', 'Media', 'Legal', 'Consulting', 'Other',
      ],
    },
    companySize: {
      type: String,
      required: true,
      enum: ['1-10', '11-50', '51-200', '201-1000', '1000+'],
    },
    // Google Workspace domain the org is verified against (e.g. "acme.com"),
    // set once the admin completes Google verification in email-verify.html.
    domain: { type: String, default: null, trim: true, lowercase: true },
    verified: { type: Boolean, default: false },
    createdBy: { type: mongoose.Schema.Types.ObjectId, ref: 'User', default: null },
  },
  { timestamps: true }
);

module.exports = mongoose.model('Organization', organizationSchema);
