@echo off
chcp 65001 >nul
setlocal

echo.
echo ═══════════════════════════════════════════════════
echo  🤖 TDA Deep Wiki 백엔드 설치
echo ═══════════════════════════════════════════════════
echo.

REM Python 버전 체크
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python이 설치되지 않았습니다.
    echo         https://www.python.org/downloads/ 에서 Python 3.11+ 설치 ^(Add to PATH 체크^)
    pause
    exit /b 1
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.11 이상이 필요합니다.
    python --version
    pause
    exit /b 1
)

echo [OK] Python 버전 확인
python --version
echo.

REM 가상환경 생성
if not exist .venv (
    echo [..] 가상환경 생성 중...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] venv 생성 실패
        pause
        exit /b 1
    )
)

REM 가상환경 활성화 + pip 업그레이드
call .venv\Scripts\activate.bat
echo [..] pip 업그레이드 중...
python -m pip install --upgrade pip --quiet

REM 의존성 설치
echo [..] 의존성 설치 중... ^(2~3분 소요^)
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] 의존성 설치 실패
    pause
    exit /b 1
)

REM .env 확인
if not exist .env (
    echo.
    echo [WARNING] .env 파일이 없습니다.
    echo           .env.example을 복사해서 .env로 만들고 키를 입력하세요:
    echo             copy .env.example .env
    echo             notepad .env
    echo.
)

REM Ollama 체크
echo.
echo [..] Ollama 연결 확인...
curl -s http://localhost:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo [WARNING] Ollama가 실행 중이지 않습니다.
    echo           https://ollama.com/download 설치 후 시스템 트레이에서 실행
) else (
    echo [OK] Ollama 연결 ^(localhost:11434^)
)

REM repos 폴더 생성
if not exist repos mkdir repos

echo.
echo ═══════════════════════════════════════════════════
echo  [DONE] 설치 완료
echo ═══════════════════════════════════════════════════
echo.
echo 다음 단계:
echo   1. .env 파일이 없으면 .env.example 복사 후 키 입력
echo   2. Ollama 모델 pull: ollama pull qwen2.5-coder:14b
echo                        ollama pull nomic-embed-text
echo   3. run.bat 실행
echo.
pause
