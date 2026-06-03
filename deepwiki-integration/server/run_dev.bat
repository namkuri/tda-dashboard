@echo off
REM [r209+] 개발용 — --reload 켜서 main.py / indexer.py 등 변경 시 자동 재시작.
REM 운영은 run.bat 사용.
setlocal

echo.
echo ============================================================
echo  TDA Deep Wiki Backend - DEV (auto-reload)
echo ============================================================
echo.

if not exist .venv\Scripts\python.exe (
    echo [FAIL] Virtual environment not found.
    echo        Run setup.bat first.
    goto :error
)
if not exist .env (
    echo [FAIL] .env file is missing. Run setup.bat or copy .env.example .env.
    goto :error
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [FAIL] Could not activate venv.
    goto :error
)
python -c "import uvicorn" 2>nul
if errorlevel 1 (
    echo [FAIL] uvicorn is not installed. Run setup.bat.
    goto :error
)

echo [OK] venv ready - auto-reload enabled
echo.
echo Server: http://localhost:8000
echo Health: http://localhost:8000/health
echo Code changes auto-reload. Press Ctrl+C to stop.
echo ============================================================
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

echo.
echo Server stopped.
pause
exit /b 0

:error
echo.
echo ============================================================
echo  [ABORT] Cannot start dev server.
echo ============================================================
echo.
pause
exit /b 1
