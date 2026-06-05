# MusiQA Backend - Railway Deployment Setup

This file contains essential steps and checks to deploy the backend to Railway.

## Required Files in `backend/`
- `app.py` (main application) — already present
- `requirements.txt` — already present
- `Procfile` — already present
- `Dockerfile` — optional (present)
- `.env.example` — present (copy this to Railway env)
- `manage_cookies.py` — present
- `cookies_pool/` — optional but recommended
- `PROXY_SETUP_GUIDE.md`, `OPTIMIZATION_SUMMARY.md` — present

## Railway Environment Variables
Set the following in Railway's Environment settings:
- `BOT_TOKEN` — Telegram bot token
- `YOUTUBE_PROXY` — HTTP or SOCKS5 proxy (required for reliability)
- `YOUTUBE_API_KEY` — Optional (for API calls)
- `DATABASE_URL` — if used
- `AUTO_REFRESH_COOKIES` — `1` to enable
- `COOKIE_REFRESH_INTERVAL_HOURS` — e.g., `6`
- `COOKIE_PROXIES` — comma-separated per-cookie proxies
- `PLAYWRIGHT_PROFILE_DIR` — optional path if using profile-based cookie extraction

## Procfile Check
Open the `backend/Procfile` and ensure it points to the correct start command. Example:
```
web: python backend/app.py
```
If the current Procfile uses different paths, update accordingly.

## Dockerfile Check
If you prefer deploying with Docker, ensure `backend/Dockerfile` exposes port 8080 or configured port and the entrypoint runs `app.py`.

## Quick Deploy Steps
1. Push branch to remote (GitHub)
2. Connect the repository to Railway
3. Set Environment variables in the Railway project
4. Choose `backend/Dockerfile` if using Docker or use `Procfile` start command
5. Deploy and monitor logs

## Smoke Test After Deploy
- Confirm logs show `YOUTUBE_PROXY` usage
- Try a sample `/download <youtube-url>` command
- Check for any `ALL 5 STAGES FAILED` errors and verify proxy & cookies

## Troubleshooting
- If `YOUTUBE_PROXY` not set: deployment will likely fail; set it first.
- If Playwright is required but not installed: consider using cookie pool instead.

---

If you want, I can:
- Verify `backend/Procfile` and update it
- Copy additional docs into `backend/` (done)
- Run a quick static dependency check for `requirements.txt`
