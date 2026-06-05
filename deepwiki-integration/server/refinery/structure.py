"""[r246] 로직 우선 구조 분석 — LLM 없이 문서 구조화.

분해 단계에서 곧바로 LLM 을 부르지 않고, 먼저 로직으로:
  - md 헤더(#, ##...)로 목차·대분류 트리
  - 헤더 없으면 문서 1섹션
  - 섹션별 키워드 빈도(불용어 제거 + 인접 2-그램) 상위 추출 → 하위 노드
결과(nodes)는 LLM decompose 와 동일 형태라 이후 단계(추천/제안/위키)가 그대로 동작.
사용자가 결과가 마음에 안 들면 [LLM 으로 분해]를 누른다.
"""
import re
import time
from collections import Counter
from typing import List, Dict, Any, Optional

# 한국어 조사/어미/흔한 기능어 + 영어 불용어
_STOP = set("""
의 가 이 은 는 을 를 에 와 과 도 으로 로 에서 에게 께 한테 부터 까지 만 보다 처럼 같이 및 또는 그리고 그러나 하지만
그래서 따라서 즉 등 수 것 적 인 한 할 하는 하고 하여 되어 된 될 들 더 매우 가장 또 또한 이런 그런 저런 어떤 무슨
이것 그것 저것 여기 거기 저기 때 때문 위해 대해 대한 통해 따라 있다 없다 있는 없는 한다 했다 된다 됐다 이다 였다
우리 저희 그들 당신 자신 경우 정도 부분 내용 사용 통한 관련 다음 이전 현재
a an the of to in on for and or but is are was were be been being with as by at from this that these those it its
""".split())

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_\-]+|[가-힣]{2,}")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _uid(prefix: str = "n") -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{int.from_bytes(__import__('os').urandom(2), 'big')}"


_HTML_TAGRE = re.compile(r"<[^>]+>")
_STYLE_SCRIPT = re.compile(r"<(style|script)[\s\S]*?</\1>", re.I)


def _strip(text: str) -> str:
    """HTML 이면 style/script/태그 제거(자체 완결 — 무거운 import 없음)."""
    t = text or ""
    if "<" in t and (">" in t) and ("</" in t or "/>" in t):
        t = _STYLE_SCRIPT.sub(" ", t)
        t = _HTML_TAGRE.sub(" ", t)
        t = re.sub(r"&[a-z#0-9]+;", " ", t)
        t = re.sub(r"[ \t]+", " ", t)
    return t


def keywords(text: str, top: int = 8) -> List[Dict[str, Any]]:
    """섹션 텍스트 → [{text, count}] (빈도 상위 단어 + 의미있는 2-그램)."""
    toks = [t.lower() for t in _TOKEN.findall(text or "") if t.lower() not in _STOP and len(t) >= 2]
    if not toks:
        return []
    uni = Counter(toks)
    big = Counter()
    for i in range(len(toks) - 1):
        a, b = toks[i], toks[i + 1]
        if a in _STOP or b in _STOP or a == b:   # 동어반복 2-그램 제외
            continue
        big[a + " " + b] += 1
    out: List[Dict[str, Any]] = []
    seen = set()
    # 빈도 2+ 2-그램 우선(더 구체적)
    for p, c in big.most_common(top):
        if c < 2:
            break
        out.append({"text": p, "count": c}); seen.add(p)
    for w, c in uni.most_common(top * 2):
        if len(out) >= top:
            break
        if w in seen or any(w in p for p in seen):
            continue
        out.append({"text": w, "count": c}); seen.add(w)
    out.sort(key=lambda x: -x["count"])
    return out[:top]


def _sections_from_headings(body: str) -> List[Dict[str, Any]]:
    """md 헤더로 (level, title, text) 섹션 목록. 헤더 없으면 [] 반환."""
    lines = body.split("\n")
    heads = []
    for i, ln in enumerate(lines):
        m = _HEADING.match(ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2).strip()))
    if not heads:
        return []
    secs = []
    # 첫 헤더 이전 서문은 무시(또는 '개요')
    for k, (li, lvl, title) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        text = "\n".join(lines[li + 1:end]).strip()
        secs.append({"level": lvl, "title": title, "text": text})
    return secs


def analyze_structure(vault_docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """vault → 로직 구조 노드(LLM 없음). {nodes, doc_count, section_count, has_headings}."""
    nodes: List[Dict[str, Any]] = []
    section_count = 0
    any_heading = False

    for vd in vault_docs:
        body = _strip(vd.get("content") or "").strip()
        if not body:
            continue
        vid, vtitle = vd.get("id"), vd.get("title") or "(문서)"
        secs = _sections_from_headings(body)
        if secs:
            any_heading = True
            # 헤더 계층 → parent 매핑(스택)
            stack: List[Dict[str, Any]] = []  # [{level, id}]
            for sec in secs:
                section_count += 1
                while stack and stack[-1]["level"] >= sec["level"]:
                    stack.pop()
                parent_id = stack[-1]["id"] if stack else None
                cat_id = _uid("n")
                nodes.append({
                    "id": cat_id, "title": sec["title"][:60], "kind": "category",
                    "summary": "", "parent_id": parent_id, "ai_confidence": 70,
                    "level": sec["level"], "auto_category_hint": "canon",
                    "source_refs": [{"vault_id": vid, "vault_title": vtitle, "span_text": sec["text"][:120]}],
                })
                stack.append({"level": sec["level"], "id": cat_id})
                # 섹션 키워드 → 하위 concept 노드
                for kw in keywords(sec["text"], top=6):
                    nodes.append({
                        "id": _uid("n"), "title": kw["text"][:60], "kind": "concept",
                        "summary": f"'{sec['title']}' 섹션 주요 키워드 (빈도 {kw['count']})",
                        "parent_id": cat_id, "ai_confidence": min(95, 50 + kw["count"] * 5),
                        "auto_category_hint": "canon",
                        "source_refs": [{"vault_id": vid, "vault_title": vtitle, "span_text": kw["text"]}],
                    })
        else:
            # 헤더 없음 → 문서 1섹션 + 전체 키워드
            section_count += 1
            cat_id = _uid("n")
            nodes.append({
                "id": cat_id, "title": vtitle[:60], "kind": "category", "summary": "",
                "parent_id": None, "ai_confidence": 60, "level": 1, "auto_category_hint": "canon",
                "source_refs": [{"vault_id": vid, "vault_title": vtitle, "span_text": body[:120]}],
            })
            for kw in keywords(body, top=10):
                nodes.append({
                    "id": _uid("n"), "title": kw["text"][:60], "kind": "concept",
                    "summary": f"주요 키워드 (빈도 {kw['count']})", "parent_id": cat_id,
                    "ai_confidence": min(95, 50 + kw["count"] * 4), "auto_category_hint": "canon",
                    "source_refs": [{"vault_id": vid, "vault_title": vtitle, "span_text": kw["text"]}],
                })

    return {
        "nodes": nodes,
        "doc_count": len(vault_docs),
        "section_count": section_count,
        "leaf_count": sum(1 for n in nodes if n.get("kind") != "category"),
        "has_headings": any_heading,
    }
