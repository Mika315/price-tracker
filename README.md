# Hotel Price Tracker

Track hotel booking links and receive email alerts when prices move based on your tracker settings.

## What users see

- Register/login with email and password.
- Add a tracker with hotel URL, meal rules, and optional `Price paid`.
- Choose alert direction:
  - `Only when price drops`
  - `Any change`
  - `Only when price rises`
- Click `Check now` or wait for automatic checks.
- Get email alerts when your tracker conditions are met.

## Official support scope

This app is officially optimized for:

- **Astral**
- **Dan**
- **Fattal**

Other hotel websites may still work, but are treated as **best effort** (no guaranteed accuracy).

## Technical features

- Per-user data isolation (each account sees only its own trackers/history).
- Dual DB mode:
  - **Production**: PostgreSQL via `DATABASE_URL` (recommended for Render/Supabase).
  - **Local fallback**: SQLite (`hotel_tracker.db`) when `DATABASE_URL` is not set.
- Background scheduler for automatic checks.
- Optional ntfy topic per tracker.

## Run locally

```bash
pip install -r requirements.txt
playwright install chromium
set SECRET_KEY=your-random-secret
python app.py
```

Open `http://127.0.0.1:5000`, create an account, and add trackers.

## Email alerts setup (for deployment/admin)

Email sending is configured on the server where `app.py` runs.

Required environment variables:

- `DATABASE_URL` (for cloud persistence, e.g. Supabase Postgres)
- `SMTP_HOST`
- `SMTP_PORT` (`587` or `465`)
- `SMTP_USER`
- `SMTP_PASSWORD`
- `MAIL_FROM`

Recommended:

- `SECRET_KEY` (required in production)
- `CHECK_INTERVAL_MINUTES` (default `30`)
- `SESSION_COOKIE_SECURE=true` (when using HTTPS)
- `SMTP_USE_TLS=true` (default for 587)
- `SMTP_USE_SSL=true` (for 465)

## Troubleshooting email

If email does not arrive:

1. Verify email env vars are set on the same server running the app.
2. Restart the app after env changes.
3. In the tracker, set `Minimum %` to `0` while testing.
4. For `Any change` / `Only when price rises`, the first run sets baseline; next run can trigger.
5. Check spam folder.
6. Check `tracker.log` for email errors.

## Persistent database on Render (free setup with Supabase)

1. Create a Supabase project.
2. Open **Project Settings -> Database** in Supabase.
3. Copy the Postgres connection string (URI), and ensure it includes `sslmode=require`.
4. In Render service -> **Environment**, add:
   - `DATABASE_URL=<your-supabase-postgres-uri>`
5. Keep your SMTP env vars as before.
6. Deploy again (`Manual Deploy -> Deploy latest commit`).

When `DATABASE_URL` is set, the app automatically uses PostgreSQL and your data persists across restarts/sleep.

## GitHub Actions (`daily_check.yml`)

If you use CLI import from `config/trackers.json`, set `IMPORT_USER_ID` to an existing `users.id` so imported trackers belong to the correct account.
