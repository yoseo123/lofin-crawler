@echo off
setlocal

set "EXIT_CODE=0"
pushd "%~dp0" || (
  echo [ERROR] Failed to enter project directory:
  echo   %~dp0
  pause
  exit /b 1
)
set "LOFIN_WEB_PORT=8787"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found.
  echo Run first-time setup in PowerShell:
  echo   python -m venv .venv
  echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
  echo   .\.venv\Scripts\python.exe -m playwright install chromium
  set "EXIT_CODE=1"
  goto done
)

echo [INFO] Checking dependencies...
".venv\Scripts\python.exe" -c "import flask, playwright" >nul 2>&1
if errorlevel 1 (
  echo [INFO] Installing requirements...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [ERROR] pip install failed.
    set "EXIT_CODE=1"
    goto done
  )
)

echo [INFO] Ensuring Chromium for Playwright...
".venv\Scripts\python.exe" -m playwright install chromium >nul 2>&1

echo [INFO] Starting browser in 3 seconds...
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://127.0.0.1:%LOFIN_WEB_PORT%"

echo [INFO] Starting web server...
".venv\Scripts\python.exe" webapp.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo [INFO] webapp.py stopped.
:done
popd
pause
exit /b %EXIT_CODE%
