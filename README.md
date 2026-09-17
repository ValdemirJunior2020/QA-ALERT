# QA ALERT

Local-first HotelPlanner QA monitoring command center.

## Goals

- Monitor Customer Service calls on a schedule (default: every 5 minutes)
- Ignore calls at or under the configured minimum duration (default: 60 seconds)
- Queue multiple calls safely when they arrive together
- Collect call audio, booking details, documentation, agent/call-center metadata
- Transcribe locally
- Delete audio after a successful transcription
- QA locally with Ollama using the QA form, Matrix, and process-update documents in `knowledge/`
- Send Slack alerts only for configured serious findings
- Save call/transcript/booking/QA data locally
- Export saved QA data to Excel from the dashboard
- Use the interactive Pixel Office as the main worker/status UI
- Optionally publish the local dashboard through Cloudflare Tunnel

## Concurrency design

If 5 calls arrive at the same scan, all 5 are registered immediately. Lightweight collection work can run concurrently, while expensive stages are protected by configurable limits. The default design allows up to 5 active pipeline jobs, 1 transcription at a time, and 2 Ollama QA jobs at a time. Calls that are waiting remain persisted in SQLite, so they are not lost if QA ALERT is restarted.

## Stack

- FastAPI local backend
- SQLite in WAL mode (local persistent queue + QA database)
- Playwright adapter for HotelPlanner monitoring
- Faster-Whisper/Whisper-compatible local transcription layer
- Ollama local QA engine
- React/Vite Pixel Office frontend
- openpyxl Excel exports
- Slack Incoming Webhook alerts
- Cloudflare Tunnel for the permanent domain (optional)

## Start

1. Run `INSTALL.bat` once.
2. Put your QA form, Matrix, and update documents inside `knowledge/`.
3. Run `START.bat`.
4. Open `http://localhost:8787`.
5. Configure HotelPlanner monitoring, Ollama, Slack, and worker limits from Settings.

## Cloudflare

The recommended public address is a protected subdomain such as `qa.hotelplannerqa.com` routed through Cloudflare Tunnel to `http://localhost:8787`. Do not expose the HotelPlanner browser profile, Slack webhook, or local secrets through Git.

See `docs/ARCHITECTURE.md` and `docs/CLOUDFLARE.md`.

## Important

The HotelPlanner page adapter is intentionally configuration-driven. Exact selectors and download behavior must match the authenticated HotelPlanner page you use; they are not hard-coded from guesses. Existing `Downloads\\QA-CALLS` collector output is also supported as an ingestion path while the direct page watcher is being finalized.
