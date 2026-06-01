"""[r115] Phase C — 기획(canon) vs 1차 자동 위키 대조 분석기.

흐름:
  1) deep_wiki_pages (1차 자동 위키) 가져옴
  2) wiki_docs (kind='canon') = 사용자가 작성한 기획·승인 문서
  3) LLM에 매칭 요청 — 기획 항목 vs 구현 항목 대응
  4) match_score (0~1) + findings 배열 (missing/mismatch/extra/severity)
  5) deep_wiki_audits 테이블에 보고서 저장

SSE 이벤트:
  {"event": "stage", "stage": "load_pages"|"load_canons"|"compare"|"save"}
  {"event": "progress", "current": i, "total": N, "canon_title": "..."}
  {"event": "audit_done", "audit_id": "...", "title": "...", "match_score": 0.0~1.0}
  {"event": "done", "audits_created": M}
"""
import json
import re
from typing import AsyncIterator, Dict, Any, List, Optional

from ollama_client import get_ollama
from supabase_store import get_store
from wiki_generator import _safe_format  # [r147] str.format → _safe_format (canon_content 의 brace KeyError 방지)


_AUDIT_PROMPT = """당신은 기획 문서와 실제 코드 위키를 대조하는 감사관(auditor)입니다.

## 사용자의 기획 문서 (canon)
제목: {canon_title}

```markdown
{canon_content}
```

## 자동 생성된 코드 위키 페이지들 (1차 MD, 발췌)
{pages_summaries}

## 작업
위 기획 문서가 실제 코드(1차 위키)에 어떻게 구현됐는지 대조 분석하세요.

### 출력 형식 — 한국어 마크다운

# 📊 {canon_title} — 기획 대조 보고서

## 일치도 요약
한 줄로 전체 일치도 평가. 예: "기획의 80% 정도가 구현됨. 일부 누락·차이 있음."

## 매핑 표
| 기획 항목 | 구현 페이지 | 상태 |
|-----------|-------------|------|
| 전투 시스템 | `combat` | ✅ 구현됨 |
| 콤보 시스템 | (없음) | ❌ 미구현 |
| 인벤토리 | `inventory` | 🔶 부분 구현 |

## 발견된 차이 (Findings)
각 항목을 다음 형식으로:

### ❌ 미구현 (missing) — 기획에는 있지만 코드 위키에 없음
- **항목명**: 기획 인용 + 구현 부재 사유 추정 (severity: high/medium/low)

### 🔶 부분/다른 구현 (mismatch) — 둘 다 있지만 차이
- **항목명**: 기획 vs 구현 비교 (severity)

### ➕ 기획 외 구현 (extra) — 코드엔 있지만 기획 문서엔 없음
- **항목명**: 어떤 페이지에 있는지 (severity 보통 low)

## 결론
짧게 정리. 가장 우선 확인 필요한 부분 1~3개 강조.

## JSON 메타 (마지막에 반드시 이 형식으로 포함)
```json
{
  "match_score": 0.0~1.0 사이 실수,
  "findings": [
    {"type": "missing|mismatch|extra", "title": "...", "severity": "high|medium|low", "note": "..."}
  ],
  "mapped_pages": ["slug1", "slug2"]
}
```
<!-- [r147] _safe_format 사용 — {{ }} 이스케이프 불필요. 단일 brace 그대로 LLM 에 전달됨 -->

## 규칙
- 기획 문서 텍스트와 위키 페이지 본문에 명시된 사실만 사용
- 추측은 "~으로 보임" 으로 표시
- 일반 상식 답변 금지
"""


def _summarize_pages(pages: List[Dict[str, Any]], max_chars: int = 6000) -> str:
    """1차 위키 페이지들을 LLM 프롬프트에 적합한 짧은 형태로 요약."""
    parts = []
    total = 0
    for p in pages:
        if p.get("slug") == "_overview":
            continue
        slug = p.get("slug") or "?"
        title = p.get("title") or slug
        summary = p.get("summary") or ""
        content = (p.get("content") or "")[:600]  # 페이지당 600자 컷
        block = f"### `{slug}` — {title}\n_요약_: {summary}\n\n{content}"
        if total + len(block) > max_chars:
            break
        parts.append(block)
        total += len(block)
    return "\n\n---\n\n".join(parts) if parts else "(자동 위키 페이지 없음)"


