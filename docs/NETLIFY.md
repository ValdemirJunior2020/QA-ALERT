# QA ALERT on Netlify

QA ALERT uses a split deployment:

- **Netlify:** hosts the Pixel Office frontend and the `/api/*` proxy function.
- **Your Windows PC:** runs FastAPI, SQLite, Ollama, Whisper, HotelPlanner monitoring, evidence processing and Slack logic.
- **Cloudflare Tunnel:** gives the local FastAPI service a stable HTTPS address without opening a router port.

This keeps the expensive/private QA processing local while still giving you a web dashboard.

## Netlify settings

Connect Netlify to:

`ValdemirJunior2020/QA-ALERT`

The repository already contains `netlify.toml`, so Netlify should automatically use:

- Publish directory: `frontend`
- Functions directory: `netlify/functions`
- No frontend build command required

## Required Netlify environment variable

In Netlify, add:

`QA_ALERT_BACKEND_URL=https://YOUR-CLOUDFLARE-QA-DOMAIN`

Do **not** add `/api` at the end. Example:

`QA_ALERT_BACKEND_URL=https://qa.example.com`

The Pixel Office continues to request `/api/settings`, `/api/dashboard`, `/api/export.xlsx`, etc. The Netlify function forwards those requests to the local backend through Cloudflare.

## Optional Cloudflare Access service token

If the Cloudflare Tunnel hostname is protected by Cloudflare Access, create a service token for the Netlify proxy and add these Netlify environment variables:

`CF_ACCESS_CLIENT_ID=...`

`CF_ACCESS_CLIENT_SECRET=...`

The browser never receives those values. They exist only inside the Netlify function.

## Local side

Your Windows PC must still run QA ALERT:

`START.bat`

Cloudflare Tunnel should forward the chosen hostname to:

`http://127.0.0.1:8787`

If the PC is off or QA ALERT is not running, the Netlify dashboard will remain available but API calls will report that the backend is unreachable.

## Excel downloads

The existing `Download Excel` button continues to use `/api/export.xlsx`. The Netlify proxy preserves binary responses and the download filename, so XLSX exports work through the Netlify site.

## Security

Never commit `.env`, Slack bot tokens, Cloudflare service-token secrets, HotelPlanner sessions, browser profiles, recordings or the local SQLite database to GitHub. The repository `.gitignore` already excludes the main local secret/data paths.
