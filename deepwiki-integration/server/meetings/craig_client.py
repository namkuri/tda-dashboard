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


def _consume(cli, base: str, r, out_dir: str) -> int:
    """응답이 zip/오디오면 추출, JSON(다운로드 경로)이면 따라가 추출. 추출 트랙 수(0=실패)."""
    if r.status_code != 200:
        return 0
    ct = (r.headers.get("content-type") or "").lower()
    body = r.content
    if _looks_binary_archive(body):
        return _extract(body, out_dir)
    if "json" in ct:
        try:
            j = r.json()
        except Exception:
            return 0
        data = j.get("data") if isinstance(j.get("data"), dict) else {}
        fpath = j.get("file") or j.get("download") or j.get("url") or data.get("file") or data.get("download")
        if fpath:
            durl = fpath if str(fpath).startswith("http") else (base + "/dl/" + str(fpath).lstrip("/").split("/")[-1])
            try:
                r2 = cli.get(durl)
                if r2.status_code == 200 and _looks_binary_archive(r2.content):
                    return _extract(r2.content, out_dir)
            except Exception:
                return 0
    return 0


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
                       timeout: float = 1200.0) -> dict:
    """Craig 녹음 다운로드 → out_dir 에 멀티트랙 추출. {tried, ok, count}.

    실제 Craig API: POST /api/recording/{id}/cook?key={key}  (JSON {format, container}).
    cook 이 zip 을 스트리밍하거나 다운로드 경로(JSON)를 주면 따라가 추출한다.
    """
    rid, k = parse_rec(id_or_url, key)
    if not rid:
        raise ValueError("녹음 ID 를 해석하지 못했습니다(링크/ID 확인).")
    body = {"format": "flac", "container": "zip", "dynaudnorm": False}
    tried: List[str] = []
    last_err = None
    with httpx.Client(follow_redirects=True, timeout=timeout) as cli:
        for base in _CRAIG_BASES:
            cook = f"{base}/api/recording/{rid}/cook?key={k}"
            # 1) POST cook (정식 경로)
            tried.append("POST " + cook)
            try:
                r = cli.post(cook, json=body)
                n = _consume(cli, base, r, out_dir)
                if n > 0:
                    return {"tried": tried, "ok": True, "count": n, "url": "POST " + cook}
                last_err = f"POST cook HTTP {r.status_code} ({(r.headers.get('content-type') or '')[:40]})"
            except Exception as e:
                last_err = str(e)
            # 2) GET cook (구버전/스트리밍형 대비)
            gurl = cook + "&format=flac&container=zip"
            tried.append("GET " + gurl)
            try:
                r = cli.get(gurl)
                n = _consume(cli, base, r, out_dir)
                if n > 0:
                    return {"tried": tried, "ok": True, "count": n, "url": "GET " + gurl}
                last_err = f"GET cook HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)
    raise RuntimeError(
        "Craig 자동 다운로드 실패 — 녹음 ID/key 가 맞는지, 녹음이 만료(자동삭제)되지 않았는지 확인하세요. "
        "안 되면 Craig 페이지에서 multi-track zip 을 받아 '파일 업로드'로 올리세요. "
        f"마지막 오류: {last_err}. 시도: {tried}"
    )


def download_url(url: str, out_dir: str, timeout: float = 600.0) -> dict:
    """직접 다운로드 URL(zip/오디오) → 추출."""
    with httpx.Client(follow_redirects=True, timeout=timeout) as cli:
        r = cli.get(url)
        r.raise_for_status()
        n = _extract(r.content, out_dir)
    return {"ok": True, "count": n, "url": url}
