@echo off
REM ASCII-only output
setlocal

echo.
echo ============================================================
echo  TDA Deep Wiki Backend - RUN
echo ============================================================
echo.

REM ---------- Check venv exists ----------
if not exist .venv\Scripts\python.exe (
    echo [FAIL] Virtual environment not found ^(.venv\Scripts\python.exe missing^).
    echo        Run setup.bat first.
    goto :error
)

REM ---------- Check .env exists ----------
if not exist .env (
    echo [FAIL] .env file is missing.
    echo        1. copy .env.example .env
    echo        2. notepad .env
    echo        3. Fill in SUPABASE_URL and SUPABASE_SERVICE_KEY
    goto :error
)

REM ---------- Activate venv ----------
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [FAIL] Could not activate venv.
    goto :error
)

REM ---------- Verify uvicorn ----------
python -c "import uvicorn" 2>nul
if errorlevel 1 (
    echo [FAIL] uvicorn is not installed in the venv.
    echo        The setup.bat did not complete successfully.
    echo        Run: setup.bat   ^(it will re-create the venv^)
    goto :error
)

REM ---------- [r243] Meeting minutes STT deps (non-fatal) ----------
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [WARN] ffmpeg NOT on PATH - meeting transcription disabled. Install: winget install Gyan.FFmpeg  then use a NEW shell.
) else (
    echo [OK] ffmpeg found
)
python -c "import faster_whisper" 2>nul
if errorlevel 1 (
    echo [WARN] faster-whisper NOT installed in venv - run: pip install faster-whisper   ^(run check_meetings.bat for details^)
) else (
    echo [OK] faster-whisper installed
)

REM ---------- Start server ----------
echo [OK] venv and uvicorn ready
echo.
echo Server starting on http://localhost:8000
echo Visit http://localhost:8000/health to verify
echo Press Ctrl+C to stop
echo.
echo ============================================================
echo.
python -m uvicorn main:app --host 0.0.0.0 --port 8000

REM On exit
echo.
echo Server stopped.
pause
exit /b 0

:error
echo.
echo ============================================================
echo  [ABORT] Cannot start server. Fix the issue above.
echo ============================================================
echo.
pause
exit /b 1
