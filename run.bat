@echo off
setlocal
cd /d "%~dp0"
title Suture

where python >nul 2>nul
if errorlevel 1 goto nopython
python main.py
pause
exit /b 0

:nopython
echo.
echo Python not found. Install Python 3.11+ first:
echo   https://www.python.org/downloads/
echo.
pause
exit /b 1
