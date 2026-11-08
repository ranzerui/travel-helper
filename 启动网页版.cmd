@echo off
rem ---------------------------------------------------------------------------
rem  ASCII ONLY on purpose: cmd.exe reads .cmd files as GBK (cp936).
rem  A UTF-8 saved script with Chinese text gets garbled and breaks parsing,
rem  which is exactly why the page never opened.
rem ---------------------------------------------------------------------------
setlocal
rem UTF-8 console, so Chinese printed by Python shows correctly (not mojibake)
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)


if not defined PY (
    echo [ERROR] Python not found.
    echo Install Python 3.12 or newer and follow README.md.
    pause
    exit /b 2
)

echo Using: %PY%
%PY% -c "import sys; print('Python', sys.version.split()[0])"

:loop
%PY% -m travel_planner.web %*
set "code=%errorlevel%"
rem exit code 3 = code changed, restart automatically
if "%code%"=="3" (
    echo.
    echo [auto-restart] code changed, reloading...
    ping -n 2 127.0.0.1 >nul
    goto loop
)
echo.
echo Server exited with code %code%
pause
exit /b %code%
