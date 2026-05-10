# Astral Hotels Price Tracker

**Status:** Ongoing Project

A small Flask web app to monitor **Astral Hotels** booking prices (`astralhotels.co.il` only). Users sign in, paste an Astral URL with dates, set optional “price paid” and alert rules, and receive **email alerts** when conditions are met (SMTP required on the server).

## Scope

- **Supported:** booking URLs on **astralhotels.co.il** (official Astral site).
- **Rejected:** any other hotel or domain — the API returns a clear validation error.

## Features

- Register / sign in / sign out.
- **Forgot password:** request a reset link by email (requires SMTP). Link opens the app with `?reset=…` to set a new password.
- Per-user trackers and price history (PostgreSQL in production, SQLite locally).
- Scheduled background checks (`CHECK_INTERVAL_MINUTES`, default 30).
- Optional ntfy topic per tracker (legacy push).

## Technologies Used

- Python
- Flask
- SQLite locally / PostgreSQL in production
- Playwright
- APScheduler
- SMTP / Resend email notifications
- GitHub Actions

## Run locally

```bash
pip install -r requirements.txt
playwright install chromium
set SECRET_KEY=your-random-secret
python app.py
```

Open `http://127.0.0.1:5000`, create an account, add an **Astral** booking URL.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL URI (e.g. Supabase). If unset, SQLite `hotel_tracker.db` is used. |
| `SECRET_KEY` | Flask session signing (required in production). |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM` | Outbound email (alerts + password reset). |
| `SMTP_USE_TLS` / `SMTP_USE_SSL` | Match your provider (587 + TLS vs 465 + SSL). |
| `CHECK_INTERVAL_MINUTES` | Scheduler interval (default `30`). |
| `SESSION_COOKIE_SECURE` | Set `true` behind HTTPS. |
| `PUBLIC_APP_URL` or `RENDER_EXTERNAL_URL` | Base URL for password-reset links (e.g. `https://your-app.onrender.com`). If unset, the current request host is used. |
| `IMPORT_USER_ID` | For CLI import (`python src/main.py` + `config/trackers.json`): existing `users.id`. |

## Password reset

1. Configure SMTP on the same host as the app.
2. Set `PUBLIC_APP_URL` (or rely on `RENDER_EXTERNAL_URL` on Render) so reset links point to your public HTTPS URL.
3. User clicks **Forgot password?**, enters email, opens the link, sets a new password (valid **1 hour**).

## Deploy (e.g. Render + Supabase)

1. Create a Supabase project and copy the Postgres URI (`sslmode=require`).
2. In Render → Environment: `DATABASE_URL`, `SECRET_KEY`, SMTP vars, and optionally `PUBLIC_APP_URL` / `RENDER_EXTERNAL_URL`.
3. Redeploy after env changes.

## GitHub Actions (`daily_check.yml`)

Runs `python src/main.py` on a schedule. `config/trackers.json` entries must use **Astral-only** URLs. Set `IMPORT_USER_ID` to your user id.

## Legal / product note

This is an independent demo project. Astral Hotels is a trademark of its owner; this app is not affiliated with or endorsed by Astral Hotels. Use only in line with the site’s terms and robots/API policies.
