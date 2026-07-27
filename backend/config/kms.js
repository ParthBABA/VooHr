const { KeyManagementServiceClient } = require('@google-cloud/kms');

const client = new KeyManagementServiceClient();

// Uses gcloud's Application Default Credentials automatically — no JSON
// key file needed locally (set up via `gcloud auth application-default login`).
// In production, this same code authenticates via the service account
// attached to the Cloud Run/Compute Engine instance, again with no key file.

const REQUIRED_ENV = ['GCP_PROJECT_ID', 'GCP_KMS_LOCATION', 'GCP_KMS_KEY_RING', 'GCP_KMS_KEY'];
REQUIRED_ENV.forEach((key) => {
  if (!process.env[key]) {
    console.warn(`[kms] warning: ${key} is not set — field encryption will fail until it is.`);
  }
});

const KEY_NAME = client.cryptoKeyPath(
  process.env.GCP_PROJECT_ID,
  process.env.GCP_KMS_LOCATION,
  process.env.GCP_KMS_KEY_RING,
  process.env.GCP_KMS_KEY
);

/**
 * Sends a small (32-byte) Data Encryption Key to Cloud KMS to be wrapped.
 * Cloud KMS stores which key VERSION was used inside the ciphertext itself,
 * so key rotation is handled automatically — decrypt() below never needs to
 * be told which version to use.
 */
async function wrapDataKey(dataKeyBuffer) {
  const [result] = await client.encrypt({
    name: KEY_NAME,
    plaintext: dataKeyBuffer,
  });
  return result.ciphertext; // Buffer
}

async function unwrapDataKey(wrappedKeyBuffer) {
  const [result] = await client.decrypt({
    name: KEY_NAME,
    ciphertext: wrappedKeyBuffer,
  });
  return result.plaintext; // Buffer
}

module.exports = { wrapDataKey, unwrapDataKey };
