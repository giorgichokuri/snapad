@echo off
title SnapAd
cd /d "%~dp0"
echo ============================================
echo   SnapAd - setup and start
echo ============================================

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python was not found. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this file again.
  echo PYTHON_NOT_FOUND > setup.log
  pause
  exit /b 1
)

%PY% --version > setup.log 2>&1
type setup.log

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  %PY% -m venv .venv >> setup.log 2>&1
)

echo Installing packages (first time takes a minute)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt >> setup.log 2>&1
if errorlevel 1 (
  echo Package install FAILED. See setup.log
  echo INSTALL_FAILED >> setup.log
  pause
  exit /b 1
)
echo INSTALL_OK >> setup.log

if not exist ".env" copy ".env.example" ".env" >nul

echo.
echo SnapAd is starting at http://localhost:8000
echo Keep this window open. Close it to stop SnapAd.
echo.
start "" cmd /c "timeout /t 4 >nul & start http://localhost:8000"
".venv\Scripts\python.exe" -m uvicorn app.main:app --port 8000
pause
