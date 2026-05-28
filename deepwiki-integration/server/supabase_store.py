"""Supabase pgvector 어댑터 — 청크 저장 + 유사도 검색."""
from supabase import create_client, Client
from typing import List, Optional, Dict, Any
from config import settings


class SupabaseStore:
    """doc_chunks 테이블에 청크 저장/검색.

    service_role key 사용 (RLS 우회 + bulk insert).
    """

    def __init__(self):
        self.client: Client = create_client(
            settings.SUPABASE_URL,
            settings.SUPABASE_SERVICE_KEY,
        )

    def upsert_chunks(self, chunks: List[Dict[str, Any]]) -> int:
        """청크 일괄 upsert. 중복 키(source_type+source_id+chunk_idx)는 갱신.

        chunks: 각 dict는 다음 필드 포함:
            project_id, source_type, source_id, source_path, source_title,
            chunk_idx, content, embedding (list[float]), token_count
        """
        if not chunks:
            return 0
        # supabase-py upsert는 PostgREST가 on_conflict 파라미터 지원
        res = (
            self.client.table("doc_chunks")
            .upsert(chunks, on_conflict="source_type,source_id,chunk_idx")
            .execute()
        )
        return len(res.data) if res.data else len(chunks)

    def delete_by_source(self, source_type: str, source_id: Optional[str] = None) -> int:
        """특정 source 청크 삭제 (재인덱싱 전 정리용)."""
        q = self.client.table("doc_chunks").delete().eq("source_type", source_type)
        if source_id:
            q = q.eq("source_id", source_id)
        res = q.execute()
        return len(res.data) if res.data else 0

    def delete_by_project(self, project_id: str, source_type: Optional[str] = None) -> int:
        """특정 프로젝트의 청크 삭제."""
        q = self.client.table("doc_chunks").delete().eq("project_id", project_id)
        if source_type:
            q = q.eq("source_type", source_type)
        res = q.execute()
        return len(res.data) if res.data else 0

    def search(
        self,
        query_embedding: List[float],
        project_id: Optional[str] = None,
        source_types: Optional[List[str]] = None,
        top_k: int = 8,
    ) -> List[Dict[str, Any]]:
        """벡터 유사도 검색 (RPC 호출)."""
        res = self.client.rpc(
            "match_doc_chunks",
            {
                "query_embedding": query_embedding,
                "match_project_id": project_id,
                "source_types": source_types,
                "match_count": top_k,
            },
        ).execute()
        return res.data or []

    def stats(self) -> Dict[str, Any]:
        """청크 통계 (대시보드용)."""
        try:
            res = self.client.table("doc_chunks_stats").select("*").execute()
            return {"by_source": res.data or []}
        except Exception:
            # 뷰 없음 — count 폴백
            try:
                r = self.client.table("doc_chunks").select("id", count="exact", head=True).execute()
                return {"total": r.count or 0}
            except Exception:
                return {"error": "doc_chunks 테이블 없음 — SQL 마이그레이션 미실행"}

    def top_sources(self, limit: int = 15) -> List[Dict[str, Any]]:
        """[r97] source_id별 청크 수 상위 N개 — 진단용.

        public/index.html 같은 큰 파일이 인덱싱 됐는지 즉시 확인할 수 있게.
        """
        try:
            # 단순 카운트 — Python 측 집계 (pagination으로 모두 가져옴)
            from collections import Counter
            counter = Counter()
            page = 0
            page_size = 1000
            while True:
                res = (
                    self.client.table("doc_chunks")
                    .select("source_type,source_id")
                    .range(page * page_size, (page + 1) * page_size - 1)
                    .execute()
                )
                rows = res.data or []
                if not rows:
                    break
                for r in rows:
                    counter[(r.get("source_type") or "?", r.get("source_id") or "?")] += 1
                if len(rows) < page_size:
                    break
                page += 1
            top = counter.most_common(limit)
            return [
                {"source_type": st, "source_id": sid, "chunks": n}
                for (st, sid), n in top
            ]
        except Exception as e:
            return [{"error": str(e)}]

    def ping(self) -> bool:
        """연결 확인."""
        try:
            self.client.table("doc_chunks").select("id", count="exact", head=True).limit(1).execute()
            return True
        except Exception:
            return False


_store_singleton: SupabaseStore = None


def get_store() -> SupabaseStore:
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = SupabaseStore()
    return _store_singleton
