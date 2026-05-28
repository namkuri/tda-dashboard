"""Retriever — 질문 임베딩 + pgvector top-k 검색 + 영문 식별자 LIKE 보강."""
import re
from typing import List, Optional, Dict, Any
from ollama_client import get_ollama
from supabase_store import get_store
from config import settings


# [r93→r94] 유사도 임계치 — nomic-embed-text는 cosine sim이 보통 0.2~0.7 범위.
SIMILARITY_THRESHOLD = 0.10

# [r96→r100] 같은 source_id에서 가져올 최대 청크 수.
# r96에 2로 시작했으나 public/index.html(1777청크) 같은 큰 파일에서 2개만 뽑히면
# 핵심 부분(dbUpsertCategory 등)이 결과에 못 들어가는 문제. r100에 4로 완화.
# 한 파일이 결과를 점유해도 8/10 = 40%까지만 허용 → 여전히 다른 파일 6개 들어감.
MAX_CHUNKS_PER_SOURCE = 4

# [r97] 하이브리드 검색 — 쿼리에서 영문 식별자 패턴 추출용 정규식
# camelCase(dbUpsertCategory), snake_case(kanban_categories), PascalCase(MyClass),
# CONSTANT_CASE(SIMILARITY_THRESHOLD) 모두 포함. 최소 4자 이상.
_IDENTIFIER_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9_]{3,}\b")
# 무시할 일반 영단어 (한국어 쿼리에 섞일 수 있는 짧은 영문)
_STOPWORDS = {"this", "that", "with", "from", "have", "what", "when", "where", "which", "would", "could",
              "should", "about", "into", "data", "code", "file", "function", "class", "type", "name",
              "task", "tasks", "user", "users", "true", "false", "null", "none", "self", "table"}
# LIKE 검색 시 임베딩 sim에 더해줄 보너스 (LIKE 매칭은 정확하므로 강한 부스트)
_LIKE_BOOST = 0.20

# [r101] 한국어 추상 키워드 → TDA 프로젝트의 영문 식별자 매핑.
# 한국어로 "칸반 데이터 모델" 같이 질문하면 영문 식별자가 0개라 LIKE 검색이
# 작동 안 했음 — 이 매핑으로 영문 키워드를 자동 추가해 정확 매칭 보강.
# 키는 한국어 부분 단어, 값은 LIKE 검색에 추가될 영문 식별자 리스트.
_KOREAN_KEYWORD_MAP = {
    # 칸반 / 보드
    "칸반": ["kanban", "Category", "Board", "renderBoard", "kanban_categories"],
    "보드": ["Board", "renderBoard", "kanban_categories"],
    "카테고리": ["Category", "kanban_categories", "dbUpsertCategory", "dbDeleteCategory", "addNewCategory"],
    "컬럼": ["column", "field"],
    # 태스크 / 카드
    "태스크": ["Task", "tasks", "dbUpsertTask", "dbDeleteTask", "addNewTask"],
    "카드": ["task", "dbUpsertTask", "renderBoard", "openCardModal"],
    # 스프린트
    "스프린트": ["Sprint", "sprints", "dbUpsertSprint", "startSprint", "endSprint", "renderSprint"],
    "회고": ["retrospective", "generateRetrospective", "endSprint"],
    "끼어들기": ["intrusion", "intrusionCount", "intrusionLog"],
    # 데이터 / 스키마
    "데이터": ["payload", "schema", "table", "dbUpsert"],
    "모델": ["model", "schema", "table", "payload"],
    "테이블": ["table", "schema", "supabaseClient", "from"],
    "스키마": ["schema", "table", "migration"],
    "구조": ["payload", "schema", "model"],
    "필드": ["field", "column", "payload"],
    # 인증 / 사용자
    "로그인": ["login", "auth", "signIn", "supabaseClient.auth"],
    "인증": ["auth", "OAuth", "deep_link"],
    "사용자": ["user", "users", "currentUser", "myNickname"],
    "프로필": ["profile", "user", "displayName", "avatar"],
    # 문서 / 위키
    "문서": ["doc", "wiki_docs", "dbUpsertDoc", "renderDoc"],
    "위키": ["wiki", "wiki_docs", "Canon", "wikiDoc"],
    "그래프": ["graph", "diagram", "renderGraph"],
    # 리뷰 / 결재
    "리뷰": ["review", "review_requests", "dbUpsertReview"],
    "결재": ["approval", "review", "approve"],
    # 일반 함수/검색
    "함수": ["function", "async"],
    "검색": ["search", "filter", "query"],
    "필터": ["filter", "applyFilters", "filterDev", "filterStatus"],
    "렌더": ["render", "renderBoard", "renderSprint"],
    # Zone (TDA 고유)
    "선반": ["Shelf", "zone"],
    "지금": ["Now", "zone"],
    "묻힘": ["Buried", "zone"],
    # 캘린더 / 일정
    "캘린더": ["calendar", "calendar_events", "dbUpsertEvent"],
    "일정": ["event", "calendar_events"],
    # 자료
    "에셋": ["asset", "assets", "asset_folders"],
    "이슈": ["issue", "issues"],
    "버그": ["bug", "bug_reports"],
    # Deep Wiki
    "임베딩": ["embedding", "embed", "ollama"],
    "청크": ["chunk", "doc_chunks", "chunk_text", "chunk_code"],
    "유사도": ["similarity", "SIMILARITY_THRESHOLD"],
}


