# Deep Wiki + AI Agent 통합 시스템 — 테스트 가이드 (r116 완료 시점)

## ✅ 완성된 기능 (r93~r116)

### 🤖 AI Agent (사이드바 → 도구 그룹)
Qwen 2.5 Coder 14B + Tool Use 챗봇. 15개 도구로 앱 라이브 데이터·정적 컨텐츠·자동 위키 능동 조회.

**도구 목록 (15개):**
| # | 도구 | 무엇을 함 |
|---|---|---|
| 1 | `get_active_sprint` | 진행중 스프린트 + 카드 + 카테고리 |
| 2 | `list_tasks` | 칸반 카드 필터 검색 |
| 3 | `search_vector` | 코드/문서 벡터 의미 검색 |
| 4 | `list_docs` | wiki_docs 문서 목록 |
| 5 | `list_reviews` | 결재 요청 목록 |
| 6 | `list_calendar_events` | 일정 |
| 7 | `list_issues` | 이슈 트래커 |
| 8 | `list_sprints` | 스프린트 메타 목록 |
| 9 | `list_users` | 팀원 목록 |
| 10 | `get_project_info` | 프로젝트 종합 메타 |
| 11 | `list_wiki_pages` | Deep Wiki 1차 위키 페이지 목록 |
| 12 | `get_wiki_page` | Deep Wiki 페이지 본문 |
| 13 | `search_wiki_pages` | Deep Wiki LIKE 검색 |
| 14 | `list_wiki_audits` | 2차 기획 대조 보고서 목록 |
| 15 | `get_wiki_audit` | 보고서 본문 상세 |

### 📘 Deep Wiki (사이드바 → 자료 그룹)
유니티 Git 레포 LLM 자동 분석 + 기획 일치도 보고서.

- **🤖 위키 자동 생성** → 1차 MD (시스템 개요·폴더 구조·주요 클래스·의존성·코드 발췌)
- **📊 기획 대조 보고서** → 2차 MD (canon 문서 vs 1차 위키 매핑·일치도·findings)
- **본문 기능**: 마크다운 + Mermaid + 코드 강조 + h2/h3 TOC + 페이지 검색 + SPA 페이지 링크

## 🧪 테스트 순서

### 사전 준비
```powershell
cd C:\projects\tda-dashboard
git pull origin main
```

**Supabase SQL Editor** 에서 실행 (한 번):
```
deepwiki-integration/migrations/001_doc_chunks.sql    (r90 시점)
deepwiki-integration/migrations/002_wiki_pages.sql    (r112 시점)
```

**서버 재시작:**
```powershell
cd deepwiki-integration\server
.\run.bat
```

시작 로그에서 확인:
- ✓ Ollama 연결 OK
- ✓ LLM 'qwen2.5-coder:14b' 사용 가능
- ✓ 임베딩 'nomic-embed-text' 사용 가능
- ✓ Supabase 연결 OK
- ✓ 설정 값 정상

---

### 시나리오 1️⃣ — AI Agent 라이브 데이터 (r108~r111)

사이드바 → 도구 → 🤖 **AI Agent**

```
진행중인 스프린트 알려줘
```
→ `get_active_sprint` 호출 → W22 sprint 마크다운 표 (목표·기간·끼어들기·카드 4개)

```
이번달 일정 있어?
```
→ `list_calendar_events` 호출

```
내 결재 대기 문건
```
→ `list_reviews(status='pending')`

```
프로젝트 정보 알려줘
```
→ `get_project_info` → 이름·Git URL·참여자·통계 (이전엔 ID만 나왔던 문제 해결)

---

### 시나리오 2️⃣ — Deep Wiki 자동 위키 생성 (r113~r114)

사이드바 → 자료 → 📘 **Deep Wiki**

1. **🤖 위키 자동 생성** 클릭
2. Git URL 입력 (예: 본인 유니티 프로젝트)
3. 1~5분 대기. 좌측 진행률:
   ```
   🔄 Git clone → ✅ 완료 (커밋 abc123)
   📁 카테고리 8개 발견
   🤖 [1/8 · 12%] 매니저 (Managers)
   🤖 [2/8 · 25%] 시스템 (Systems)
   ...
   🎉 완료 — 9개 페이지 생성됨
   ```
4. 좌측 트리에 자동 생성 페이지 등장 → 클릭해서 본문 확인
   - 마크다운 + 코드 발췌 + 표
   - **Mermaid 다이어그램** 자동 렌더 (r114)
   - **우측 TOC** (h2/h3 자동 추출, 클릭 스크롤)
   - **검색바** (페이지 제목 부분 매칭)

---

### 시나리오 3️⃣ — 2차 기획 대조 보고서 (r115)

