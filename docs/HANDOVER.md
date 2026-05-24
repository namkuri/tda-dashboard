# TDA Dashboard v40 — Claude Code 핸드오프 문서

> 이 파일은 claude.ai 채팅에서 진행하던 작업을 Claude Code (터미널 CLI)로 이전할 때
> 새 세션에게 컨텍스트를 전달하기 위한 압축본입니다. **처음 세션에서 가장 먼저 읽으세요.**

## 0. 첫 명령 (Claude Code에게)

```
이 리포의 작업을 이어받습니다. 먼저 다음 두 문서를 차례로 읽으세요:
1. docs/HANDOVER.md (이 파일) — 현재 상태와 남은 작업
2. docs/TDA_v40_통합_마스터_보고서.md — 전체 설계와 핵심 결정

읽은 후 "현재 상태 OK, Day 2 청크 4 시작 준비됨" 같은 식으로 보고하세요.
새 코드/파일을 만들기 전에 항상 관련 파일을 view로 먼저 보세요.
```

---

## 1. 프로젝트 한 줄 요약

게임 개발팀(pB-3 COMBAT 모듈, 2-3명)을 위한 단일-HTML Wiki+Kanban 대시보드 v40.
Supabase(인증·DB·Realtime) + GitHub Pages(배포) + Tauri(.msi 데스크톱) 3개 레이어.

## 2. 환경 정보

### 로컬
- 경로: `C:\projects\tda-dashboard`
- OS: Windows 11 (PowerShell)
- Git: HTTPS + PAT (또는 SSH로 전환 가능, 이전 시도했으나 PAT으로 진행 중)
- Node.js 24, Rust 1.95, MSVC Build Tools 설치됨, Tauri CLI 2.11

### 원격
- GitHub 리포: https://github.com/namkuri/tda-dashboard (public)
- 공개 URL: https://namkuri.github.io/tda-dashboard/
- 배포: GitHub Actions가 `public/` 푸시 시 자동 (~1-2분)
- 데스크톱 빌드: 태그 푸시(`v*`)로 `release.yml` 자동 .msi 생성

### Supabase
- Project ref: `xrmfunjekgwhoxtltzrj`
- Dashboard: https://supabase.com/dashboard → COMBINE's Org → namkuri's Project → main
- OAuth callback: `https://xrmfunjekgwhoxtltzrj.supabase.co/auth/v1/callback`
- Site URL: `https://namkuri.github.io/tda-dashboard/`
- Anon key: **이미 `public/index.html`에 박혀 있음**, 매번 교체 불필요
- RLS: LOOSE 모드 (`authenticated all access`). STRICT 모드 SQL은 `docs/tda_v40_supabase_migration.sql` §4-B에 주석으로 있음
- 9개 테이블: projects, kanban_categories, tasks, wiki_docs, task_comments, tag_colors, users (auth 연동), sprints, review_requests
- Realtime publication 모든 테이블 활성화됨

### OAuth
- Google OAuth Client ID: `1056578285190-koqstt8hj79pm0f7531kb96q20kfn829.apps.googleusercontent.com` (project "Center", center-182911)
- JS Origins: `https://xrmfunjekgwhoxtltzrj.supabase.co`, `http://localhost:3000`, `https://namkuri.github.io`
- Redirect URI: `https://xrmfunjekgwhoxtltzrj.supabase.co/auth/v1/callback`
- GitHub OAuth App: 같은 callback URL, namkuri 계정 인증됨
- ⚠️ Tauri 앱에서 OAuth 동작 X — `tda://auth-callback` deep-link 필요 (**Day 3 작업**)

## 3. 마스터 보고서 핵심 결정 (꼭 지킬 것)

`docs/TDA_v40_통합_마스터_보고서.md` §0 — 다음 결정들은 절대 뒤집지 말 것:

