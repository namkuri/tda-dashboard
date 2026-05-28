# 🤖 TDA Deep Wiki — RAG 챗봇 백엔드

DeepWiki-Open의 코드 위키 생성 기법을 기반으로, **Supabase pgvector + Ollama Qwen-Coder**를 활용한 자체 호스팅 RAG 챗봇.

## 🏗️ 아키텍처

```
┌────────────────────────────────────────────────────────────┐
│  사용자 브라우저 (TDA Dashboard @ GitHub Pages)               │
│  ⚙️ 설정 모달에 백엔드 URL · 모델 · 인덱싱 트리거 버튼            │
└──────────────────────┬─────────────────────────────────────┘
                       │  HTTPS
                       ▼
┌────────────────────────────────────────────────────────────┐
│  Cloudflare Tunnel (무료, 카드 등록 불필요)                   │
│  https://random.trycloudflare.com                          │
└──────────────────────┬─────────────────────────────────────┘
                       │  localhost:8000
                       ▼
┌────────────────────────────────────────────────────────────┐
│  내 PC (Windows + RTX 5060Ti 16GB)                          │
│                                                            │
│  ┌──────────────────────────────────────────┐              │
│  │  FastAPI 서버 (uvicorn :8000)            │              │
│  │  ┌────────────────────────────────────┐ │              │
│  │  │  /health   /chat   /index/code     │ │              │
│  │  │            /index/wiki  /index/task│ │              │
│  │  └────────────────────────────────────┘ │              │
│  │   ↕ ollama_client.py                    │              │
│  └──────────┬────────────────────────┬─────┘              │
│             │                        │                    │
│  ┌──────────▼─────────┐    ┌─────────▼──────────┐         │
│  │ Ollama :11434      │    │ Supabase pgvector  │         │
│  │ - qwen2.5-coder:14b│    │ doc_chunks 테이블   │         │
│  │ - nomic-embed-text │    │ (768d 벡터)        │         │
│  └────────────────────┘    └────────────────────┘         │
└────────────────────────────────────────────────────────────┘
```

## 📂 폴더 구조

```
deepwiki-integration/
├── README.md             ← 이 파일
├── USER_TASKS.md         ← 사용자 작업 체크리스트 (먼저 읽으세요)
├── migrations/
│   └── 001_doc_chunks.sql  ← Supabase에 1회 실행
└── server/
    ├── .env.example      ← .env로 복사 후 키 입력
    ├── requirements.txt
    ├── setup.bat         ← Windows 원클릭 설치
    ├── run.bat           ← 서버 시작
    ├── main.py           ← FastAPI 엔트리포인트
    ├── config.py
    ├── ollama_client.py  ← Ollama API 래퍼 (임베딩 + LLM)
    ├── supabase_store.py ← pgvector 어댑터
    ├── chunker.py        ← 텍스트 청킹 로직
    ├── indexer.py        ← 코드 + 문서 + 태스크 인덱싱
    ├── retriever.py      ← top-k 유사도 검색
    └── rag.py            ← 프롬프트 조립 + LLM 응답 스트리밍
```

## 🎯 3-Phase 파이프라인 (구현됨)

### 1차: 코드 위키 생성
- `POST /index/code` — Git 레포 URL을 받아서 모든 파일을 분석
- 파일별 → 500토큰 청크 → `nomic-embed-text` 임베딩 (768d)
- `doc_chunks` 테이블에 `source_type='code'`로 저장
- 옵션: 파일별 요약을 LLM이 생성 → Supabase `wiki_docs`에 `kind='canon'`으로 자동 저장 (TDA에서 바로 열람 가능)

### 2차: 기획 문서 인덱싱
- `POST /index/wiki` — Supabase `wiki_docs` 전체 읽기 → 청크 → 임베딩 → `source_type='wiki'`
- `POST /index/task` — kanban_tasks의 title/desc/details → 청크 → `source_type='task'`
- `POST /index/sprint` — sprints의 goal/checklists → 청크 → `source_type='sprint'`

### 3차: 통합 RAG 답변
- `POST /chat` (SSE 스트리밍) — 질문 임베딩 → 모든 source 통합 top-k 검색
- 컨텍스트 조립 (코드 청크 + 문서 청크 + 태스크 청크 모두 포함)
- Qwen이 답변 생성 + 출처 인용
- 응답은 `data: {"delta": "...", "sources": [...]}` SSE 라인으로 스트림

## ⚙️ 환경 변수 (.env)

