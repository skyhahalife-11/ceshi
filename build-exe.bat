@echo off
setlocal
cd /d "%~dp0"
title Suture - build exe

where python >nul 2>nul
if errorlevel 1 goto nopython

echo.
echo [1/3] running tests ...
python -m unittest discover -s tests -t .
if errorlevel 1 goto testfail

echo.
echo [2/3] installing build tools ...
python -m pip install --user --quiet --upgrade pyinstaller
python -m pip install --user --quiet pywebview

echo.
echo [3/3] building, this takes a minute ...
python -m PyInstaller --clean --noconfirm build/suture.spec
if errorlevel 1 goto buildfail

echo.
echo ============================================
echo   DONE:  %cd%\dist\suture.exe
echo   Double-click that file to run it.
echo ============================================
echo.
pause
exit /b 0

:nopython
echo.
echo Python not found. Install Python 3.11+ first:
echo   https://www.python.org/downloads/
echo Remember to tick "Add Python to PATH" during install.
echo.
pause
exit /b 1

:testfail
echo.
echo Tests failed - not building. Please send the output above.
echo.
pause
exit /b 1

:buildfail
echo.
echo Build failed. Please send the output above.
echo.
pause
exit /b 1
