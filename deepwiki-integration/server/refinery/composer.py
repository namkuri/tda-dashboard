"""[r223] 트리 합성 + 파일 본문 자동 작성 (옵시디언 링크 + frontmatter JSON).

스펙 §4.3, §6.1 — 트리 합성 1회 + 파일 본문 N회 + 링크 보강 1회.
"""
import json
import re
import time
from typing import AsyncIterator, Dict, Any, List, Optional

from ollama_client import get_ollama
from llm_router import get_llm  # [r226]
from ._author_guard import history_entry, llm_author


# 기본 트리 템플릿 카테고리 (LLM 이 변경 가능, 사용자도 편집 가능)
DEFAULT_TREE_FOLDERS = [
    {"path": "0. 정체성", "for_categories": ["canon"], "for_kinds": ["concept", "decision"]},
    {"path": "1. 아키텍처", "for_categories": ["canon"], "for_kinds": ["concept"]},
    {"path": "2. 핵심 메커니즘", "for_categories": ["canon"], "for_kinds": ["concept", "fact"]},
    {"path": "3. 가설 (playtest 대기)", "for_categories": ["hyp"]},
    {"path": "4. 리스크 & 방어", "for_categories": ["canon", "hyp"], "for_kinds": ["risk"]},
    {"path": "5. 백로그", "for_categories": ["later"]},
    {"path": "6. 폐기 결정", "for_categories": ["cut"]},
    {"path": "7. 정합 매트릭스", "for_categories": ["canon"], "for_kinds": ["fact"]},
    {"path": "8. 출처", "for_categories": [], "for_kinds": []},  # vault 링크용
]


def _slug(s: str) -> str:
    """파일명용 — 공백 → _, 위험 문자 제거."""
    s = re.sub(r"[\\/:\*\?\"<>\|]", "", s or "").strip()
    s = re.sub(r"\s+", "_", s)
    return s[:60] or "untitled"


def _route_node_to_folder(node: Dict[str, Any], category: str) -> str:
    """노드 → 폴더 경로 결정."""
    kind = (node.get("kind") or "concept").lower()
    if kind == "category":
        # category 노드 자체는 폴더로 변환 가능
        return None
    for folder in DEFAULT_TREE_FOLDERS:
        cats_ok = (not folder["for_categories"]) or (category in folder["for_categories"])
        kinds_ok = (not folder.get("for_kinds")) or (kind in folder["for_kinds"])
        if cats_ok and kinds_ok:
            return folder["path"]
    return "5. 백로그"  # 기본


