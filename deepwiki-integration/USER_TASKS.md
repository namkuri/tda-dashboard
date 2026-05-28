# 🔧 사용자 작업 체크리스트 — Deep Wiki 백엔드 설정

> **목표**: 내 PC(RTX 5060Ti)에서 FastAPI + Ollama(Qwen 2.5-Coder 14B) + Supabase pgvector를 띄워서
> 어디서나 TDA Dashboard 앱이 RAG 챗봇으로 활용 가능하게 만들기

> **총 소요시간**: 약 30분 액션 + 약 15~20분 모델 다운로드 대기

---

## ✅ 체크박스 — 순서대로 진행

### [ ] 0. 사전 점검
- Windows 10/11 ✓
- RTX 5060Ti(16GB VRAM) ✓
- 5060Ti 드라이버 최신: `nvidia-smi` 명령으로 확인
- 디스크 여유 30GB 이상 (모델 9GB + Python venv + 로그)
- 인터넷 회선 (모델 다운로드용)

---

### [ ] 1. Ollama 설치 (3분)
```powershell
winget install Ollama.Ollama
```
또는 https://ollama.com/download 에서 OllamaSetup.exe 다운로드 → 실행

설치 확인:
```powershell
ollama --version
```
설치 후 시스템 트레이에 🦙 아이콘 보임. 백그라운드 자동 실행 (`localhost:11434`).

---

### [ ] 2. Qwen 모델 + 임베딩 모델 pull (15분, ~9GB)
```powershell
ollama pull qwen2.5-coder:14b
ollama pull nomic-embed-text
```

진행률이 100%까지 가는 거 확인. (한 번 받으면 영구 캐시)

확인:
```powershell
ollama list
```
→ `qwen2.5-coder:14b` 과 `nomic-embed-text` 둘 다 보여야 함.

---

### [ ] 3. Python 3.11+ 설치 확인 (1분)
```powershell
python --version
```
3.11 미만이면 https://www.python.org/downloads/ 에서 3.11 또는 3.12 설치 (Add to PATH 체크 잊지 마세요).

---

### [ ] 4. Supabase Service Role Key 복사 (1분)
1. https://supabase.com/dashboard 로그인
2. 본인 프로젝트 선택
3. 좌측 메뉴 **Project Settings** → **API**
4. **Project API keys** 섹션에서 `service_role` (secret) 키 복사
   - ⚠️ 이건 anon key가 아닙니다. 비밀로 유지.
   - 인덱싱 시 RLS 우회 + 대량 삽입에 필요

또한 다음 두 정보도 메모:
- **Project URL**: `https://xxxxxxxxxxxxxxxx.supabase.co`
- **anon key**: (이미 TDA 앱에 입력된 것과 동일)

---

### [ ] 5. Supabase pgvector + 테이블 생성 (1분)
1. Supabase Dashboard → **SQL Editor**
2. New query
3. `migrations/001_doc_chunks.sql` 파일 전체 복사 → 붙여넣기 → **Run**
4. 성공 메시지 확인. 좌측 Table Editor에서 `doc_chunks` 테이블 생성 확인.

---

### [ ] 6. `.env` 파일 생성 (2분)
1. `deepwiki-integration/server/` 폴더로 이동
2. `.env.example` 파일 복사하여 `.env` 로 이름 변경
3. 메모장으로 열어서 다음 값 채우기:
```env
SUPABASE_URL=https://xxxxxxxxxxxxxxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJh... (4번에서 복사한 service_role key)
OLLAMA_BASE_URL=http://localhost:11434
LLM_MODEL=qwen2.5-coder:14b
EMBED_MODEL=nomic-embed-text
PORT=8000
```

---

### [ ] 7. setup.bat 실행 (5분)
탐색기에서 `deepwiki-integration/server/setup.bat` 더블클릭
또는 PowerShell에서:
```powershell
cd C:\projects\tda-dashboard\deepwiki-integration\server
.\setup.bat
```
다음 작업이 자동 수행됨:
- Python 가상환경 생성 (`.venv`)
- pip install -r requirements.txt
- 종속성 확인

"✅ 설치 완료" 메시지 확인.

---

