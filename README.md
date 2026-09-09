# VooVr (Flask + MongoDB + Google OAuth)

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

## Running tests

Install the dev dependencies (includes pytest and the security scanners), then run the suite:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt
pytest
```

Tests are `unittest`-style classes discovered via pytest (`test_*.py` in the
repo root). They use an in-memory Mongo facade, so no real MongoDB, Google, or
external network is required. Imports need a couple of environment variables —
set them in `.env` (from `.env.example`) or export them, e.g. `SECRET_KEY`,
`HASH_INDEX_SECRET`, and `JWT_SECRET`. CI runs the same suite on every push /
pull request to `main`.

## Security scanning

Two report-only scanners run in CI (`.github/workflows/ci.yml`, `security` job)
and can be run locally:

- **bandit** — static analysis that flags common security issues in Python code
  (hardcoded secrets, dangerous eval/exec, unsafe subprocess, bare `except`,
  etc.).
  ```bash
  bandit -r . -x '*/test_*.py,*/static/*,*/.ven*,*/venv*,*/node_modules/*,*/docs/*,*/marketing-video/*,*/templates/*'
  ```

- **pip-audit** — audits the dependencies in `requirements.txt` against known
  vulnerability databases (OSV) to catch CVEs in direct and transitive packages.
  ```bash
  pip-audit -r requirements.txt
  ```

Both are currently in **report-only** mode: they upload their output as a CI
artifact but do not fail the build. Review the artifacts and address findings,
then flip them to fail the build once the noise floor is acceptable.

## Frontend presentation

- **Brand:** VooVr across pages and transactional emails.
- **Typography:** `static/style.css` loads Inter and Lato for the shared heading
  and body tokens. Auth/legal components use the same light/dark surface tokens.
- **Shared head:** `templates/shared-head.html` provides favicon, description,
  Open Graph, and Twitter metadata. `page_rendering.py` inserts it into the
  `<!-- shared-head -->` slot when Flask serves a static HTML page, including
  legacy HTML URLs. The landing template includes it directly. Share URLs omit
  query parameters; account/workspace pages are marked `noindex`.
- **Feedback:** `static/ui-feedback.js` exposes `VooVrUI.show`, `clear`, and
  async `ask`. Confirmations use a styled dialog with keyboard focus management;
  error and success banners use accessible live regions and plain text.
- **Responsive scale:** 480 / 768 / 1024 / 1280px. Mobile workspace navigation
  uses a compact icon rail with accessible labels. Wide tables scroll locally.
- **Inline styles:** shared auth/legal markup uses CSS classes. Runtime styles
  for chart values, animations, and visibility are still used where needed.
- **Registration:** `/signup` collects organization details and calls the
  existing `/api/onboarding/org` → `/auth/email/start` → `/verify-otp` flow.
- **Password recovery:** reset email delivery is not implemented. The endpoint
  returns an explicit unavailable response instead of claiming an email was sent.

### Presentation checks

```bash
python -m pytest test_frontend_presentation.py -q
```

For the browser audit, install Playwright in your development environment and
run `node scripts/check_ui.cjs` with it on Node's module search path (`NODE_PATH`
can point to an external installation). The audit uses installed Microsoft Edge,
starts a local presentation-only server, mocks API responses, and checks all 24
pages at the four breakpoints, auth feedback, confirmation cancellation/acceptance,
theme changes, and directory pagination beyond 200 employees. It does not connect
to the production database. `scripts/preview_ui.py` can also be run directly to
inspect `/preview/<filename>.html` locally.
