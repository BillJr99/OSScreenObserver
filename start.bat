@echo off
REM ═══════════════════════════════════════════════════════════════════════════
REM  start.bat — Windows launcher for OSScreenObserver.
REM
REM  Detects missing dependencies, prompts before installing, then starts the
REM  server in the default mode (inspect — interactive VLM setup runs only
REM  in this mode).
REM
REM  Uses winget when available (Windows 10 1809+ / Windows 11) to install
REM  Python, Tesseract, and Ollama. Falls back to printing the download URL
REM  on older systems.
REM ═══════════════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ===============================================================
echo   OSScreenObserver - Windows launcher
echo ===============================================================

REM ─── Detect winget ──────────────────────────────────────────────────────────

set "HAS_WINGET=0"
where winget >nul 2>&1 && set "HAS_WINGET=1"

REM ─── Python ─────────────────────────────────────────────────────────────────

set "PY_CMD="
where python >nul 2>&1 && set "PY_CMD=python"
if "%PY_CMD%"=="" where py >nul 2>&1 && set "PY_CMD=py -3"

if "%PY_CMD%"=="" (
    echo   [x] Python 3 was not found on PATH.
    if "%HAS_WINGET%"=="1" (
        call :CONFIRM "Install Python 3.12 via winget?"
        if !ANSWER!==Y (
            winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
            echo   Please close and re-open this terminal so PATH picks up Python, then re-run start.bat.
            pause
            exit /b 0
        )
    ) else (
        echo   winget is unavailable. Install Python from https://www.python.org/downloads/
    )
    echo   Aborting - Python 3 is required.
    pause
    exit /b 1
)
for /f "tokens=*" %%V in ('%PY_CMD% --version 2^>^&1') do set "PY_VER=%%V"
echo   [+] Python -^> %PY_VER%

REM ─── Tesseract (OCR) ────────────────────────────────────────────────────────

set "HAS_TESS=0"
where tesseract >nul 2>&1 && set "HAS_TESS=1"
if not exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" (
    if "%HAS_TESS%"=="0" (
        echo   [x] tesseract not found - OCR will be unavailable.
        if "%HAS_WINGET%"=="1" (
            call :CONFIRM "Install Tesseract via winget?"
            if !ANSWER!==Y (
                winget install -e --id UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements
                echo   After install, set ocr.tesseract_cmd in config.json to the full path:
                echo     "C:/Program Files/Tesseract-OCR/tesseract.exe"
            )
        ) else (
            echo   Install from https://github.com/UB-Mannheim/tesseract/wiki
            echo   then set ocr.tesseract_cmd in config.json to the installed path.
        )
    )
) else (
    echo   [+] tesseract present at "%ProgramFiles%\Tesseract-OCR\tesseract.exe"
)

REM ─── Ollama (optional - for local VLM) ──────────────────────────────────────

where ollama >nul 2>&1
if errorlevel 1 (
    echo   [i] ollama not found - required only if you want a local VLM.
    if "%HAS_WINGET%"=="1" (
        call :CONFIRM "Install Ollama via winget?"
        if !ANSWER!==Y (
            winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
        )
    ) else (
        echo   Install from https://ollama.com/download/windows
    )
) else (
    for /f "tokens=*" %%V in ('ollama --version 2^>nul') do set "OLLAMA_VER=%%V"
    echo   [+] ollama -^> !OLLAMA_VER!
)

REM ─── Python virtualenv + pip install ────────────────────────────────────────

set "VENV_DIR=%CD%\.venv"
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    call :CONFIRM "Create a project virtualenv at .venv\?"
    if !ANSWER!==Y (
        %PY_CMD% -m venv "%VENV_DIR%"
    )
)

if exist "%VENV_DIR%\Scripts\activate.bat" (
    call "%VENV_DIR%\Scripts\activate.bat"
    echo   [+] activated virtualenv .venv\
    set "PY_CMD=python"
)

call :CONFIRM "Install/upgrade Python dependencies from requirements.txt?"
if !ANSWER!==Y (
    %PY_CMD% -m pip install --upgrade pip
    %PY_CMD% -m pip install -r requirements.txt
)

REM ─── Launch ─────────────────────────────────────────────────────────────────

echo.
echo   Starting OSScreenObserver (default mode: inspect)...
echo   Web UI -^> http://127.0.0.1:5001
echo.
%PY_CMD% main.py %*
exit /b %ERRORLEVEL%

REM ─── :CONFIRM subroutine — sets ANSWER=Y or ANSWER=N ───────────────────────
:CONFIRM
set "ANSWER=N"
set /p "REPLY=%~1 [Y/n] "
if /i "%REPLY%"==""    set "ANSWER=Y"
if /i "%REPLY%"=="y"   set "ANSWER=Y"
if /i "%REPLY%"=="yes" set "ANSWER=Y"
exit /b 0