1. **Kanban = 3-Zone Attention Model** (Now/Shelf/Buried). 컬럼 늘리지 않음.
2. **Sprint** = Mon-Sun 1주, ISO week 라벨 (`sprint-2026W22` 등). 첫 스프린트 = 5/26~6/1 W22.
3. **Buried 자동 이동** = 완료 후 3일. 이미 구현됨 (`autoBuryOldCompleted`).
4. **Review rule** = 요청당 선택 `{all_agree, majority, specific_approver, force}`. 모든 결정은 audit trail 필수 (rule_used, approvers, rejecters, skippers, timestamps, proposer).
5. **Auth** = Google + GitHub OAuth. Supabase identity linking ON.
6. **Distribution** = Tauri .msi (PWA-only 제외).
7. **Hosting** = GitHub Pages public repo.
8. **Progression** = v40 + Tauri 병렬.
9. **Users** = 2-3명 내부, 코드 사이닝 미적용.
10. **5-aspect tag 시스템 폐기** (문서 분류와 무관 결정). 대신 양방향 task↔doc 링크로 대체 (**Day 3 작업**).

리뷰 시스템은 모든 결정에 적용: intrusion, standard_change, decision(slop type, 캐릭터 역할 등). 결정 시마다 `03_결정 로그` 폴더에 ADR-YYYYMMDD-HHMM-*.md 자동 생성.

## 4. 진행 상황 (2026-05-24 일요일 작업)

### ✅ 완료
- **Day 1 Track A**: OAuth 통합, Supabase 스키마 마이그레이션, 위키 폴더 자동 시드, users 테이블 + 트리거 + RLS
- **Day 1 Track B Phase A**: GitHub 리포 + Pages 자동 배포 + PWA(manifest, sw, 아이콘)
- **Day 1 Track B Phase B**: Tauri scaffold + Rust 환경 + .msi 빌드 + 설치 검증
- **Day 2 청크 1**: Zone 배지 (`badge-zone-*`) + cycleZone 함수 + Zone 우선 정렬 + dbUpsertTask에 v40 필드 추가
- **Day 2 청크 2**: Zone 필터 토글 (Now만 / Now+Shelf기본 / 전체)
- **Day 2 청크 3**: autoBuryOldCompleted (완료 3일 후 자동 매장) + 카테고리 헤더 Zone 카운트 (📌3 📦5 🗑2) + sw.js network-first 전략 (캐시 stale 해결)
- **Day 2 청크 4**: Sprint Lock UI — 칸반 상단 스프린트 띠(활성 스프린트 라벨·기간·목표·intrusion 카운트), 시작/종료(로그인한 누구나 + confirm + 토스트 알림 + `history` audit), ISO week 라벨 자동(`sprint-2026W22`), 활성 스프린트 중 카드 추가 시 intrusion 선택 모달(포함/외부기록), `sprints` 테이블 로드+upsert+`rt-sprints` 실시간 구독
  - ⚠️ **필요 SQL(1회)**: `ALTER TABLE public.sprints ADD COLUMN IF NOT EXISTS history jsonb DEFAULT '[]'::jsonb;` — `docs/tda_v40_supabase_migration.sql` §2-2에 추가됨. Supabase SQL Editor에서 실행해야 시작/종료 히스토리가 DB에 저장됨.
- **Day 3 청크 1**: 양방향 task↔doc 링크 (§5.5) — `[[task:ID]]`/`[[doc:ID]]` 위키링크 자동 양방향 등록 + 클릭 네비, 카드 "📄 연결된 문서" 패널 + 문서 뷰 "🔗 연결" 바(태스크/문서), 피커 모달(검색→클릭 연결). 데이터 갭 수정: `_DOC_META_COLS`에 `linked_*` 추가(미추가 시 저장이 링크를 []로 덮어씀), `dbUpsertDoc`이 `linked_*` 저장, rt-tasks/rt-wiki-docs에 zone/sprint/링크 멀티유저 동기화 보완. **새 SQL 불필요**(컬럼 기존재).

### 🔲 남은 작업

#### Day 3 — 연결 + 리뷰 (진행 중)
- ✅ 청크 1: 양방향 task↔doc 링크 (완료)
- 청크 2: Review request 시스템 — 데이터 + 생성 모달(4룰: all_agree/majority/specific_approver/force) + 투표 UI(approve/reject/skip) + 알림 뱃지
- 청크 3: 결정 시 ADR 자동 생성(`03_결정 로그`) + intrusion 정식 연결(Shelf→Now lock, §1.3) — 청크 4의 경량 모달 교체
- 청크 4: Tauri `tauri-plugin-deep-link` 적용 (`tda://auth-callback` 스킴) — Tauri 앱에서 OAuth 작동
- 정렬 갭(교차검증): 수동 Bury 사유 필수(§1.5), 스프린트 종료 이월(§1.4, Day 4)

