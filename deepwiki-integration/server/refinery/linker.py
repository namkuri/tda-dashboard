"""[r223] 옵시디언 [[]] 링크 후처리 + 백링크 자동 생성.

LLM 본문 작성 후 호출:
- 본문 안 평문 파일명 → [[파일명]] 자동 치환 (단어 매칭만)
- 각 파일 끝에 백링크 패널 자동 추가
"""
import re
from typing import Dict, Any, List, Tuple


def link_files(files_with_body: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """files: [{path, body, ...}, ...]
    각 body 의 다른 파일 제목 평문 → [[파일명]] 치환 + 백링크 패널 추가.
    """
    # 파일명 인덱스 (확장자 .md 제거)
    title_to_path: Dict[str, str] = {}
    for f in files_with_body:
        path = f["path"]
        # 파일명만 추출
        fname = path.split("/")[-1].replace(".md", "")
        title_to_path[fname] = path
        # node_title 도 매칭
        if f.get("node_title"):
            title_to_path[f["node_title"]] = path

    # 길이 긴 제목 우선 (overlap 방지)
    sorted_titles = sorted(title_to_path.keys(), key=lambda x: -len(x))

    # 백링크 누적 (file_path → [referring_path])
    backlinks: Dict[str, List[str]] = {p: [] for p in [f["path"] for f in files_with_body]}

    for f in files_with_body:
        body = f.get("body", "")
        self_path = f["path"]
        for title in sorted_titles:
            tpath = title_to_path[title]
            if tpath == self_path:
                continue
            # 이미 [[]] 안에 있으면 skip — 패턴: title 이 [[ 뒤가 아닐 때만
            # 단순 휴리스틱: 같은 라인에 [[ 가 있고 그 안에 title 있으면 skip
            # 본문 안 평문 매칭 — 단어 경계 (\b 한국어는 [^\w] 로)
            try:
                pat = r"(?<!\[\[)" + re.escape(title) + r"(?!\]\])"
                if re.search(pat, body):
                    body = re.sub(pat, f"[[{title}]]", body, count=1)
                    backlinks.setdefault(tpath, []).append(self_path)
            except re.error:
                continue
        f["body"] = body

    # 백링크 패널 자동 추가
    for f in files_with_body:
        refs = backlinks.get(f["path"], [])
        if not refs:
            continue
        unique_refs = sorted(set(refs))
        bl_lines = ["", "---", "", f"## ↩ 백링크 ({len(unique_refs)})", ""]
        for r in unique_refs:
            rname = r.split("/")[-1].replace(".md", "")
            bl_lines.append(f"- [[{rname}]]")
        # body 끝에 추가 (이미 있으면 교체)
        body = f["body"]
        body = re.sub(r"\n---\n\n## ↩ 백링크[\s\S]*$", "", body)
        f["body"] = body.rstrip() + "\n" + "\n".join(bl_lines) + "\n"
    return files_with_body


def extract_user_sections(body: str) -> Tuple[List[Dict[str, Any]], str]:
    """`<!-- USER:start --> ... <!-- USER:end -->` 마커 안 내용 추출.

    반환: ([{marker_id, content}], 마커 제거된 body)
    """
    pattern = r"<!-- USER:start(?::([\w-]+))? -->([\s\S]*?)<!-- USER:end -->"
    found = []
    def _repl(m):
        mid = m.group(1) or f"u{len(found)}"
        found.append({"marker_id": mid, "content": m.group(2)})
        return f"<!-- USER_PLACEHOLDER:{mid} -->"
    stripped = re.sub(pattern, _repl, body)
    return found, stripped


def reinject_user_sections(body: str, user_sections: List[Dict[str, Any]]) -> str:
    """저장된 USER 섹션을 재생성된 body 의 placeholder 자리 또는 끝에 재삽입."""
    for u in user_sections:
        mid = u["marker_id"]
        content = u["content"]
        marker = f"<!-- USER:start:{mid} -->{content}<!-- USER:end -->"
        ph = f"<!-- USER_PLACEHOLDER:{mid} -->"
        if ph in body:
            body = body.replace(ph, marker)
        else:
            # placeholder 없으면 본문 끝에 추가 (보존 보장)
            body = body.rstrip() + f"\n\n{marker}\n"
    return body