def _extract_identifiers(query: str) -> List[str]:
    """[r97→r101→r102] 쿼리에서 검색에 쓸 키워드 추출.

    1) 영문 식별자(camelCase/snake_case 등) 정규식 추출.
    2) [r101] 한국어 추상 키워드는 _KOREAN_KEYWORD_MAP으로 영문 식별자 보강.
    3) [r102] 한국어 매핑 키 자체도 LIKE 검색 키워드에 추가 — sprint/task/wiki
       청크가 순수 한국어로 저장돼 있을 때 잡기 위함.
    """
    matches = _IDENTIFIER_RE.findall(query or "")
    seen = set()
    out = []
    for m in matches:
        lower = m.lower()
        if lower in _STOPWORDS:
            continue
        if m in seen:
            continue
        seen.add(m)
        out.append(m)
    # [r101] 한국어 부분 단어 → 매핑된 영문 식별자 추가
    q = query or ""
    expanded: List[str] = []
    for ko, mapped in _KOREAN_KEYWORD_MAP.items():
        if ko in q:
            # [r102] 매핑 키(한국어 단어) 자체도 LIKE 키워드로 추가 — sprint 등
            # 청크가 한국어로 저장돼 있을 때 매칭되도록.
            if ko not in seen:
                expanded.insert(0, ko)  # 한국어를 앞쪽에 배치 — 우선 LIKE
                seen.add(ko)
            for m in mapped:
                if m in seen or m in expanded:
                    continue
                expanded.append(m)
                seen.add(m)
    # 폭주 방지 — 매핑 확장은 최대 12개까지만 (한국어 + 영문)
    out.extend(expanded[:12])
    return out


