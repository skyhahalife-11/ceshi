@echo off
setlocal
cd /d "%~dp0"
title Suture - check only

where python >nul 2>nul
if errorlevel 1 goto nopython
echo Read-only check. Nothing on this machine will be modified.
echo.
python main.py --cli --no-fix
echo.
pause
exit /b 0

:nopython
echo.
echo Python not found. Install Python 3.11+ first:
echo   https://www.python.org/downloads/
echo.
pause
exit /b 1
