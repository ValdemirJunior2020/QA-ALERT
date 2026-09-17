@echo off
setlocal
cd /d "%~dp0"
title QA ALERT Installer
where python >nul 2>nul || (echo Python is required.& pause & exit /b 1)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
if not exist data mkdir data
if not exist data\evidence mkdir data\evidence
if not exist data\exports mkdir data\exports
if not exist knowledge mkdir knowledge
if not exist .env if exist .env.example copy .env.example .env >nul
echo.
echo QA ALERT installation complete.
echo Add your Slack bot token to .env only if Slack alerts are enabled.
pause
