# VooHR Backend (Flask + MongoDB + Google OAuth)

Powers the "get started" flow for the VooVr frontend: creating an organization,
verifying identity with Google, and signing back in.

## How the flow works

**New user (registration):**
1. `onboarding.html` — user fills in Organization Name / Industry / Company Size.
   Submitting the form calls `POST /api/onboarding/org`, which stashes those
   details in the server-side session (nothing is written to the DB yet).
2. `email-verify.html` — "Continue with Google" sends the browser to
   `/auth/google/register`, which starts the Google OAuth flow.
3. `/auth/google/callback` — once Google confirms the user's identity, the
   backend creates the `organizations` document and a `users` document
   (role `admin`) tied to it, logs the user in (session cookie), and redirects
   to `onboarding-complete.html`.
4. `onboarding-complete.html` — calls `GET /api/me` to show the real name and
   org name, then links to `dashboard.html`.

**Returning user (sign-in):**
1. `signin.html` — "Continue with Google" sends the browser to
   `/auth/google/signin`.
2. `/auth/google/callback` looks the email up in `users`. If found, logs them
   in and redirects to `dashboard.html`. If not found, redirects back to
   `signin.html?error=no_account`, which the page shows as a banner.

`dashboard.html` calls `GET /api/me` on load; if there's no valid session it
redirects to `signin.html`. Signing out (`POST /auth/logout`) clears the
session.

## Data model (MongoDB)

- `organizations`: `{ name, industry, company_size, created_at }`
- `users`: `{ google_id, email, name, picture, org_id, role, created_at, last_login }`

## Setup

1. Copy `.env.example` to `.env` and fill in:
   - `MONGODB_URI` — your existing MongoDB connection string.
   - `SECRET_KEY` — any long random string.
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — from a Google Cloud OAuth
     Client (type "Web application") at
     https://console.cloud.google.com/apis/credentials.
     Add this as an **Authorized redirect URI**:
     `http://localhost:5000/auth/google/callback`
     (swap the host for your real domain in production, and add both if you
     test locally and deploy).

### Deploying to Railway (field encryption via Google Cloud KMS)

Field-level encryption uses Google Cloud KMS. On Railway:

1. Open your project → **Variables**.
2. Add `GOOGLE_CREDENTIALS_JSON` and **paste the full service-account JSON as
   its value** (single-line string — Railway stores it as a secret, no file
   needed at runtime). Make sure it is a Railway *secret*, not a public
   variable, and never commit it.
3. Add `GCP_PROJECT_ID`, `GCP_KMS_LOCATION` (e.g. `asia-south1`),
   `GCP_KMS_KEY_RING`, and `GCP_KMS_KEY` matching your key in Google Cloud
   Console.

Locally, either set `GOOGLE_CREDENTIALS_JSON` in `.env` or use
`GOOGLE_APPLICATION_CREDENTIALS` pointing at the local key file.

2. Install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Run it:
   ```bash
   python app.py
   ```
   Visit `http://localhost:5000/onboarding.html` to try the sign-up flow, or
   `http://localhost:5000/signin.html` to sign in.

## Notes / next steps

- Only the register → verify → complete → dashboard flow (plus sign-in and
  sign-out) is wired to the backend right now, per your request. `directory.html`
  and `sync.html` are still static/mock data — happy to wire those up next.
- The dashboard's stats, alerts, and employee table are still hardcoded
  placeholders; only the sidebar user info (name/role/avatar) and sign-out are
  live.
- Sessions are Flask's signed cookie sessions (no server-side session store
  needed). Fine for this scale; swap for `Flask-Session` + Mongo if you want
  server-side session revocation later.
- I also fixed a pre-existing bug in the original files: `dashboard.html`,
  `directory.html`, and `sync.html` linked to `css/style.css`, but the actual
  file is `style.css` — so those pages were rendering unstyled. Fixed to point
  at `style.css` directly.
