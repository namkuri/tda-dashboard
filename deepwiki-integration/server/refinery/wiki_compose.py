"""[r247] 위키 본문 작성 — 채택된 위키 '구조(목차)'를 따라 문서 본문을 후속 생성.

들여오기 추천(intake)에서 만든 위키 구조 = [{title, summary, outline:[...]}] 를
유저가 채택한 뒤, 이 모듈이 각 문서의 목차를 따라 마크다운 본문을 작성한다.
LLM 1회/문서. 결과는 apply_tree 가 그대로 쓰는 files[] 형태로 반환.
"""
import re
import time
from typing import AsyncIterator, Dict, Any, List, Optional

from llm_router import get_llm


_SYSTEM = """당신은 게임개발 프로젝트 위키 문서를 작성하는 도구입니다.
주어진 '문서 제목 + 목차 + 참고 발췌'를 바탕으로, 목차 순서를 그대로 따르는
마크다운 문서 본문을 한국어로 작성합니다.

[규칙]
1. 첫 줄은 frontmatter(--- ... ---) — title, category: canon, created_by, updated_by 포함.
2. 본문은 주어진 목차의 각 항목을 `## 항목` 헤더로 만들고 내용을 채움.
3. 참고 발췌에 근거해 작성하되, 없는 사실을 지어내지 말 것(불명확하면 'TBD').
4. **옵시디언 양방향 링크**: 본문에서 다른 위키 문서·원본 자료를 언급할 땐 `[[문서명]]`
   형태로 연결하라([연결 후보] 목록의 제목을 정확히 사용). 지식 네트워크를 만드는 게 목적.
5. 출력은 frontmatter+본문 한 덩어리. 코드펜스(```)로 감싸지 말 것.
"""


def _slug(s: str) -> str:
    s = re.sub(r"[\\/:\*\?\"<>\|]", "", s or "").strip()
    s = re.sub(r"\s+", "_", s)
    return s[:60] or "untitled"


def _vault_excerpt(vault_docs: List[Dict[str, Any]], limit_chars: int = 1800) -> str:
    """참고용 vault 발췌(전체에서 균등 트림)."""
    parts = []
    docs = vault_docs or []
    if not docs:
        return "(없음)"
    per = max(200, limit_chars // max(1, len(docs)))
    for d in docs[:6]:
        body = (d.get("content") or "").strip().replace("\n", " ")
        if body:
            parts.append(f"- [{d.get('title') or '문서'}]: {body[:per]}")
    return "\n".join(parts) or "(없음)"


async def compose_wiki_body(
    *,
    docs: List[Dict[str, Any]],          # [{id, title, summary, outline:[...], node_ids?}]
    nodes: Optional[List[Dict[str, Any]]] = None,
    vault_docs: Optional[List[Dict[str, Any]]] = None,
    session: Dict[str, Any],
    user_id: str,
    model: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """채택된 위키 구조 → 문서별 본문 작성.

    SSE 이벤트: stage / doc_done / done(files 포함)
    """
    llm = get_llm(model)
    root = f"🔬 {session.get('title') or '정련'} 위키"
    vault_block = _vault_excerpt(vault_docs or [])
    files: List[Dict[str, Any]] = []
    total = len(docs or [])
    # [r265] 옵시디언 링크 후보 — 다른 위키 문서 제목 + 원본 vault 제목
    all_doc_titles = [(d.get("title") or "").strip() for d in (docs or []) if d.get("title")]
    vault_titles = [(v.get("title") or "").strip() for v in (vault_docs or []) if v.get("title")]
    yield {"event": "stage", "message": f"위키 본문 작성 시작 — 문서 {total}개"}

    for idx, doc in enumerate(docs or []):
        title = (doc.get("title") or "문서").strip()
        outline = [str(x) for x in (doc.get("outline") or []) if str(x).strip()]
        outline_block = "\n".join(f"- {o}" for o in outline) if outline else "- 개요\n- 상세"
        # 이 문서와 같은 상위 폴더의 형제 위키(우선) + 나머지 + vault
        same_folder, other = [], []
        my_top = (doc.get("folder_path") or [None])[0]
        for d in (docs or []):
            t = (d.get("title") or "").strip()
            if not t or t == title:
                continue
            (same_folder if (d.get("folder_path") or [None])[0] == my_top else other).append(t)
        link_cands = list(dict.fromkeys(same_folder + other + vault_titles))[:24]
        link_block = ", ".join(f"[[{t}]]" for t in link_cands) if link_cands else "(없음)"
        yield {"event": "stage", "message": f"[{idx + 1}/{total}] {title} 작성 중…"}
        user_prompt = (
            f"문서 제목: {title}\n"
            f"요약: {doc.get('summary') or ''}\n"
            f"작성자: {user_id}\n\n"
            f"[목차]\n{outline_block}\n\n"
            f"[참고 발췌]\n{vault_block}\n\n"
            f"[연결 후보 — 관련된 것을 본문에 [[제목]] 으로 링크]\n{link_block}\n\n"
            "위 목차 순서대로 위키 문서 본문을 작성하고, 관련 문서는 [[제목]] 으로 연결하세요."
        )
        buf = ""
        try:
            async for delta in llm.chat_stream(
                messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user_prompt}],
                model=model, temperature=0.5,
            ):
                buf += delta
        except Exception as e:
            buf = (
                f"---\ntitle: \"{title}\"\ncategory: canon\n"
                f"created_by: \"{user_id}\"\nupdated_by: \"{user_id}\"\n---\n\n❌ LLM 실패: {e}\n"
            )
        # 코드펜스 제거
        body = re.sub(r"^```(?:markdown|md)?\s*\n?", "", buf.strip())
        body = re.sub(r"\n?```\s*$", "", body).strip()
        # [r265] 옵시디언 링크 보장 — LLM 이 [[]] 를 거의 안 넣었으면 '관련 문서' 푸터 추가
        if body.count("[[") < 2:
            rel = (same_folder[:5] or other[:5])
            srcs = vault_titles[:4]
            foot_parts = []
            if rel:
                foot_parts.append("- 관련 문서: " + " ".join(f"[[{t}]]" for t in rel))
            if srcs:
                foot_parts.append("- 원본 자료: " + " ".join(f"[[{t}]]" for t in srcs))
            if foot_parts:
                body = body + "\n\n## 관련 문서\n" + "\n".join(foot_parts) + "\n"
        # [r258] 폴더 계층(folder_path) 반영 — 깊은 위키 폴더 구조로 생성
        folders = [_slug(str(x)) for x in (doc.get("folder_path") or []) if str(x).strip()]
        path = "/".join([root] + folders + [_slug(title)]) + ".md"
        f = {
            "path": path,
            "body": body,
            "target_kind": "canon",
            "category": "canon",
            "node_ids": doc.get("node_ids") or doc.get("origin_node_ids") or [],
        }
        files.append(f)
        yield {"event": "doc_done", "index": idx + 1, "total": total, "title": title, "path": f["path"]}

    yield {"event": "done", "files": files, "summary": f"문서 {len(files)}개 본문 작성 완료"}


_uid_counter = [0]


def _uid(prefix: str) -> str:
    _uid_counter[0] += 1
    return f"{prefix}_{int(time.time() * 1000)}_{_uid_counter[0]}"
