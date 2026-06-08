"""[r242] 화자별 멀티트랙 → faster-whisper STT → 시간순 병합.

Craig 멀티트랙: 트랙 파일 1개 = 화자 1명(파일명 = 디스코드 닉네임).
각 트랙을 ffmpeg 로 16k mono wav 변환 후 faster-whisper 로 STT, 타임스탬프로 병합.

서버 사전 준비: `pip install faster-whisper`, `ffmpeg`(PATH).
"""
import os
import re
import glob
import shutil
import subprocess
import tempfile
from typing import List, Dict, Any, Optional

_AUDIO_EXTS = (".flac", ".ogg", ".oga", ".wav", ".mp3", ".m4a", ".aac", ".opus", ".webm")
_model_cache: Dict[str, Any] = {}

# [r288] faster-whisper >=1.0 의 배치 추론 파이프라인 — 한 트랙 안에서 여러 청크를 배치 처리.
#   GPU 활용도 5~10% → 50~90% 로 끌어올림. 미설치 시 자동 폴백.
try:
    from faster_whisper import BatchedInferencePipeline as _BatchedPipeline   # type: ignore
    _HAS_BATCHED = True
except Exception:
    _BatchedPipeline = None
    _HAS_BATCHED = False


# [r284 #4] pip 로 설치한 nvidia-cublas-cu12 / nvidia-cudnn-cu12 의 DLL 을 Python 이 찾도록
#   site-packages/nvidia/.../bin 디렉터리를 DLL 검색 경로에 추가. Windows 는 Python 3.8+ 부터
#   os.add_dll_directory 가 표준. 이걸 안 하면 ctranslate2 가 cublas64_12.dll 을 못 찾아 GPU
#   초기화 실패 → CPU 폴백.
def _register_nvidia_dll_dirs():
    import importlib
    found = []
    for pkg in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime"):
        try:
            mod = importlib.import_module(pkg)
            base = os.path.dirname(getattr(mod, "__file__", "") or "")
            if not base:
                continue
            # Windows: bin/, Linux: lib/
            for sub in ("bin", "lib"):
                d = os.path.join(base, sub)
                if os.path.isdir(d):
                    found.append(d)
        except Exception:
            continue
    if not found:
        return
    if hasattr(os, "add_dll_directory"):
        for d in found:
            try:
                os.add_dll_directory(d)
            except Exception:
                pass
    # PATH 추가(자식 프로세스/일부 라이브러리는 PATH 만 검색)
    cur_path = os.environ.get("PATH", "")
    add = os.pathsep.join(found)
    if add and add not in cur_path:
        os.environ["PATH"] = add + os.pathsep + cur_path
    print(f"[meetings.transcribe] CUDA DLL 디렉터리 등록: {found}")


_register_nvidia_dll_dirs()


def is_available() -> Dict[str, Any]:
    """faster-whisper / ffmpeg / GPU 가용 여부."""
    out = {"faster_whisper": False, "ffmpeg": False, "gpu": False,
           "device_resolved": "cpu", "error": None}
    try:
        import faster_whisper  # noqa: F401
        out["faster_whisper"] = True
    except Exception as e:
        out["error"] = f"faster-whisper 미설치: {e}"
    out["ffmpeg"] = bool(shutil.which("ffmpeg"))
    if not out["ffmpeg"] and not out["error"]:
        out["error"] = "ffmpeg 미설치(PATH)"
    # [r247] GPU(CUDA) 가용성 — 실패 시 CPU 폴백 안내(CPU 도 동작은 함)
    if out["faster_whisper"]:
        try:
            out["gpu"] = _cuda_ok()
            out["device_resolved"] = "cuda" if out["gpu"] else "cpu"
        except Exception:
            out["gpu"] = False
    return out


def _cuda_ok() -> bool:
    """CUDA(cuBLAS/cuDNN)가 실제로 로드 가능한지 사전 확인.

    [r284 #4] 우선순위:
      1) WHISPER_DEVICE=cpu 이면 False
      2) ctranslate2.get_cuda_device_count() 가 0 이면 False (디바이스 없음)
      3) ctranslate2.get_supported_compute_types('cuda') 가 비어있지 않으면 True
         (실제 로드 가능. nvidia-cublas-cu12 가 설치돼 있고 DLL 디렉토리가 등록됐으면 통과)
      4) Windows ctypes.WinDLL fallback — cublas64_12.dll 직접 로드 시도
    """
    if os.environ.get("WHISPER_DEVICE", "").lower() == "cpu":
        return False
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() <= 0:
            return False
        # ctranslate2 가 실제 로드 가능한 compute type 을 알려준다 — 가장 신뢰성 있는 검사
        try:
            ct = ctranslate2.get_supported_compute_types("cuda")
            if ct:
                return True
        except Exception:
            pass
        # Windows ctypes fallback
        import ctypes
        if hasattr(ctypes, "WinDLL"):
            for dll in ("cublas64_12.dll", "cublas64_11.dll"):
                try:
                    ctypes.WinDLL(dll); return True
                except OSError:
                    continue
            return False
        # 비 Windows — 위 device_count 통과만으로 OK
        return True
    except Exception:
        return False


