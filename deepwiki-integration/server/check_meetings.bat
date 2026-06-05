@echo off
REM [r243] Meeting-minutes deps check - ffmpeg / faster-whisper / GPU(CUDA). ASCII-only.
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo.
echo ============================================================
echo  Meeting minutes (STT) - dependency check
echo ============================================================
echo.

if not exist .venv\Scripts\python.exe (
    echo [FAIL] .venv not found. Run setup.bat first.
    goto :end
)
call .venv\Scripts\activate.bat >nul 2>nul

echo [1/3] ffmpeg (on PATH)
where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo        [MISSING] ffmpeg not on PATH.
    echo                  Install: winget install Gyan.FFmpeg   then open a NEW shell.
) else (
    echo        [OK] ffmpeg found
)
echo.

echo [2/3] faster-whisper (inside venv)
python -c "import faster_whisper, ctranslate2; print('       [OK] faster-whisper installed / ctranslate2', ctranslate2.__version__)" 2>nul
if errorlevel 1 (
    echo        [MISSING] not installed in venv.
    echo                  Run:  .venv\Scripts\activate   then  pip install faster-whisper
)
echo.

echo [3/3] GPU (CUDA) availability
python -c "import ctranslate2; n=ctranslate2.get_cuda_device_count(); print('       [OK] CUDA devices =', n, '(GPU)' if n else '-> CPU fallback (slow)')" 2>nul
if errorlevel 1 (
    echo        [INFO] CUDA not available (faster-whisper missing, or no CUDA). Runs on CPU.
    echo               For GPU:  pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
)
echo.

echo ============================================================
echo  If [1] and [2] are [OK], restart backend - the meeting
echo  page banner will disappear. [3] CPU fallback still works.
echo ============================================================

:end
echo.
pause
