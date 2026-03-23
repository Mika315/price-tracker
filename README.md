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
- Local SQLite database (`hotel_tracker.db` by default).
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

## GitHub Actions (`daily_check.yml`)

If you use CLI import from `config/trackers.json`, set `IMPORT_USER_ID` to an existing `users.id` so imported trackers belong to the correct account.
