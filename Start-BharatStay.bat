@echo off
setlocal
set "PROJECT_DIR=C:\Users\pheonix\OneDrive\Desktop\Anudip Project\sayan sir\Final run"
cd /d "%PROJECT_DIR%" || (
  echo BharatStay project folder was not found:
  echo %PROJECT_DIR%
  pause
  exit /b 1
)
 powershell -NoProfile -Command "if (Test-NetConnection -ComputerName 127.0.0.1 -Port 8000 -InformationLevel Quiet) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 goto open_site
start "BharatStay server" /D "%PROJECT_DIR%" cmd /k python app.py
timeout /t 4 /nobreak >nul
:open_site
start "" "http://localhost:8000/BharatStay-Single-File.html"
endlocal
