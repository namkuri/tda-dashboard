"""[r242] Craig 녹음 다운로드.

Craig 다운로드 API 는 버전에 따라 경로가 다를 수 있어, 여러 후보 URL 을 순서대로
시도하고 zip/오디오를 받으면 추출한다. 모두 실패하면 시도한 URL 을 담아 에러를 던져
사용자가 `_CRAIG_BASES`/`_dl_candidates` 를 조정할 수 있게 한다.

입력은 녹음 ID+키 또는 `https://craig.chat/rec/<ID>?key=<KEY>` 링크.
"""
import os
import re
import zipfile
import tempfile
from typing import List, Tuple, Optional
from urllib.parse import urlparse, parse_qs

import httpx

# 녹음 페이지가 호스팅되는 도메인 후보(서버에서 실제 동작하는 것으로 조정 가능)
_CRAIG_BASES = ["https://craig.chat", "https://craig.horse"]
_ZIP_MAGIC = b"PK\x03\x04"
_AUDIO_MAGIC = [b"OggS", b"fLaC", b"RIFF", b"ID3", b"\xff\xfb"]


def parse_rec(id_or_url: str, key: Optional[str] = None) -> Tuple[str, str]:
    """'rec 링크' 또는 'ID' + key → (id, key)."""
    s = (id_or_url or "").strip()
    if s.startswith("http"):
        u = urlparse(s)
        # /rec/<ID> 또는 /<ID>
        m = re.search(r"/rec/([A-Za-z0-9]+)", u.path) or re.search(r"/([A-Za-z0-9]{6,})$", u.path)
        rid = m.group(1) if m else ""
        q = parse_qs(u.query)
        k = (q.get("key") or [key or ""])[0]
        return rid, k
    return s, (key or "")


def _dl_candidates(base: str, rid: str, key: str) -> List[str]:
    """녹음 멀티트랙 다운로드 후보 URL(서버에서 검증·조정)."""
    return [
        f"{base}/api/recording/{rid}/cook?key={key}&format=flac&container=zip",
        f"{base}/api/recording/{rid}.zip?key={key}&format=flac",
        f"{base}/api/recording/{rid}?key={key}&format=flac&container=zip",
        f"{base}/dl/{rid}?key={key}&format=flac&container=zip",
        f"{base}/rec/{rid}/cook?key={key}&format=flac&container=zip",
    ]


def _looks_binary_archive(content: bytes) -> bool:
    if not content:
        return False
    if content[:4] == _ZIP_MAGIC:
        return True
    head = content[:4]
    return any(content[:len(m)] == m for m in _AUDIO_MAGIC) or head == b"RIFF"


def _extract(content: bytes, out_dir: str) -> int:
    """zip 이면 추출, 단일 오디오면 파일로 저장. 추출된 오디오 추정 개수 반환."""
    os.makedirs(out_dir, exist_ok=True)
    if content[:4] == _ZIP_MAGIC:
        fd, zpath = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with open(zpath, "wb") as f:
            f.write(content)
        try:
            with zipfile.ZipFile(zpath) as z:
                z.extractall(out_dir)
                names = [n for n in z.namelist() if not n.endswith("/")]
        finally:
            try:
                os.remove(zpath)
            except Exception:
                pass
        return len(names)
    # 단일 오디오(믹스)
    with open(os.path.join(out_dir, "mix.audio"), "wb") as f:
        f.write(content)
    return 1


def download_recording(id_or_url: str, key: Optional[str], out_dir: str,
                       timeout: float = 600.0) -> dict:
    """Craig 녹음 다운로드 → out_dir 에 트랙 추출. {tried, ok, count}."""
    rid, k = parse_rec(id_or_url, key)
    if not rid:
        raise ValueError("녹음 ID 를 해석하지 못했습니다(링크/ID 확인).")
    tried: List[str] = []
    last_err = None
    with httpx.Client(follow_redirects=True, timeout=timeout) as cli:
        for base in _CRAIG_BASES:
            for url in _dl_candidates(base, rid, k):
                tried.append(url)
                try:
                    r = cli.get(url)
                    if r.status_code == 200 and _looks_binary_archive(r.content):
                        n = _extract(r.content, out_dir)
                        if n > 0:
                            return {"tried": tried, "ok": True, "count": n, "url": url}
                    else:
                        last_err = f"HTTP {r.status_code} ({(r.headers.get('content-type') or '')[:40]})"
                except Exception as e:
                    last_err = str(e)
    raise RuntimeError(
        "Craig 자동 다운로드 실패 — 엔드포인트를 서버에서 조정하거나 파일을 직접 업로드하세요. "
        f"마지막 오류: {last_err}. 시도 URL: {tried[:3]} …"
    )


def download_url(url: str, out_dir: str, timeout: float = 600.0) -> dict:
    """직접 다운로드 URL(zip/오디오) → 추출."""
    with httpx.Client(follow_redirects=True, timeout=timeout) as cli:
        r = cli.get(url)
        r.raise_for_status()
        n = _extract(r.content, out_dir)
    return {"ok": True, "count": n, "url": url}
