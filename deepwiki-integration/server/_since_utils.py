"""[r209] updated_at 컬럼 타입 자동 fallback — timestamptz(ISO) ↔ bigint(epoch ms).

tda-dashboard의 wiki_docs/tasks/sprints 는 updated_at 이 bigint(ms 타임스탬프),
신규 테이블(issues/calendar_events/wbs_nodes 등) 은 timestamptz 일 수 있어
ISO 문자열을 그대로 .gte 로 던지면 22P02 'invalid input syntax for type bigint'.

이 헬퍼는 build_query 콜백을 받아 다음 순서로 자동 시도:
 1) ISO 문자열 그대로
 2) epoch ms 정수 (timestamp parse 가능할 때)
 3) since 무시 (전체 fetch)
"""
from datetime import datetime
from typing import Callable, List, Dict, Any, Optional


def _to_epoch_ms(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        s = iso
        if isinstance(s, str) and s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except Exception:
        return None


def _is_type_error(err: Exception) -> bool:
    """22P02 또는 bigint 캐스팅 실패류 인지 판정."""
    msg = str(err).lower()
    return ("22p02" in msg) or ("invalid input syntax" in msg) or ("bigint" in msg and "type" in msg)


def fetch_with_since_fallback(
    build_query: Callable[[], Any],
    since: Optional[str],
    column: str = "updated_at",
) -> List[Dict[str, Any]]:
    """build_query: () -> QueryBuilder (table().select().eq() 까지 적용된 객체).
    매번 새 객체를 반환해야 함(supabase-py 빌더가 mutating 이라).
    """
    if not since:
        try:
            res = build_query().execute()
            return res.data or []
        except Exception:
            return []
    # 1) ISO 그대로
    try:
        res = build_query().gte(column, since).execute()
        return res.data or []
    except Exception as e_iso:
        if not _is_type_error(e_iso):
            # 다른 종류 에러 — since 떼고 전체
            try:
                return build_query().execute().data or []
            except Exception:
                return []
    # 2) ms 로 재시도
    ms = _to_epoch_ms(since)
    if ms is not None:
        try:
            res = build_query().gte(column, ms).execute()
            return res.data or []
        except Exception:
            pass
    # 3) since 없이
    try:
        return build_query().execute().data or []
    except Exception:
        return []
