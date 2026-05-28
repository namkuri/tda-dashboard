@echo off
REM ASCII-only output to avoid CP949/UTF-8 mojibake on Korean Windows
setlocal EnableDelayedExpansion

echo.
echo ============================================================
echo  TDA Deep Wiki Backend - SETUP
echo ============================================================
echo.

REM ---------- Step 1: Check Python ----------
where python >nul 2>&1
if errorlevel 1 (
    echo [FAIL] Python is not installed or not in PATH.
    echo        Install Python 3.11 or 3.12 from https://www.python.org/downloads/
    echo        IMPORTANT: Check "Add Python to PATH" during installation.
    goto :error
)

echo [..] Checking Python version...
python --version
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) and sys.version_info < (3,14) else 1)"
if errorlevel 1 (
    echo [FAIL] Python 3.11 or 3.12 is required ^(3.13 may have dependency issues^).
    echo        Current version above. Install 3.11 or 3.12 from python.org.
    goto :error
)
echo [OK] Python version OK
echo.

REM ---------- Step 2: Create venv ----------
if exist .venv (
    echo [..] Removing existing .venv folder ^(clean install^)...
    rmdir /s /q .venv
)
echo [..] Creating virtual environment ^(.venv^)...
python -m venv .venv
if errorlevel 1 (
    echo [FAIL] Failed to create virtual environment.
    goto :error
)
echo [OK] .venv created
echo.

REM ---------- Step 3: Activate venv ----------
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [FAIL] Could not activate virtual environment.
    goto :error
)
echo [OK] venv activated
echo.

REM ---------- Step 4: Upgrade pip ----------
echo [..] Upgrading pip...
python -m pip install --upgrade pip --quiet
if errorlevel 1 (
    echo [FAIL] pip upgrade failed.
    goto :error
)
echo [OK] pip upgraded
echo.

REM ---------- Step 5: Install dependencies ----------
echo [..] Installing dependencies ^(2-5 minutes^)...
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [FAIL] Dependency installation failed.
    echo        Common causes:
    echo          - Network issue: try again
    echo          - Python 3.13 incompatibility: install 3.11 or 3.12
    echo          - Missing C++ build tools: install Visual Studio Build Tools
    goto :error
)
echo [OK] Dependencies installed
echo.

REM ---------- Step 6: Verify uvicorn ----------
echo [..] Verifying uvicorn installation...
python -c "import uvicorn; print('uvicorn', uvicorn.__version__)"
if errorlevel 1 (
    echo [FAIL] uvicorn is not importable. Setup did NOT complete correctly.
    goto :error
)
echo [OK] uvicorn is importable
echo.

REM ---------- Step 7: Verify supabase ----------
echo [..] Verifying supabase client...
python -c "from supabase import create_client; print('supabase OK')"
if errorlevel 1 (
    echo [FAIL] supabase client not importable.
    goto :error
)
echo [OK] supabase client OK
echo.

REM ---------- Step 8: Create repos folder ----------
if not exist repos mkdir repos
echo [OK] repos\ folder ready
echo.

REM ---------- Step 9: Check .env ----------
if not exist .env (
    echo [WARN] .env file is missing.
    echo        1. copy .env.example .env
    echo        2. notepad .env
    echo        3. Fill in SUPABASE_URL and SUPABASE_SERVICE_KEY
    echo.
)

REM ---------- Step 10: Check Ollama ----------
echo [..] Checking Ollama at localhost:11434...
curl -s -o nul -w "%%{http_code}" http://localhost:11434/api/tags > _ollama_check.tmp 2>nul
set /p OLLAMA_STATUS=<_ollama_check.tmp
del _ollama_check.tmp >nul 2>&1
if "%OLLAMA_STATUS%"=="200" (
    echo [OK] Ollama is running
) else (
    echo [WARN] Ollama is not responding ^(status: %OLLAMA_STATUS%^).
    echo        Install: https://ollama.com/download
    echo        After install, Ollama runs automatically in system tray.
    echo        Also run: ollama pull qwen2.5-coder:14b
    echo                  ollama pull nomic-embed-text
)
echo.

echo ============================================================
echo  [DONE] Setup completed successfully
echo ============================================================
echo.
echo Next steps:
echo   1. If .env is missing: copy .env.example .env  and edit it
echo   2. If Ollama warn: install Ollama + pull models
echo   3. Run: run.bat
echo.
pause
exit /b 0

:error
echo.
echo ============================================================
echo  [ABORT] Setup failed. Fix the issue above and rerun.
echo ============================================================
echo.
pause
exit /b 1