async def retrieve(
    query: str,
    project_id: Optional[str] = None,
    source_types: Optional[List[str]] = None,
    top_k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """질문 → 임베딩 + 영문/한국어 키워드 LIKE → 병합 → 다양성 → top_k.

    [r97/r101/r102] 하이브리드 검색:
    - 임베딩 벡터 검색 (의미 매칭)
    - 쿼리에 영문 식별자가 있으면 LIKE 정확 매칭도 병행
    - 한국어 매핑 키도 LIKE 검색에 포함 (sprint 등 한국어 청크 잡기)
    - LIKE 매칭은 sim +0.20 부스트로 상위 강제

    [r106] source_type 다양성 보장:
    - top_k 안에 sprint·task·wiki 각 최소 1~2개씩 강제 포함 (있다면)
    - code 한 종류가 결과 전체를 점유하지 않도록
    """
    top_k = top_k or settings.TOP_K
    ollama = get_ollama()
    store = get_store()

    # 1. 벡터 임베딩 검색
    embedding = await ollama.embed(query)
    fetch_count = max(top_k * 5, 40)  # [r106] 더 많이 뽑아서 다양성 위한 후보 확보
    vec_results = store.search(
        query_embedding=embedding,
        project_id=project_id,
        source_types=source_types,
        top_k=fetch_count,
    )

    # 2. [r97] 영문 식별자 + [r102] 한국어 매핑 키 LIKE 검색
    identifiers = _extract_identifiers(query)
    like_results = []
    if identifiers:
        like_results = _like_search(
            store=store,
            identifiers=identifiers,
            project_id=project_id,
            source_types=source_types,
            limit=fetch_count,
        )

    # 3. 병합
    merged = _merge_results(vec_results, like_results)
    if not merged:
        return []

    # 4. 임계치 필터 + 폴백
    filtered = [r for r in merged if r.get("similarity", 0) >= SIMILARITY_THRESHOLD]
    if len(filtered) < 3:
        filtered = merged

    # 5. source_id별 청크 수 제한 (한 파일이 결과 점유 방지)
    diversified = _dedup_by_source(filtered, max_per_source=MAX_CHUNKS_PER_SOURCE)

    # 6. [r106] source_type 다양성 보장 — sprint/task/wiki 무조건 포함
    return _ensure_type_diversity(diversified, top_k)


# [r106] source_type별 결과 쿼터 — top_k 안에서 각 타입 최소 보장.
# code는 코드/문서 파일, wiki/task/sprint는 사용자가 만든 라이브 데이터.
# 사용자 질문이 칸반·스프린트·태스크에 관한 경우 라이브 데이터가 더 중요한데
# 보고서.md 같은 큰 code 파일에 밀려 안 들어오는 문제 해결.
_TYPE_QUOTAS = {
    "sprint": 2,  # 사용자의 실제 스프린트 정보 — 가장 적게 인덱싱돼서 강제 포함
    "task": 3,    # 칸반 카드 — 사용자가 만든 핵심 데이터
    "wiki": 2,    # 사용자가 쓴 위키 문서
    "code": 5,    # 코드/문서 파일 — 양이 많으니 쿼터 큼
}


def _ensure_type_diversity(chunks: List[Dict[str, Any]], top_k: int) -> List[Dict[str, Any]]:
    """[r106] top_k 안에 각 source_type 최소 N개 보장.

    1) source_type별로 청크 분리 (유사도 내림차순 유지)
    2) 각 type 쿼터만큼 우선 채택
    3) 남은 자리는 sim 높은 순으로 채움
    4) 최종 sim 내림차순 정렬
    """
    if not chunks:
        return []
    by_type: Dict[str, List[Dict[str, Any]]] = {}
    for c in chunks:
        t = c.get("source_type") or "?"
        by_type.setdefault(t, []).append(c)

    # 각 type 쿼터만큼 채택 (있는 만큼만)
    picked_ids = set()
    result: List[Dict[str, Any]] = []
    for stype, quota in _TYPE_QUOTAS.items():
        for c in by_type.get(stype, [])[:quota]:
            cid = c.get("id")
            if cid not in picked_ids:
                picked_ids.add(cid)
                result.append(c)

    # 남은 자리는 sim 높은 순으로
    remaining = [c for c in chunks if c.get("id") not in picked_ids]
    remaining.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    while len(result) < top_k and remaining:
        c = remaining.pop(0)
        picked_ids.add(c.get("id"))
        result.append(c)

    # 최종 sim 내림차순
    result.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    return result[:top_k]


def _like_search(
    store,
    identifiers: List[str],
    project_id: Optional[str],
    source_types: Optional[List[str]],
    limit: int,
) -> List[Dict[str, Any]]:
    """[r97] doc_chunks.content에 식별자가 정확히 들어간 청크를 LIKE로 검색.

    각 식별자에 대해 ilike '%ident%' 매칭. 결과를 식별자별로 모은 뒤 중복 제거.
    similarity는 LIKE 매칭이므로 0.5(보수적 기본값)에 부스트 합산 — _merge_results에서 처리.
    """
    out_by_id: Dict[str, Dict[str, Any]] = {}
    for ident in identifiers[:5]:  # 식별자 최대 5개까지만 — 폭주 방지
        try:
            q = store.client.table("doc_chunks").select(
                "id,project_id,source_type,source_id,source_path,source_title,content,token_count"
            )
            if project_id:
                q = q.eq("project_id", project_id)
            if source_types:
                q = q.in_("source_type", source_types)
            # PostgREST의 ilike — content 내 식별자 포함
            q = q.ilike("content", f"%{ident}%").limit(limit)
            res = q.execute()
            for row in (res.data or []):
                cid = row["id"]
                if cid in out_by_id:
                    continue
                # LIKE 매칭 청크는 sim 기본값을 0.5로 설정 (의미 매칭과 비교 가능하게)
                # 추후 _merge_results에서 부스트 적용
                row["similarity"] = 0.50
                row["_match_kind"] = "like"
                row["_match_ident"] = ident
                out_by_id[cid] = row
        except Exception as e:
            # LIKE 실패는 치명적 아님 — 임베딩 결과만으로 진행
            print(f"[retriever] LIKE search failed for '{ident}': {e}")
    return list(out_by_id.values())


def _merge_results(
    vec_results: List[Dict[str, Any]],
    like_results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """[r97] 벡터 검색 결과 + LIKE 검색 결과 병합.

    같은 청크 id가 양쪽에 있으면 sim = max(vec_sim + LIKE_BOOST, like_default).
    LIKE 매칭만 있는 청크는 sim 0.50 + LIKE_BOOST = 0.70.
    벡터 매칭만 있는 청크는 그대로.
    """
    by_id: Dict[Any, Dict[str, Any]] = {}
    for r in vec_results:
        by_id[r["id"]] = dict(r)
    for r in like_results:
        cid = r["id"]
        if cid in by_id:
            # 양쪽 매칭 — 벡터 sim에 부스트 추가
            existing = by_id[cid]
            existing["similarity"] = min(1.0, existing.get("similarity", 0) + _LIKE_BOOST)
            existing["_match_kind"] = "vec+like"
            existing["_match_ident"] = r.get("_match_ident")
        else:
            # LIKE만 매칭 — 기본 sim에 부스트
            rr = dict(r)
            rr["similarity"] = min(1.0, rr.get("similarity", 0.50) + _LIKE_BOOST)
            by_id[cid] = rr
    # 유사도 내림차순 정렬
    return sorted(by_id.values(), key=lambda x: x.get("similarity", 0), reverse=True)


def _dedup_by_source(chunks: List[Dict[str, Any]], max_per_source: int = 2) -> List[Dict[str, Any]]:
    """[r96] 같은 source_id의 청크를 max_per_source개까지만 유지."""
    counts: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    for c in chunks:
        sid = c.get("source_id") or "(unknown)"
        n = counts.get(sid, 0)
        if n >= max_per_source:
            continue
        counts[sid] = n + 1
        out.append(c)
    return out
