@echo off
rem BGAN forward-link decoder - Windows launcher.
rem
rem Checks Python, installs dependencies if needed, warns if the ETSI Annex C
rem tables are missing, then starts the GUI. Any arguments are passed through,
rem so "start.cmd C:\path\capture.wav" opens with that file selected.
rem
rem Written with goto labels rather than nested parentheses: cmd parses a
rem whole parenthesised block before running it, so an if/else containing
rem redirection or further parens fails to parse on some Windows versions.

setlocal
cd /d "%~dp0"
title BGAN decoder

rem Probe each candidate by running it, not by checking it exists. Windows
rem ships a "python"/"python3" that is really a Microsoft Store app-execution
rem alias stub: it is on PATH and `where` finds it, but it is not Python.
set "PY=py -3"
call :trypy
if %errorlevel%==0 goto :checkdeps
set "PY=python"
call :trypy
if %errorlevel%==0 goto :checkdeps
set "PY=python3"
call :trypy
if %errorlevel%==0 goto :checkdeps

echo.
echo   No working Python 3.9+ was found (tried py -3, python, python3).
echo.
echo   Install it from https://www.python.org/downloads/ and tick
echo   "Add python.exe to PATH" during setup.
echo.
echo   If "python" opens the Microsoft Store instead, turn off the alias:
echo   Settings ^> Apps ^> Advanced app settings ^> App execution aliases.
echo.
pause
exit /b 1

:trypy
%PY% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3,9) else 1)" >nul 2>&1
exit /b %errorlevel%

:checkdeps
%PY% -c "import numpy, scipy, numba, matplotlib" >nul 2>&1
if %errorlevel%==0 goto :checktk
echo   Installing dependencies (first run only)...
%PY% -m pip install --quiet -r requirements.txt
if %errorlevel% neq 0 goto :pipfail
goto :checktk

:pipfail
echo.
echo   Dependency install failed. Try running this by hand:
echo       %PY% -m pip install -r requirements.txt
echo.
pause
exit /b 1

:checktk
%PY% -c "import tkinter" >nul 2>&1
if %errorlevel%==0 goto :checkannex
echo.
echo   tkinter is missing, so the GUI cannot start.
echo   Re-run the Python installer and enable "tcl/tk and IDLE".
echo.
echo   The command-line tools still work without it:
echo       %PY% tools\decode_wav.py capture.wav --survey
echo.
pause
exit /b 1

:checkannex
if exist "ts_1027440201_AnnexC1_v010101p0" goto :run
if exist "work\ts_1027440201_AnnexC1_v010101p0" goto :run
if exist "annex\ts_1027440201_AnnexC1_v010101p0" goto :run
echo.
echo   WARNING: the ETSI Annex C tables were not found.
echo.
echo   They are ETSI copyright so they are not shipped with this repository,
echo   but they are a free download. Decoding will fail without them.
echo.
echo     1. https://www.etsi.org/standards - search for TS 102 744-2-1
echo     2. download ts_1027440201_AnnexC1_v010101p0.zip and ...AnnexC2...zip
echo     3. extract both here, next to this script
echo.
echo   See README.md for detail. Starting anyway.
echo.
pause

:run
echo   Starting BGAN decoder GUI...
%PY% tools\gui.py %*
if %errorlevel% neq 0 goto :crashed
exit /b 0

:crashed
echo.
echo   The GUI exited with an error (code %errorlevel%). Scroll up for details.
echo.
pause
exit /b 1
