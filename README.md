# QA ALERT

Local-first HotelPlanner QA monitoring command center with a Netlify-hosted Pixel Office frontend.

## Goals

- Monitor Customer Service calls on a schedule (default: every 5 minutes)
- Ignore calls at or under the configured minimum duration (default: 60 seconds)
- Queue multiple calls safely when they arrive together
- Collect call audio, booking details, documentation, agent/call-center metadata
- Transcribe locally
- Delete full audio after successful transcription while retaining only configured evidence clips for flagged cases
- QA locally with Ollama using the QA Form / Rubric, Original Matrix, and **Matrix update emails sent**
- Treat **Matrix update emails sent** as the highest-priority process source when it overwrites the Matrix
- Send Slack alerts only for configured findings
- Save call/transcript/booking/QA data locally
- Export saved QA data to Excel from the dashboard
- Use the interactive Pixel Office as the main worker/status UI
- Host the frontend on Netlify while keeping the QA engine on the local Windows PC

## Concurrency design

If 5 calls arrive at the same scan, all 5 are registered immediately. Lightweight collection work can run concurrently, while expensive stages are protected by configurable limits. The default design allows up to 5 active pipeline jobs, 1 transcription at a time, and 2 Ollama QA jobs at a time. Calls that are waiting remain persisted in SQLite, so they are not lost if QA ALERT is restarted.

## Stack

- FastAPI local backend
- SQLite in WAL mode (local persistent queue + QA database)
- Playwright/configurable HotelPlanner monitoring adapter
- Local Whisper-compatible transcription layer
- Ollama local QA engine
- Pixel Office web frontend
- openpyxl Excel exports
- Slack bot messaging with configurable recipient
- Netlify frontend hosting + API proxy
- Cloudflare Tunnel for the permanent backend domain

## Local start

1. Run `INSTALL.bat` once.
2. Keep the three QA sources of truth on the local machine.
3. Run `START.bat`.
4. Open `http://localhost:8787` for direct local access.
5. Configure monitoring, Ollama, Slack, evidence retention and worker limits from Settings.

## Netlify

The repository is ready to connect directly to Netlify.

- Publish directory: `frontend`
- Functions directory: `netlify/functions`
- Build command: none required

Add this Netlify environment variable:

`QA_ALERT_BACKEND_URL=https://YOUR-CLOUDFLARE-QA-DOMAIN`

Do not include `/api` at the end.

The Netlify frontend continues using `/api/...`. A Netlify function securely proxies those calls to the local QA backend through Cloudflare. Excel downloads are proxied as binary responses as well.

See `docs/NETLIFY.md` for the full setup.

## Cloudflare

Route your protected Cloudflare Tunnel hostname to:

`http://127.0.0.1:8787`

If Cloudflare Access protects that hostname, Netlify can use optional `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` environment variables. Those values remain server-side and never appear in the Pixel Office browser.

## Important

The HotelPlanner page adapter remains configuration-driven. Exact selectors and download behavior must match the authenticated HotelPlanner page you use; they are not hard-coded from guesses. Existing `Downloads\\QA-CALLS` collector output remains the safe ingestion path while the direct watcher is finalized.

Never commit `.env`, Slack tokens, HotelPlanner browser sessions, recordings, evidence audio, SQLite databases, exports or Cloudflare secrets.