def _extract_json_meta(content: str) -> Dict[str, Any]:
    """답변 끝의 ```json ... ``` 블록에서 meta 추출."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(1))
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}


async def audit_wiki(
    project_id: str,
    model: Optional[str] = None,
    canon_ids: Optional[List[str]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """canon 문서별로 1차 위키와 대조하는 보고서 N개 생성.

    canon_ids 가 주어지면 그 문서들만 대조(부분 갱신). 없으면 전체 canon 대조.
    """
    if not project_id:
        yield {"event": "error", "message": "project_id 필수"}
        return

    store = get_store()
    ollama = get_ollama()
    if not await ollama.ping():
        yield {"event": "error", "message": "Ollama 연결 실패"}
        return

    # 1) 1차 위키 페이지들 로드
    yield {"event": "stage", "stage": "load_pages", "message": "자동 위키 페이지 로드 중..."}
    try:
        pages_res = (
            store.client.table("deep_wiki_pages")
            .select("slug,title,summary,content")
            .eq("project_id", project_id)
            .execute()
        )
        pages = pages_res.data or []
    except Exception as e:
        yield {"event": "error", "message": f"deep_wiki_pages 조회 실패: {e}"}
        return
    if not pages:
        yield {"event": "error", "message": "자동 위키 페이지가 없습니다. 먼저 🤖 위키 자동 생성을 실행하세요."}
        return
    pages_summaries = _summarize_pages(pages)
    yield {"event": "info", "message": f"{len(pages)}개 위키 페이지 로드됨"}

    # 2) canon 기획 문서들 로드
    yield {"event": "stage", "stage": "load_canons", "message": "기획 문서(canon) 로드 중..."}
    try:
        canon_res = (
            store.client.table("wiki_docs")
            .select("id,title,content,meta,emoji")
            .eq("project_id", project_id)
            .eq("kind", "canon")
            .execute()
        )
        canons = canon_res.data or []
    except Exception as e:
        yield {"event": "error", "message": f"wiki_docs(canon) 조회 실패: {e}"}
        return
    # 폴더 제외, 빈 내용 제외
    canons = [
        c for c in canons
        if (c.get("content") or "").strip()
        and not (c.get("meta") or {}).get("isFolder")
    ]
    # [r150] 사용자가 대조 대상(기획)을 직접 선택한 경우 그 문서들만 필터
    selected_ids = set(canon_ids) if canon_ids else None
    if selected_ids is not None:
        canons = [c for c in canons if c.get("id") in selected_ids]
    if not canons:
        if selected_ids is not None:
            yield {"event": "error", "message": "선택한 기획 문서가 비어있거나 존재하지 않습니다."}
        else:
            yield {"event": "error", "message": "기획 문서(canon kind)가 없습니다. 프로젝트 위키에서 'Official' 문서를 먼저 작성하세요."}
        return
    yield {"event": "info", "message": f"{len(canons)}개 기획 문서 로드됨" + (" (선택 대상)" if selected_ids is not None else "")}

    # 3) 기존 감사 보고서 삭제 — 선택 대조면 해당 문서 보고서만, 전체면 프로젝트 전부
    try:
        if selected_ids is not None:
            target_audit_ids = [f"dwa:{project_id}:{cid}" for cid in selected_ids]
            store.client.table("deep_wiki_audits").delete().in_("id", target_audit_ids).execute()
        else:
            store.client.table("deep_wiki_audits").delete().eq("project_id", project_id).execute()
    except Exception:
        pass

    # 4) canon 별로 LLM 대조
    yield {"event": "stage", "stage": "compare", "message": f"LLM이 {len(canons)}개 기획 문서를 대조 중..."}
    audits_created = 0
    for idx, c in enumerate(canons):
        title = c.get("title") or "(제목 없음)"
        content = (c.get("content") or "")[:8000]  # 너무 긴 기획은 컷
        yield {
            "event": "progress",
            "current": idx + 1,
            "total": len(canons),
            "canon_title": title,
        }
        # [r147] _safe_format — canon_content 안에 csharp/json 의 { } 가 있어도 KeyError 안 남
        prompt = _safe_format(
            _AUDIT_PROMPT,
            canon_title=title,
            canon_content=content,
            pages_summaries=pages_summaries,
        )
        try:
            chunks: List[str] = []
            async for d in ollama.chat_stream(
                messages=[
                    {"role": "system", "content": "당신은 기획-구현 대조 감사관입니다. 한국어로 답합니다."},
                    {"role": "user", "content": prompt},
                ],
                model=model,
                temperature=0.2,
            ):
                chunks.append(d)
            audit_content = "".join(chunks).strip()
        except Exception as e:
            yield {"event": "warn", "message": f"LLM 실패 ({title}): {type(e).__name__}: {e}"}
            continue

        meta = _extract_json_meta(audit_content)
        match_score = meta.get("match_score")
        if not isinstance(match_score, (int, float)):
            match_score = None
        findings = meta.get("findings") or []
        if not isinstance(findings, list):
            findings = []
        mapped_pages = meta.get("mapped_pages") or []
        if not isinstance(mapped_pages, list):
            mapped_pages = []

        audit_id = f"dwa:{project_id}:{c.get('id')}"
        record = {
            "id": audit_id,
            "project_id": project_id,
            "title": f"📊 {title} — 기획 대조 보고서",
            "summary": (audit_content.split("\n\n", 2)[1][:200] if audit_content else None),
            "content": audit_content,
            "related_pages": mapped_pages,
            "related_canons": [c.get("id")],
            "match_score": float(match_score) if match_score is not None else None,
            "findings": findings,
        }
        try:
            store.client.table("deep_wiki_audits").upsert(record).execute()
            audits_created += 1
            yield {
                "event": "audit_done",
                "audit_id": audit_id,
                "title": record["title"],
                "match_score": record["match_score"],
                "findings_count": len(findings),
            }
        except Exception as e:
            yield {"event": "warn", "message": f"DB 저장 실패 ({title}): {e}"}

    yield {"event": "done", "audits_created": audits_created}
