@echo off
setlocal
cd /d "%~dp0"
title QA ALERT

if not exist .venv\Scripts\python.exe (
  echo Run INSTALL.bat first.
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat
if exist .env for /f "usebackq tokens=1,* delims==" %%A in (".env") do if not "%%A"=="" if not "%%A:~0,1"=="#" set "%%A=%%B"

set "CF_CONFIG=%USERPROFILE%\.cloudflared\config.yml"

where cloudflared >nul 2>nul
if %errorlevel%==0 (
  if exist "%CF_CONFIG%" (
    echo [QA ALERT] Starting Cloudflare tunnel qa-alert...
    start "QA ALERT Cloudflare Tunnel" /min cloudflared tunnel --config "%CF_CONFIG%" run qa-alert
  ) else (
    echo [QA ALERT] Cloudflare config not found: %CF_CONFIG%
    echo [QA ALERT] Netlify live data will not work until the tunnel config exists.
  )
) else (
  echo [QA ALERT] cloudflared not found in PATH.
  echo [QA ALERT] Local mode will still work, but Netlify live data will not update.
)

start "QA ALERT" http://127.0.0.1:8787
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8787