def build_tree_skeleton(
    *,
    session_title: str,
    nodes: List[Dict[str, Any]],
    classifications: Dict[str, str],
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    """LLM 없이 트리 골격 생성 — 분류 결과 기반.

    출력:
    {
      "root": "🔬 {session_title} 시스템 정의서",
      "files": [
        {
          "path": "🌟 한장정의서.md",
          "target_kind": "canon",
          "category": "overview",
          "node_ids": [...],
          "is_overview": true,
        },
        {
          "path": "0. 정체성/한줄정의.md",
          "target_kind": "canon",
          "category": "canon",
          "node_ids": ["n_xxx"],
        },
        ...
      ]
    }
    """
    root = f"🔬 {session_title} 시스템 정의서"
    files: List[Dict[str, Any]] = []
    # 한장정의서 — 모든 확정/가설 카테고리 노드 참조
    overview_nodes = [n["id"] for n in nodes if classifications.get(n["id"]) in ("canon", "hyp")]
    files.append({
        "path": "🌟 한장정의서.md",
        "target_kind": "canon",
        "category": "overview",
        "node_ids": overview_nodes,
        "is_overview": True,
    })
    # 각 노드별 파일
    for n in nodes:
        if n.get("kind") == "category":
            continue
        cat = classifications.get(n["id"])
        if not cat:
            continue
        folder = _route_node_to_folder(n, cat)
        if folder is None:
            continue
        filename = _slug(n["title"]) + ".md"
        path = f"{folder}/{filename}"
        files.append({
            "path": path,
            "target_kind": "canon" if cat != "later" else "wiki",
            "category": cat,
            "node_ids": [n["id"]],
            "node_title": n["title"],
        })
    # 출처
    files.append({
        "path": "8. 출처/vault_원본_목록.md",
        "target_kind": "canon",
        "category": "meta",
        "node_ids": [],
        "is_vault_list": True,
    })
    files.append({
        "path": "8. 출처/changelog.md",
        "target_kind": "canon",
        "category": "meta",
        "node_ids": [],
        "is_changelog": True,
    })
    return {"root": root, "files": files}


_SYSTEM_BODY = """당신은 정련된 노드 메타와 vault 발췌를 받아 마크다운 파일 본문을 작성하는 도구입니다.

[엄격 규칙]
1. 노드 메타와 vault 발췌(source_refs) **만** 근거. 일반 지식 금지.
2. 다른 파일 언급 시 **옵시디언 링크** `[[파일명]]` 사용. 평문 X.
3. vault 인용 시 각주 `[^N]`. 본문 끝에 `[^N]: [원본 §X](vault:vault_xxx?find=...)` 형식.
4. 파일 상단에 YAML frontmatter (---) 필수:
```
---
title: "..."
category: canon|hyp|later|cut|overview
node_ids: [...]
ai_confidence: 0~100
created_by: "..."
updated_by: "..."
created_at: "..."
updated_at: "..."
tags: [...]
review: "..."
session: "..."
---
```
5. 출력은 마크다운 한 덩어리. 다른 텍스트 없음.
6. 한장정의서면 3블록 강제: 📖 소개 / 📑 목차 / 🎯 결론.
"""


async def compose_file_body(
    *,
    file_meta: Dict[str, Any],         # build_tree_skeleton 의 files 한 개
    nodes: List[Dict[str, Any]],       # 이 파일에 포함된 노드 객체들
    related_files: List[Dict[str, Any]],  # 옵시디언 링크 후보
    session: Dict[str, Any],
    user_id: str,
    model: Optional[str] = None,
) -> str:
    """LLM 한 번 호출로 파일 본문 작성."""
    ollama = get_llm(model)  # [r226]
    related_listing = ", ".join(f"[[{(f.get('node_title') or f['path'].split('/')[-1].replace('.md',''))}]]" for f in related_files[:20])
    vault_refs = []
    for n in nodes:
        for r in (n.get("source_refs") or []):
            vault_refs.append(f"- {r.get('vault_title')}: \"{(r.get('span_text') or '')[:120]}\"")
    vault_block = "\n".join(vault_refs[:12]) if vault_refs else "(없음)"
    node_listing = "\n".join(
        f"- {n['title']} ({n.get('kind','concept')}): {(n.get('summary') or '')[:200]}"
        for n in nodes
    )
    overview_extras = ""
    if file_meta.get("is_overview"):
        overview_extras = f"\n\n[이 파일은 한장정의서]\n- 📖 소개 / 📑 목차 / 🎯 결론 3 블록 강제\n- 목차에 다음 파일 [[링크]] 포함: {related_listing}\n"
    user_prompt = f"""파일 작성:
경로: {file_meta['path']}
카테고리: {file_meta.get('category')}
세션: {session.get('title')} ({session.get('id')})
작성자: {user_id}

[포함 노드 {len(nodes)}개]
{node_listing}

[vault 발췌]
{vault_block}

[관련 파일 (옵시디언 링크 후보)]
{related_listing or '(없음)'}
{overview_extras}

frontmatter + 본문 한 덩어리로. 다른 출력 없음.
"""
    buf = ""
    try:
        async for delta in ollama.chat_stream(
            messages=[
                {"role": "system", "content": _SYSTEM_BODY},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            temperature=0.4,
        ):
            buf += delta
    except Exception as e:
        return f"---\ntitle: \"{file_meta['path']}\"\nerror: \"LLM 실패\"\n---\n\n❌ LLM 실패: {e}\n"
    # 코드 펜스 제거
    buf = re.sub(r"^```(?:markdown|md)?\s*\n?", "", buf.strip())
    buf = re.sub(r"\n?```\s*$", "", buf)
    return buf.strip()


def compose_vault_list_md(*, vault_docs: List[Dict[str, Any]], session: Dict[str, Any], user_id: str) -> str:
    """LLM 없이 vault 원본 목록 자동 생성."""
    iso = _iso()
    lines = [
        "---",
        f"title: \"vault 원본 목록\"",
        f"category: meta",
        f"session: \"{session.get('id')}\"",
        f"created_by: \"{user_id}\"",
        f"updated_by: \"{user_id}\"",
        f"created_at: \"{iso}\"",
        f"updated_at: \"{iso}\"",
        f"tags: [meta, vault, source]",
        "---",
        "",
        "# 📚 vault 원본 목록",
        "",
        f"> 정련소 세션 #{session.get('id')} 에 import 된 원본 문서들. 편집 잠금.",
        "",
    ]
    for vd in vault_docs:
        lines.append(f"- [[{vd.get('title') or vd.get('id')}]] — `{vd.get('id')}`")
    return "\n".join(lines)


def compose_changelog_md(*, session: Dict[str, Any], user_id: str) -> str:
    iso = _iso()
    hist = session.get("history") or []
    lines = [
        "---",
        f"title: \"changelog\"",
        f"category: meta",
        f"session: \"{session.get('id')}\"",
        f"created_by: \"{user_id}\"",
        f"updated_by: \"{user_id}\"",
        f"created_at: \"{iso}\"",
        f"updated_at: \"{iso}\"",
        f"tags: [meta, changelog]",
        "---",
        "",
        "# 📜 변경 이력",
        "",
        f"> 세션 #{session.get('id')} 의 모든 변경.",
        "",
    ]
    for h in hist[-50:]:
        lines.append(f"- **{h.get('at')}** · `{h.get('action')}` · {h.get('by')} — {h.get('detail','')}")
    return "\n".join(lines)


def _iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