def current_device() -> str:
    """[r286] 다음 transcribe_track 호출에서 사용될 device — _cuda_ok 결과 기반.
    main._meet_run_job 이 session.stt_device 에 저장 → 프론트가 진행 중 배너에 'GPU/CPU' 표시.
    """
    return "cuda" if _cuda_ok() else "cpu"


def _load_model(size: str = "medium", force_cpu: bool = False):
    """모델 로드. GPU 가용성 사전 검증 → 안 되면 즉시 CPU/int8 폴백.

    [r288] num_workers 추가(CPU↔GPU 전송 병렬). GPU 이고 faster-whisper >=1.0 이면
    BatchedInferencePipeline 으로 감싸서 GPU 활용도 ↑(5~10% → 50~90%).
    환경변수: WHISPER_NUM_WORKERS(기본 2), WHISPER_BATCHED=0 으로 끄기 가능.
    """
    cache_key = (size, "cpu" if force_cpu else "auto")
    if cache_key in _model_cache:
        return _model_cache[cache_key]
    from faster_whisper import WhisperModel
    if force_cpu or not _cuda_ok():
        device, compute = "cpu", os.environ.get("WHISPER_COMPUTE_CPU", "int8")
    else:
        device = os.environ.get("WHISPER_DEVICE", "cuda")
        compute = os.environ.get("WHISPER_COMPUTE", "float16")
    nw = max(1, int(os.environ.get("WHISPER_NUM_WORKERS", "2") or "2"))
    try:
        m = WhisperModel(size, device=device, compute_type=compute, num_workers=nw)
    except Exception as e:
        print(f"[meetings.transcribe] {device}/{compute} num_workers={nw} 로드 실패({e}) → CPU/int8 폴백")
        m = WhisperModel(size, device="cpu", compute_type="int8")
        cache_key = (size, "cpu")
        device = "cpu"
    # GPU 인 경우만 배치 파이프라인 사용(CPU 는 효과 적음). VRAM 같은 모델 공유.
    use_batched = _HAS_BATCHED and device != "cpu" and os.environ.get("WHISPER_BATCHED", "1") != "0"
    if use_batched:
        try:
            wrapped = _BatchedPipeline(model=m)
            bs = int(os.environ.get("WHISPER_BATCH_SIZE", "8") or "8")
            print(f"[meetings.transcribe] BatchedInferencePipeline 활성(device={device}, num_workers={nw}, batch_size={bs}) — GPU 활용도 ↑")
            _model_cache[cache_key] = wrapped
            return wrapped
        except Exception as e:
            print(f"[meetings.transcribe] Batched 래핑 실패({e}) → 단일 모델 사용")
    _model_cache[cache_key] = m
    return m


def _is_cuda_runtime_err(e: Exception) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("cublas", "cudnn", "cuda", "cuinit", "cudart"))


def _speaker_from_filename(path: str) -> str:
    """파일명 → 화자 이름. Craig 패턴 여러 형태를 우선 매칭."""
    stem = os.path.splitext(os.path.basename(path))[0]
    # 1) "1-Username_123456789012345678" / "1-Username#0001" (Craig multi-track 표준)
    m = re.match(r"^\d+[-_.]\s*([^_#]+?)(?:[_#]\d{4,}.*)?$", stem)
    if m and not _looks_like_id(m.group(1)):
        return m.group(1).replace("_", " ").strip() or "화자"
    # 2) "<recID>-<discordID>-<Username>" (가끔 보이는 형식)
    parts = stem.split("-")
    if len(parts) >= 3 and not _looks_like_id(parts[-1]):
        return parts[-1].replace("_", " ").strip()
    # 3) 폴백: 기존 정규화
    s = re.sub(r"^\d+[-_.]\s*", "", stem)
    s = re.sub(r"[-_]\d{4,}$", "", s)
    s = s.replace("_", " ").strip()
    # ID처럼 보이면 그대로 두고(나중에 사용자가 이름 변경), 아니면 그대로 화자명
    return s or "화자"