```env
# Supabase
SUPABASE_URL=https://xxxxxxxxxxxxxxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJh...   # service_role key (RLS 우회 + bulk insert 필요)

# Ollama
OLLAMA_BASE_URL=http://localhost:11434
LLM_MODEL=qwen2.5-coder:14b
EMBED_MODEL=nomic-embed-text

# 서버
PORT=8000
CORS_ORIGINS=https://namkuri.github.io,http://localhost:3000

# 인덱싱 옵션
CHUNK_SIZE=500          # 토큰
CHUNK_OVERLAP=50
TOP_K=8                 # 검색 결과 개수
GIT_CLONE_DIR=./repos   # Git 레포 clone 디렉토리
```

## 🚀 시작하기

**🔴 처음이신가요?** → `USER_TASKS.md` 먼저 보세요. 단계별 체크리스트 있습니다.

빠른 시작 (이미 Ollama·Python 설치 완료):
```bash
# 1. Supabase에 SQL 실행 (1회)
psql ... -f migrations/001_doc_chunks.sql

# 2. 환경 변수
cd server
copy .env.example .env
notepad .env   # 키 입력

# 3. 설치
.\setup.bat

# 4. 시작
.\run.bat

# 5. (다른 창) Cloudflared 터널
cloudflared tunnel --url http://localhost:8000
```

## 🔌 API 엔드포인트

### `GET /health`
서버 상태 + Ollama 연결 + Supabase 연결 확인.
응답:
```json
{
  "status": "ok",
  "model": "qwen2.5-coder:14b",
  "ollama": "connected",
  "supabase": "connected",
  "chunks_count": 1247,
  "uptime_sec": 3600
}
```

### `POST /chat`
질의응답 — SSE 스트리밍.

요청:
```json
{
  "messages": [
    {"role": "user", "content": "PlayerAttack.cs는 어떻게 동작해?"}
  ],
  "project_id": "proj-1234",
  "model": "qwen2.5-coder:14b",
  "include_tasks": true,
  "stream": true
}
```

응답 (SSE):
```
data: {"delta": "PlayerAttack.cs는"}
data: {"delta": " 캐릭터의 공격을 처리하는"}
data: {"delta": " 컴포넌트입니다..."}
data: {"sources": [{"type": "code", "id": "PlayerAttack.cs", "title": "PlayerAttack.cs"}]}
data: [DONE]
```

### `POST /index/code`
Git 레포 인덱싱 (1차).

요청:
```json
{
  "git_url": "https://github.com/namkuri/tda-dashboard",
  "project_id": "proj-1234",
  "generate_wiki": true,
  "branch": "main"
}
```

진행률 SSE 스트리밍 — `{"progress": 45, "current_file": "src/index.html"}`

### `POST /index/wiki`
Supabase `wiki_docs` 인덱싱 (2차).

요청:
```json
{ "project_id": "proj-1234" }
```

### `POST /index/task`
태스크·스프린트 인덱싱 (2차 확장).

### `DELETE /index/all`
특정 source_type 또는 project_id의 청크 모두 삭제 (재인덱싱 전).

## 🧠 모델 선택 가이드

| 모델 | VRAM (Q5) | 속도 | 권장 케이스 |
|----|----|----|----|
| **Qwen 2.5-Coder 14B** ⭐ | ~10GB | ~25 tok/s | 코드·기술 문서 (DeepWiki 본래 목적) |
| Qwen 2.5 14B-Instruct | ~10GB | ~25 tok/s | 범용 한국어 답변 |
| Qwen 2.5 7B-Instruct | ~6GB | ~50 tok/s | 빠른 응답·동시 사용자 多 |

설정에서 언제든 변경 가능 (모델만 미리 `ollama pull` 해두면 됨).

## 🔒 보안 노트

- **service_role key**: `.env`에만 두고 절대 커밋 금지 (.gitignore에 포함됨)
- **Cloudflare Tunnel**: 기본은 공개 — Cloudflare Access로 이메일 화이트리스트 추가 권장
- **CORS**: GitHub Pages 도메인만 허용 (`.env`의 `CORS_ORIGINS`)
- **Rate limit**: 동시 요청 제한 권장 (asyncio.Semaphore)

## 📝 라이선스 & 출처

- **DeepWiki-Open 아이디어**: https://github.com/AsyncFuncAI/deepwiki-open (MIT)
- 본 코드는 처음부터 작성한 슬림 구현 — DeepWiki-Open의 코드 분석 기법을 참고하되,
  Supabase 통합 + 멀티 소스 인덱싱 + TDA 어댑터에 최적화

## 🆘 더 도움이 필요하면
- TDA Dashboard에서 "🐞 버그 리포트" 메뉴로 제출 (관리자만 검토 가능)
- 또는 직접 코드 수정 — 모든 함수에 한국어 docstring 작성됨
