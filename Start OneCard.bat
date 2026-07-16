@echo off
title OneCard Platform Server
echo ============================================
echo   OneCard Platform - starting server...
echo   Open:  http://localhost:8000
echo   Stop:  close this window (or Ctrl+C)
echo ============================================
cd /d "%~dp0"
start "" http://localhost:8000
python app.py
pause
