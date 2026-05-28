@echo off
chcp 65001 >nul
setlocal

echo.
echo ═══════════════════════════════════════════════════
echo  🤖 TDA Deep Wiki 백엔드 시작
echo ═══════════════════════════════════════════════════
echo.

REM 가상환경 활성화
if not exist .venv (
    echo ❌ 가상환경이 없습니다. setup.bat을 먼저 실행하세요.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat

REM .env 체크
if not exist .env (
    echo ❌ .env 파일이 없습니다.
    echo    copy .env.example .env
    echo    notepad .env
    pause
    exit /b 1
)

REM FastAPI 서버 시작 (auto-reload는 개발 시에만 — 운영은 빼는 게 좋음)
echo 🚀 서버 시작 중...
echo    http://localhost:8000/health 에서 상태 확인 가능
echo    Ctrl+C로 종료
echo.
python -m uvicorn main:app --host 0.0.0.0 --port 8000

pause
