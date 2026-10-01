@echo off
rem  START.cmd - double-click this to open the Short Order dashboard.
rem
rem  From a prompt it takes the same arguments as launch.py:
rem
rem      START.cmd play my-scheduler --seed 7
rem      START.cmd check my-scheduler
rem      START.cmd doctor
rem
rem  All it does is find Python and hand over, so there is one implementation
rem  of the actual work rather than one per platform.

setlocal enabledelayedexpansion
cd /d "%~dp0"

rem Explorer starts us through a fresh `cmd /c`, so our own name appears in the
rem command line; typing START.cmd at a prompt leaves it out. Requiring no
rem arguments too keeps `cmd /c START.cmd play ...` from stopping for a keypress
rem nobody is there to press.
rem find.exe by full path: a Git or MSYS bin directory on PATH shadows it with
rem the Unix `find`, which rejects these arguments.
echo %cmdcmdline% | "%SystemRoot%\System32\find.exe" /i "%~nx0" >nul 2>nul
if not errorlevel 1 if "%~1"=="" set "DOUBLECLICKED=1"

rem The py launcher first: it is the one that reliably finds a real CPython.
rem A bare `python` may be the Windows Store stub, which prints an advert and
rem exits instead of running anything, so it is tested before it is trusted.
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3"

if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    echo.
    echo   Python 3.8 or newer was not found.
    echo.
    echo   Install it from:
    echo       https://www.python.org/downloads/
    echo.
    echo   Tick "Add python.exe to PATH" in the installer, then
    echo   double-click this file again.
    echo.
    if defined DOUBLECLICKED pause
    exit /b 1
)

%PY% "%~dp0launch.py" %*
set "STATUS=%ERRORLEVEL%"

if defined DOUBLECLICKED (
    echo.
    pause
)
exit /b %STATUS%