**선행 조건:** 프로젝트 위키(canon kind) 문서가 1개 이상 있어야 함. 사이드바 → 운영 → 프로젝트 위키에서 "Official" 문서 작성.

1. Deep Wiki 페이지에서 **📊 기획 대조 보고서** 클릭
2. 확인 다이얼로그 → 시작
3. canon별로 LLM이 1차 위키와 대조 (1~3분):
   ```
   📊 [1/3 · 33%] 전투 시스템 (기획)
   ✅ 전투 시스템 — 기획 대조 보고서 (일치도 87%)
   ...
   🎉 완료 — 3개 보고서 생성됨
   ```
4. 좌측 트리에 새 섹션 **📊 기획 대조 보고서 (2차)** 등장
5. 보고서 클릭 → 상단에 큰 일치도 % 게이지 + findings 카운트
   - 본문: 매핑 표 + 미구현/부분/기획외 분류 + 결론

---

### 시나리오 4️⃣ — AI Agent가 Deep Wiki 활용 (r116, 진짜 통합)

다시 AI Agent로 가서:

```
전투 시스템 위키 페이지 보여줘
```
→ `get_wiki_page(slug='combat')` → 본문 인용 답변

```
dbUpsertCategory가 어느 위키 페이지에 있어?
```
→ `search_wiki_pages(query='dbUpsertCategory')` → 발췌와 함께 페이지 안내

```
기획 대조 결과 어때?
```
→ `list_wiki_audits` → 일치도 표 + 가장 문제 많은 보고서 강조

```
[보고서 제목] 상세 내용
```
→ `get_wiki_audit` → 본문 분석 답변

---

## 진단 패널 (문제 발생 시)

⚙️ AI Agent 설정 → 📊 진단:
- **🩺 서버 상태** — Ollama·Supabase·청크 통계
- **📁 인덱스 파일 목록** — 어떤 파일이 인덱싱됐는지
- **🔍 검색 테스트** — 임의 쿼리로 sim·매칭 확인
- **📜 마지막 인덱싱 로그** — SSE 이벤트 200개 누적

## 알려진 한계

1. **Qwen 2.5 Coder tool_calls 응답** — message.tool_calls 필드 대신 content에 JSON으로 오는 경우 있음 → r109 자동 fallback 파싱으로 처리됨
2. **임베딩 모델 한↔영 cross-domain 약함** — r101 한국어→영문 매핑 사전(40개)으로 보강
3. **거대 단일 HTML(>1MB)** — r98에서 MAX_FILE_SIZE_KB 4000으로 상향
4. **위키 자동 생성 시간** — 카테고리당 LLM 호출 1회 (10~40초). 큰 레포는 5분+ 가능
5. **canon 문서 부족 시 감사 실패** — `kind='canon'`인 wiki_docs가 없으면 2차 보고서 생성 불가

## API 엔드포인트 (총 14개)

```
GET    /health[?verbose=true]
POST   /chat
POST   /index/code
POST   /index/wiki
POST   /index/task
POST   /index/sprint
POST   /index/cleanup_empty
DELETE /index/all

POST   /wiki/generate
GET    /wiki/pages
GET    /wiki/page?slug=...
DELETE /wiki/pages
POST   /wiki/audit
GET    /wiki/audits
GET    /wiki/audit/{audit_id}
GET    /debug/search
```

## 코드 구조

```
deepwiki-integration/
  server/
    main.py            — FastAPI 진입점, 라우팅, startup 검증
    config.py          — Settings (CHUNK_SIZE·MAX_FILE_SIZE_KB·TOP_K)
    ollama_client.py   — Ollama API + chat_with_tools + content tool_call fallback
    supabase_store.py  — pgvector 어댑터
    chunker.py         — 시그니처 기반 청킹
    retriever.py       — 하이브리드 검색 (벡터+LIKE), 다양성 dedup, 한↔영 매핑
    rag.py             — (레거시) 단순 RAG, ?model=legacy 시 사용
    agent.py           — Tool-Use 에이전트 루프
    tools.py           — 15개 도구 정의·실행기
    indexer.py         — code/wiki/task/sprint 인덱싱 + 빈 템플릿 필터
    wiki_generator.py  — Phase A: 1차 자동 위키 페이지 생성
    wiki_auditor.py    — Phase C: 2차 기획 대조 보고서
  migrations/
    001_doc_chunks.sql — pgvector 청크 저장소
    002_wiki_pages.sql — Deep Wiki 페이지 + 감사 보고서
  DEEPWIKI_ROADMAP.md  — 큰 그림
  TEST_GUIDE_R116.md   — 이 파일
```