### [ ] 8. run.bat 실행 (서버 시작)
탐색기에서 `run.bat` 더블클릭, 또는:
```powershell
.\run.bat
```
콘솔에 다음 메시지가 나오면 성공:
```
INFO:     Uvicorn running on http://0.0.0.0:8000
INFO:     Application startup complete.
🟢 Ollama 연결 OK · qwen2.5-coder:14b
🟢 Supabase 연결 OK · doc_chunks: 0 chunks
```
이 창을 계속 켜두세요 (서버 작동 중).

**테스트**: 다른 브라우저 탭에서 http://localhost:8000/health 접속 → JSON 응답이 보이면 OK

---

### [ ] 9. Cloudflare 가입 + cloudflared 설치 (5분)
1. https://dash.cloudflare.com/sign-up — 무료 가입 (카드 등록 불필요)
2. cloudflared 설치:
```powershell
winget install --id Cloudflare.cloudflared
```
확인:
```powershell
cloudflared --version
```

---

### [ ] 10. Quick Tunnel 생성 (1분, 임시 URL)
가장 간단한 방법 — 임시 URL이지만 즉시 동작:
```powershell
cloudflared tunnel --url http://localhost:8000
```
1~2초 후 다음 라인이 콘솔에 나옴:
```
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://random-words-here.trycloudflare.com                                                |
+--------------------------------------------------------------------------------------------+
```
**이 URL을 복사**. 이 창도 켜두세요.

> 💡 영구 URL이 필요하면 https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/ 참고 (Named Tunnel + 본인 도메인)

---

### [ ] 11. TDA 앱에 URL 입력
1. TDA Dashboard → 사이드바 🤖 **Deep Wiki**
2. 우측 상단 **⚙️ 설정**
3. **백엔드 URL**에 10번에서 복사한 URL 붙여넣기 (`/health` 같은 경로는 안 붙임)
4. 🔌 **연결 테스트** 클릭
5. **🟢 연결됨 · qwen2.5-coder:14b** 메시지 확인
6. **저장**

---

### [ ] 12. 인덱싱 트리거
1. Deep Wiki 화면 → **⚙️ 설정** 다시 열기
2. **Git 레포 URL**에 코드 분석할 레포 입력 (예: `https://github.com/namkuri/tda-dashboard`)
3. **🗂 코드 인덱싱 시작** 클릭 → 5~30분 (레포 크기에 따라)
4. **📚 우리 문서 인덱싱** 클릭 → wiki_docs + 태스크 + 스프린트 임베딩 (1~5분)
5. 완료되면 채팅 입력 사용 가능

**테스트 질문**:
- "전투 시스템 관련 카드가 뭐가 있어?"
- "PlayerCharacter 프리팹 작성 가이드 알려줘"
- "이번 스프린트 목표를 한 줄로 요약해줘"

---

## 🛟 문제가 생길 때

| 증상 | 해결책 |
|----|----|
| `ollama: command not found` | PowerShell 새 창 열기 (PATH 갱신) |
| 모델 pull 진행률이 멈춤 | 인터넷 끊김 — 다시 `ollama pull ...` (이어받기) |
| `setup.bat` 도중 pip 에러 | Python 3.11+ 인지 확인, 또는 `pip install --upgrade pip` |
| `run.bat` 도중 "port already in use" | 다른 프로세스가 8000 사용 중 — `.env`의 `PORT=8001`로 변경 |
| `connection refused to ollama` | Ollama가 안 떠 있음 — 시스템 트레이 확인, 또는 `ollama serve` 수동 실행 |
| Supabase 연결 실패 | service_role 키 다시 확인 (anon key 아님!) |
| `cloudflared` 터널 URL 변경됨 | Quick Tunnel은 재시작 시 URL 바뀜 — Named Tunnel로 업그레이드하면 영구 |
| 인덱싱이 너무 느림 | Qwen 14B → 7B로 변경 (TDA 설정에서) — VRAM 부담 ↓, 속도 ↑ |

---

## 📦 운영 팁

- **PC가 켜져 있어야 서비스됨** — 자주 쓸 거면 절전 모드 끄기 (`powercfg /change standby-timeout-ac 0`)
- **팀원 사용**: 본인 PC URL을 팀에게 공유 (Cloudflare Access로 이메일 화이트리스트 추가하면 더 안전)
- **재인덱싱**: 코드/문서가 많이 바뀌면 한 번씩 다시 트리거. 기존 청크는 자동 갱신.
- **GPU 활용도 확인**: `nvidia-smi` 또는 작업 관리자에서 모니터링