#### Day 4 — 자동화 + 마무리
- Sprint 종료 시 회고 자동 생성 (완료 카드 통계, intrusion 횟수, carryover 목록)
- Tauri updater plugin 적용 (자동 업데이트, 단 pubkey 생성 필요)
- 모바일 검증 (iOS PWA 설치 테스트)
- 청크별 최종 통합 테스트

## 5. 기술 메모

### 코드 구조
- **단일 HTML 파일**: `public/index.html` (약 10,100줄). 자체 완결. CDN으로 Tailwind/DOMPurify/Supabase JS 등 로드.
- **분기 표시**: 청크별로 `[v40 Day2 청크 N]` 주석 달려 있음. 이를 grep하면 작업 범위 한눈에 보임.

### 매번 푸시 흐름
```powershell
cd C:\projects\tda-dashboard
# 코드 수정 (직접 편집 가능)
git add public/index.html
git commit -m "Day X 청크 Y: 무엇무엇"
git push
# 1-2분 후 https://namkuri.github.io/tda-dashboard/ 자동 갱신
```

### 알려진 함정
- **앵카키(anon key) 따옴표 보존**: HTML 안에 `const SUPABASE_ANON_KEY_DEFAULT = "..."`. 따옴표 깨지면 SyntaxError → 전체 로딩 실패.
- **Service Worker 캐시**: 옛 sw.js는 cache-first였음. 새 sw.js (v40-002)는 HTML/JS는 network-first, 아이콘만 cache-first. 캐시 stale 문제 해결됨.
- **Multiple GoTrueClient 경고**: `ensureSupabaseClient()`에서만 `createClient()` 1회 호출. `initSupabase()`는 재사용. 절대 두 군데서 호출하지 말 것.
- **Realtime CLOSED 재호출 루프**: 이전엔 5초 후 자동 재호출이 무한 루프 유발. 현재 자동 재호출 비활성. visibilitychange 이벤트가 재연결 담당.
- **`_initSupabaseInProgress` 가드**: SIGNED_IN 이벤트가 페이지 로드 시 2회 발화하는 문제 대응. 중복 진입 방지.

### 검증 환경
- 브라우저: Chrome (시크릿 창 권장, 캐시 영향 없음)
- 콘솔 정상 로그: `[v40 auth event] INITIAL_SESSION ...`, `[v40 seed] ...`, `[rt-wiki-docs] 연결됨`
- 콘솔 무해 경고: `manifest.json 404`(나중에 해결됨), `apple-mobile-web-app-capable deprecated`
- Tauri: `npm run tauri:dev` (10분 첫 빌드, 이후 빠름), `.msi`는 OAuth Day 3 적용 전까진 404 페이지 (정상)

## 6. 사용자 정보

- GitHub: namkuri (rnk505@gmail.com)
- 닉네임: nam kyu ryu (Google OAuth 인식됨)
- 팀 규모: 2-3명 내부
- 작업 스타일: 결정 빠름, 한국어 소통, 코드 검증 후 진행, 위계적 보고 → 실행 흐름 선호
- 컨텍스트: pB-3 COMBAT 게임 개발팀, 마스터 보고서를 첫 위클리 세션 안건으로 활용

## 7. 다음 세션 첫 발화 권장 예시

```
docs/HANDOVER.md와 docs/TDA_v40_통합_마스터_보고서.md를 읽었습니다.

현재 상태: Day 1 + Day 2 청크 1-3 완료. GitHub Pages 배포 + .msi 빌드 검증 완료.
다음: Day 2 청크 4 (Sprint Lock UI) 시작.

작업 흐름 확인:
1. public/index.html을 직접 수정
2. git add/commit/push
3. ~1-2분 후 https://namkuri.github.io/tda-dashboard/ 갱신
4. 검증 후 다음 청크

청크 4 작업은 다음 4가지를 구현합니다:
A. 활성 스프린트 표시 (헤더 우측 또는 상단 띠)
B. 스프린트 시작/종료 버튼 + ISO week 라벨 자동
C. Intrusion 시 다이얼로그 (마스터 보고서 §0.4 audit trail 형식 준수)
D. sprints 테이블 CRUD (이미 schema 존재)

구현 시작해도 될까요?
```

---

*문서 생성: 2026-05-24 일요일 늦은 저녁. 작성자: claude.ai 세션.*