def _looks_like_id(s: str) -> bool:
    """녹음 ID/디스코드 스노우플레이크처럼 보이면 True — 화자명으로 부적합."""
    s = (s or "").strip()
    if not s:
        return True
    if len(s) >= 16 and s.isdigit():   # Discord snowflake (17~19 digits)
        return True
    # 영숫자 12자 이상이고 모음 비율 낮으면 ID 의심(Craig 녹음 ID 등)
    if len(s) >= 12 and re.fullmatch(r"[A-Za-z0-9]+", s):
        vowels = sum(1 for c in s.lower() if c in "aeiou")
        if vowels / len(s) < 0.2:
            return True
    return False


def list_tracks(audio_dir: str) -> List[Dict[str, str]]:
    """디렉터리 내 오디오 트랙 목록 → [{path, speaker}]."""
    files: List[str] = []
    for ext in _AUDIO_EXTS:
        files.extend(glob.glob(os.path.join(audio_dir, "**", "*" + ext), recursive=True))
    files = sorted(set(files))
    return [{"path": f, "speaker": _speaker_from_filename(f)} for f in files]


def _to_wav(src: str) -> str:
    """ffmpeg 로 16k mono wav 변환 → 임시 경로."""
    fd, dst = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    cmd = ["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000", "-vn", dst]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return dst


def _audio_duration_sec(path: str) -> float:
    """[r287] ffprobe 로 오디오 길이(초). 진행률 계산용 — 실패 시 0."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=15,
        )
        return float((r.stdout or b"").decode().strip() or 0)
    except Exception:
        return 0.0


def _run_transcribe(model, wav: str, language: Optional[str], speaker: str,
                    progress_cb=None, total_sec: float = 0.0) -> List[Dict[str, Any]]:
    """[r287] 순회 시점에 실제 GPU 호출이 일어나므로 list() 까지 한 함수 안에서 수행.
    [r288] BatchedInferencePipeline 이면 batch_size 전달 — 청크 묶음 처리로 GPU 활용 ↑.
    progress_cb(processed_sec, total_sec) 가 주어지면 매 segment 마다 호출(throttle 은 호출처 책임).
    """
    kwargs = dict(language=language, vad_filter=True,
                  vad_parameters=dict(min_silence_duration_ms=600))
    if _HAS_BATCHED and isinstance(model, _BatchedPipeline):
        try:
            kwargs["batch_size"] = int(os.environ.get("WHISPER_BATCH_SIZE", "8") or "8")
        except Exception:
            kwargs["batch_size"] = 8
    segments, _info = model.transcribe(wav, **kwargs)
    out = []
    for s in segments:
        end_sec = float(s.end)
        txt = (s.text or "").strip()
        if txt:
            out.append({
                "t": round(float(s.start), 2),
                "dur": round(end_sec - float(s.start), 2),
                "speaker": speaker,
                "text": txt,
            })
        if progress_cb:
            try: progress_cb(end_sec, total_sec)
            except Exception: pass
    return out


def transcribe_track(path: str, speaker: str, model_size: str = "medium",
                     language: Optional[str] = "ko",
                     progress_cb=None) -> List[Dict[str, Any]]:
    """트랙 1개 STT → [{t, dur, speaker, text}] (t=시작초).

    [r287] progress_cb(processed_sec, total_sec) — 매 segment 마다 호출.
    GPU 모델로 시도하다 cuBLAS/cuDNN 미설치(RuntimeError)면 CPU 모델로 1회 자동 재시도.
    """
    wav = None
    try:
        wav = _to_wav(path)
        total_sec = _audio_duration_sec(wav)
        try:
            model = _load_model(model_size)
            return _run_transcribe(model, wav, language, speaker, progress_cb, total_sec)
        except RuntimeError as e:
            if not _is_cuda_runtime_err(e):
                raise
            print(f"[meetings.transcribe] GPU 런타임 오류({e}) → CPU 폴백으로 재시도")
            for k in list(_model_cache.keys()):
                if k[0] == model_size and k[1] != "cpu":
                    _model_cache.pop(k, None)
            cpu_model = _load_model(model_size, force_cpu=True)
            return _run_transcribe(cpu_model, wav, language, speaker, progress_cb, total_sec)
    finally:
        if wav and os.path.exists(wav):
            try:
                os.remove(wav)
            except Exception:
                pass


def merge_segments(all_segs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """여러 트랙 세그먼트를 시작 시각 순으로 병합."""
    segs = sorted(all_segs, key=lambda x: x.get("t", 0))
    return segs


def segments_to_text(segments: List[Dict[str, Any]]) -> str:
    """병합 세그먼트 → '[mm:ss] 화자: 발화' 평문(요약/검색용)."""
    lines = []
    for s in segments:
        t = int(s.get("t", 0))
        mm, ss = t // 60, t % 60
        lines.append(f"[{mm:02d}:{ss:02d}] {s.get('speaker', '?')}: {s.get('text', '')}")
    return "\n".join(lines)
