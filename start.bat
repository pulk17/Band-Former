@echo off
REM Band-Former — double-click to start the server (GPU) and open the app.
cd /d "%~dp0"
title Band-Former launcher

if not exist ".venv\Scripts\python.exe" (
  echo Could not find .venv\Scripts\python.exe
  echo Create the virtualenv and install requirements first.
  pause
  exit /b 1
)

echo Starting Band-Former server...
start "Band-Former server  (close this window to stop)" /min ".venv\Scripts\python.exe" -m uvicorn server.app:app --host 127.0.0.1 --port 8000

echo Waiting for the server to come up...
powershell -NoProfile -Command "for($i=0;$i -lt 40;$i++){try{Invoke-WebRequest 'http://127.0.0.1:8000/api/jobs' -UseBasicParsing -TimeoutSec 2 | Out-Null; exit 0}catch{Start-Sleep 1}}"

start "" "http://127.0.0.1:8000"
exit
