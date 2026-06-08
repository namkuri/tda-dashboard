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

    ctranslate2.get_cuda_device_count() 는 드라이버만 보면 통과해도, 막상 transcribe
    호출 시 cublas64_12.dll 이 없어 RuntimeError 가 날 수 있다 → 환경변수로 강제 가능.
    """
    if os.environ.get("WHISPER_DEVICE", "").lower() == "cpu":
        return False
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() <= 0:
            return False
        # cublas 라이브러리 실제 로드 시도 (Windows 환경 가드)
        import ctypes
        for dll in ("cublas64_12.dll", "cublas64_11.dll"):
            try:
                ctypes.WinDLL(dll); return True  # 하나라도 로드되면 OK
            except OSError:
                continue
        # WinDLL 자체가 안 되는 환경(=리눅스 등) 은 일단 GPU 시도 허용
        if not hasattr(ctypes, "WinDLL"):
            return True
        return False
    except Exception:
        return False


def _load_model(size: str = "medium", force_cpu: bool = False):
    """모델 로드. GPU 가용성 사전 검증 → 안 되면 즉시 CPU/int8 폴백."""
    cache_key = (size, "cpu" if force_cpu else "auto")
    if cache_key in _model_cache:
        return _model_cache[cache_key]
    from faster_whisper import WhisperModel
    if force_cpu or not _cuda_ok():
        device, compute = "cpu", os.environ.get("WHISPER_COMPUTE_CPU", "int8")
    else:
        device = os.environ.get("WHISPER_DEVICE", "cuda")
        compute = os.environ.get("WHISPER_COMPUTE", "float16")
    try:
        m = WhisperModel(size, device=device, compute_type=compute)
    except Exception as e:
        print(f"[meetings.transcribe] {device}/{compute} 로드 실패({e}) → CPU/int8 폴백")
        m = WhisperModel(size, device="cpu", compute_type="int8")
        cache_key = (size, "cpu")
    _model_cache[cache_key] = m
    return m


def _is_cuda_runtime_err(e: Exception) -> bool:
    s = str(e).lower()
    return any(k in s for k in ("cublas", "cudnn", "cuda", "cuinit", "cudart"))


def _speaker_from_filename(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    # Craig: "1-Nickname" / "1_Nickname" / "Nickname_123456" 형태 정리
    stem = re.sub(r"^\d+[-_.]\s*", "", stem)
    stem = re.sub(r"[-_]\d{4,}$", "", stem)
    stem = stem.replace("_", " ").strip()
    return stem or "화자"


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


def _run_transcribe(model, wav: str, language: Optional[str], speaker: str) -> List[Dict[str, Any]]:
    """순회 시점에 실제 GPU 호출이 일어나므로 list() 까지 한 함수 안에서 수행."""
    segments, _info = model.transcribe(
        wav, language=language, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=600),
    )
    out = []
    for s in segments:
        txt = (s.text or "").strip()
        if not txt:
            continue
        out.append({
            "t": round(float(s.start), 2),
            "dur": round(float(s.end) - float(s.start), 2),
            "speaker": speaker,
            "text": txt,
        })
    return out


def transcribe_track(path: str, speaker: str, model_size: str = "medium",
                     language: Optional[str] = "ko") -> List[Dict[str, Any]]:
    """트랙 1개 STT → [{t, dur, speaker, text}] (t=시작초).

    GPU 모델로 시도하다 cuBLAS/cuDNN 미설치(RuntimeError)면 CPU 모델로 1회 자동 재시도.
    """
    wav = None
    try:
        wav = _to_wav(path)
        try:
            model = _load_model(model_size)
            return _run_transcribe(model, wav, language, speaker)
        except RuntimeError as e:
            if not _is_cuda_runtime_err(e):
                raise
            print(f"[meetings.transcribe] GPU 런타임 오류({e}) → CPU 폴백으로 재시도")
            # 캐시된 GPU 모델 폐기, CPU 강제 로드 후 재시도
            for k in list(_model_cache.keys()):
                if k[0] == model_size and k[1] != "cpu":
                    _model_cache.pop(k, None)
            cpu_model = _load_model(model_size, force_cpu=True)
            return _run_transcribe(cpu_model, wav, language, speaker)
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
