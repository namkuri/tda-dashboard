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
    """faster-whisper / ffmpeg 가용 여부."""
    out = {"faster_whisper": False, "ffmpeg": False, "error": None}
    try:
        import faster_whisper  # noqa: F401
        out["faster_whisper"] = True
    except Exception as e:
        out["error"] = f"faster-whisper 미설치: {e}"
    out["ffmpeg"] = bool(shutil.which("ffmpeg"))
    if not out["ffmpeg"] and not out["error"]:
        out["error"] = "ffmpeg 미설치(PATH)"
    return out


def _load_model(size: str = "medium"):
    if size in _model_cache:
        return _model_cache[size]
    from faster_whisper import WhisperModel
    # GPU 우선, 실패 시 CPU. compute_type 은 환경에 맞게 자동.
    device = os.environ.get("WHISPER_DEVICE", "auto")
    compute = os.environ.get("WHISPER_COMPUTE", "auto")
    try:
        m = WhisperModel(size, device=device, compute_type=compute)
    except Exception:
        m = WhisperModel(size, device="cpu", compute_type="int8")
    _model_cache[size] = m
    return m


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


def transcribe_track(path: str, speaker: str, model_size: str = "medium",
                     language: Optional[str] = "ko") -> List[Dict[str, Any]]:
    """트랙 1개 STT → [{t, dur, speaker, text}] (t=시작초)."""
    model = _load_model(model_size)
    wav = None
    try:
        wav = _to_wav(path)
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
