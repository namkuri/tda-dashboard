"""[r223] 작성자 강제 가드 — 모든 산출물에 created_by 필수.

문서 작성 시 작성자 누락되고 히스토리 누락되는 사안 방지 (사용자 정책).
누락 시 HTTPException(422). 프론트는 currentUser.id 자동 주입.
"""
from typing import Any, Dict, Optional
from datetime import datetime, timezone
from fastapi import HTTPException


def require_author(user_id: Optional[str], context: str = "산출물") -> str:
    """user_id 필수. None 이면 422."""
    if not user_id or not user_id.strip():
        raise HTTPException(
            status_code=422,
            detail=f"작성자(user_id) 가 필요합니다 — {context} 저장 차단 (정책: 작성자 누락 0)",
        )
    return user_id.strip()


def history_entry(action: str, by: str, detail: str = "", **extra) -> Dict[str, Any]:
    """history[] 에 push 할 엔트리 1개 생성. 표준 포맷."""
    now = datetime.now(timezone.utc).isoformat()
    e = {"action": action, "by": by, "at": now, "detail": detail}
    for k, v in extra.items():
        if v is not None:
            e[k] = v
    return e


def stamp_metadata(
    obj: Dict[str, Any],
    *,
    user_id: str,
    action: str,
    detail: str = "",
    is_new: bool = False,
) -> Dict[str, Any]:
    """객체에 created_by/updated_by/updated_at + history push.

    is_new=True 면 created_by/created_at 도 설정.
    """
    require_author(user_id, context=f"action={action}")
    now = datetime.now(timezone.utc).isoformat()
    obj = dict(obj or {})
    if is_new:
        obj.setdefault("created_by", user_id)
        obj.setdefault("created_at", now)
    obj["updated_by"] = user_id
    obj["updated_at"] = now
    hist = obj.get("history") or []
    if not isinstance(hist, list):
        hist = []
    hist.append(history_entry(action, user_id, detail))
    # 너무 크면 trim — 최대 200
    if len(hist) > 200:
        hist = hist[-200:]
    obj["history"] = hist
    return obj


def llm_author(model: str = "auto") -> str:
    """LLM 자동 생성물의 작성자 식별자."""
    return f"auto:llm:{model}" if model else "auto:llm"
