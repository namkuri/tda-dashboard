# Deep Wiki 자동 위키 시스템 — 로드맵

## 최종 목표

DeepWiki-Open ([github.com/AsyncFuncAI/deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open)) 형태를 모사한 자동 위키 생성기. 유니티 프로젝트 Git 레포를 LLM이 읽어:

1. **1차 MD** — 시스템·폴더·클래스 자동 위키 페이지 N개 생성
2. **2차 MD** — 프로젝트 위키(canon 기획문서) 와 대조해 일치도 보고서 생성
3. **DeepWiki 형태 UI** — 좌측 트리 + 본문 마크다운 + 코드 인용·다이어그램
4. **AI Agent 통합** — 챗봇이 1·2차 MD를 도구로 조회

## 카테고리 분리 (r112 완료)

| 사이드바 메뉴 | 위치 | 역할 |
|---|---|---|
| **🤖 AI Agent** (도구) | 기존 r89~r111의 Qwen 챗봇 — 라이브 데이터 능동 조회 |
| **📘 Deep Wiki** (자료) | NEW — 유니티 코드 자동 위키 + 기획 일치도 보고서 |

## Phase별 진행 (r112~r116)

### ✅ r112 — 골격
- AI Agent ↔ Deep Wiki 메뉴 분리
- `deep_wiki_pages`, `deep_wiki_audits` SQL 마이그레이션
- 빈 페이지 + "위키 자동 생성" / "기획 대조" 버튼 자리
- `get_project_info` 도구 (AI Agent용) 추가

### ✅ r113 — Phase A: 1차 MD 자동 생성기

**백엔드:**
- `POST /wiki/generate` 엔드포인트 (SSE 진행률)
- `wiki_generator.py` 모듈:
  1. Git clone (이미 인덱서에서 함)
  2. 파일 트리 분석 → 카테고리 자동 추론 (Scripts/, Managers/, Systems/, Prefabs/, Scenes/ 등)
  3. 카테고리별로 LLM 호출 — "이 파일들로 위키 페이지 1개 작성"
  4. 페이지 N개 결과를 `deep_wiki_pages` 에 INSERT
  5. 페이지 슬러그·계층 관계·태그 자동 부여

**생성 페이지 예시 (유니티 프로젝트 기준):**
- `system-overview` — 프로젝트 전체 개요
- `folder-structure` — 폴더 구조 + 책임 분담
- `managers` — Manager 클래스들의 책임
- `combat-system`, `inventory-system`, `quest-system` — 시스템별
- `data-models` — ScriptableObject·struct 등
- `dependencies` — 클래스 의존성 그래프 (Mermaid)
- `entry-points` — Awake/Start 위치, 게임 루프

### ✅ r114 — Phase B: Deep Wiki UI 완성

- 좌측 페이지 트리 (parent_slug 기반 계층)
- 본문: 마크다운 렌더 + 코드블록 강조 + 인용 링크
- Mermaid 다이어그램 지원
- 페이지 내부 링크 (`[[other-page]]` 형태)
- 검색바
- 페이지 메타 (생성 시각, 관련 파일 N개, 태그)
- DeepWiki.com 비주얼 모사 (좌측 220px 트리 / 본문 800px center / 우측 TOC)

### ✅ r115 — Phase C: 2차 MD 기획 대조

**백엔드:**
- `POST /wiki/audit` 엔드포인트
- `wiki_auditor.py` 모듈:
  1. 1차 페이지 N개 + 프로젝트 위키(canon kind) 문서 M개 가져옴
  2. LLM에 매핑 요청 — "이 기획 문서에 명시된 시스템 vs 1차 위키에 적힌 실제 시스템"
  3. 일치도 점수 (0~1) + finding 배열 (missing/mismatch/extra/severity)
  4. `deep_wiki_audits` 에 보고서 저장

**UI:**
- Deep Wiki 페이지에 "📊 감사" 탭 추가
- 보고서 카드 — 일치도 게이지, 차이점 리스트, 관련 페이지·기획 링크

### ✅ r116 — Phase D: AI Agent 통합 (완료)

**도구 추가 (tools.py):**
- `list_wiki_pages(project_id, parent?, limit?)` — 1차 페이지 목록
- `get_wiki_page(slug)` — 특정 페이지 본문
- `search_wiki_pages(query)` — 페이지 내용 의미 검색 (벡터)
- `list_wiki_audits(project_id)` — 2차 보고서 목록
- `get_wiki_audit(id)` — 보고서 상세

**기대 시나리오:**
> 사용자: "전투 시스템이 기획대로 잘 구현됐어?"  
> AI Agent: `list_wiki_audits` 호출 → "전투 시스템 일치도 보고서" 찾음 → `get_wiki_audit` → 답변에 일치도 87%, 빠진 콤보 시스템 3개 등 인용

## 데이터 흐름

```
유니티 Git 레포 (사용자 입력 URL)
    │
    ├─→ [r113] /wiki/generate
    │     │
    │     ├─ Git clone + 파일 트리 스캔
    │     ├─ 카테고리별 LLM 분석
    │     └─ deep_wiki_pages 테이블 INSERT (N개)
    │
    ├─→ [r114] Deep Wiki UI
    │     ├─ 좌측 트리: parent_slug 그룹화
    │     └─ 본문: marked.js + 코드블록 + Mermaid
    │
    ├─→ [r115] /wiki/audit
    │     │
    │     ├─ wiki_docs (kind=canon) + deep_wiki_pages 가져옴
    │     ├─ LLM 대조 분석
    │     └─ deep_wiki_audits 테이블 INSERT
    │
    └─→ [r116] AI Agent 도구
          ├─ list_wiki_pages / get_wiki_page / search_wiki_pages
          └─ list_wiki_audits / get_wiki_audit
```

## 사용자 액션 시점

- **지금 (r112 끝)**: SQL 002_wiki_pages.sql 실행, 메뉴 분리 확인
- **r113 끝**: 자동 위키 생성 시도 (Git URL → 1차 페이지 N개 자동 생성 확인)
- **r114 끝**: Deep Wiki 페이지에서 트리·본문 확인
- **r115 끝**: 기획 대조 보고서 확인
- **r116 끝**: AI Agent에서 위키 관련 질문 답변 확인

## 참고

- 원본 OSS: [AsyncFuncAI/deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open)
- 우리의 차별점: 기획 대조 (2차 MD) 가 추가됨. 단순 코드 위키가 아니라 "기획-구현 갭" 가시화.
