@echo off
REM ============================================================
REM  Build the GUI into a single-file exe (no console window).
REM  Output: dist\VRChatLiveTranslate.exe
REM  Add --no-verify to skip the self-check that runs after build.
REM
REM  NOTE 1: keep this file ASCII-only.  .bat files containing
REM          non-ASCII text get mis-decoded on a GBK console
REM          (cmd merges lines, then runs the leftovers as commands).
REM  NOTE 2: keep CRLF line endings - same reason.  See .gitattributes.
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [X] .venv\Scripts\python.exe not found.
    echo     Run setup.bat first to create the venv and install dependencies.
    pause
    exit /b 1
)

if not exist "scripts\build_exe.py" (
    echo [X] scripts\build_exe.py not found - put this script in the repo root.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" "scripts\build_exe.py" %*
set RC=%ERRORLEVEL%

echo.
if %RC%==0 (
    echo [OK] Build finished - output is in dist\. Send the whole folder.
) else (
    echo [X] Build or self-check failed, exit code %RC%
)
pause
exit /b %RC%
