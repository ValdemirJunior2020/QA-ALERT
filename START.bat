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
start "QA ALERT" http://127.0.0.1:8787
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8787
