# TDA Dashboard v40 통합 마스터 보고서

> **작성일**: 2026-05-24 (일)
> **대상 버전**: v39 → v40
> **착수일**: 2026-05-26 (월, 5/26)
> **목표 완성일**: 2026-05-29 (목, 5/29)
> **이 문서의 위치**: `05_운영 / 위클리 세션 / 2026-W22.md` (첫 위클리 세션 문서로 활용)
> **선행 문서**: `TDA_v40_방향성_보고서.md` (이 문서가 흡수·대체)

---

## 0. 결정 사항 잠금 (Final Decisions)

모든 결정은 5/24까지의 협의 결과이며, 변경 시 본 문서의 ADR 첨부 + 팀 리뷰가 필요.

| # | 영역 | 결정 | 비고 |
|---|---|---|---|
| 1 | 칸반 모델 | **3-Zone Attention Model** (Now/Shelf/Buried) | 컬럼 확장 모든 안 폐기 |
| 2 | 스프린트 주기 | **월–일 1주** | ISO 주차 표기 (sprint-2026W22 등) |
| 3 | Buried 자동 이동 | **done 후 3일** | 공격적, 보드 가벼움 |
| 4 | 리뷰 의사결정 룰 | **요청별 선택** (4종) + 완전 audit trail | 일반 의사결정 메커니즘으로 격상 |
| 5 | 인증 | **Google + GitHub OAuth** (Supabase) | identity linking ON |
| 6 | 배포 방식 | **Tauri 직행** (.msi 인스톨러) | PWA는 브라우저 폴백으로 유지 |
| 7 | 호스팅 | **GitHub Pages** (공개 리포) | Supabase RLS 필수 |
| 8 | 진행 방식 | **v40 + 배포 병렬** (Track A / Track B) | 4일 안에 양쪽 동시 |
| 9 | 사용자 규모 | **팀 2~3명 내부용** | 코드 사이닝 불필요 |
| 10 | 5측면 태그 | **제거** | 양방향 링크로 대체 |

---

## 1. 칸반 — 3-Zone Attention Model

### 1.1 문제 재정의

v39 칸반의 본질적 문제는 **"컬럼이 부족"이 아니라 "한 화면에 다 보임"**이다. 회의록의 "쌓이지 않게" · "치고 들어오는 일 방지" · "처낼 것 처내기" 3가지 요구는 **덜 보여주고 명시적으로 묻는** 방향을 가리킨다.

### 1.2 세 구역의 역할

#### 🎯 Now (현재 스프린트, 잠금)
- 화면 기본 표시. 카드 5~15개로 의도적 제한 (초과 시 경고).
- 내부 상태 3단: `todo / doing / done`.
- 스프린트 활성 시 **잠금**: 신규 태스크 자동 진입 불가.
- 상단에 스프린트 목표 1줄 + 진척률 게이지 고정.
- 카테고리(combat sub-module 등) 그룹핑은 이 구역 안에서만.

#### 📚 Shelf (선반)
- 새 태스크의 **기본 도착지**. 모든 "치고 들어오는" 요청이 여기서 멈춤.
- 기본 접힘, 클릭으로 펼침.
- 정렬: 카테고리 / 우선순위 / 추가일.
- "다음 스프린트 후보" 마킹 가능.
- Shelf → Now 이동은 **스프린트 플래닝 모달에서만** 허용.

#### 🗑 Buried (묻힘)
- "쳐낸 것"의 영구 보관소.
- 진입 시 **거절 사유 필수 입력**.
- 평소 숨김. 검색으로만 접근.
- "되살리기" 가능, 이력 누적 (몇 번 되살아났는지).
- **done 후 3일 경과 시 자동 이동** (스프린트 종료와 무관, 개별 카드 기준).

### 1.3 핵심 메커니즘: Sprint Lock

스프린트 활성 시 Now에 카드 추가는 **"끼어들기(intrusion)" 요청 시스템**을 거쳐야 한다. 사유 없는 직접 추가는 막힌다. 끼어들기 요청은 §3 리뷰 시스템을 통해 처리된다.

### 1.4 자동 흐름 규칙

| 트리거 | 이동 | 비고 |
|---|---|---|
| 신규 태스크 생성 | → Shelf | 기본값, 변경 불가 |
| 스프린트 플래닝 | Shelf → Now | 플래닝 모달에서만 |
| done 처리 후 3일 경과 | Now → Buried | 자동, 개별 카드 기준 |
| 스프린트 종료 (미완료 카드) | Now → Shelf | 자동, 이월 횟수 +1 |
| 3회 이상 이월 | Shelf 내 마킹 | "정말 할 건가?" 표시 |
| Bury 액션 (수동) | Shelf/Now → Buried | 사유 필수 |
| 끼어들기 승인 | Shelf → Now | §3 리뷰 통과 시 |

### 1.5 "처내기"를 1급 액션으로

카드 우상단 `🗑` 버튼 → 사유 입력 모달 → Buried. 사유 없이는 불가. 회의록의 "처낼 것 처내기"가 명확한 클릭 동작으로 존재.

### 1.6 모바일 가독성

3-Zone은 **모바일에서 오히려 더 잘 작동**한다. 기본 화면이 Now만 표시. 상단 세그먼트 컨트롤 `[Now | Shelf | Buried]`로 전환 (iOS 네이티브 패턴).

---

## 2. 스프린트 운영

### 2.1 주기: 월–일 1주

- **첫 스프린트 시작**: 5/26 (월) = sprint-2026W22 (5/26 ~ 6/1)
- ISO 주차 표기 통일.
- 회의록의 "위클리 세션" 요구와 정렬.

### 2.2 Sprint 객체 구조

```javascript
{
  id: 'sprint-2026W22',
  weekLabel: '5/26~6/1 (W22)',
  goal: '한 줄짜리 핵심 목표',          // 예: "공격-피격 루프 최소구현"
  milestoneCriteria: [                   // 최소구현 체크리스트 3~5개
    '플레이어 공격 입력 → 적 hit 판정',
    '피격 시 HP 차감 + 피드백',
    '적 사망 처리'
  ],
  status: 'planning' | 'active' | 'review' | 'closed',
  intrusionCount: 0,                     // 끼어들기 시도 횟수
  carryoverFromPrevious: [],             // 이월 task ID
  startDate, endDate, projectId
}
```

### 2.3 위클리 운영 사이클

| 시점 | 자동 동작 | 사람 행동 |
|---|---|---|
| 월요일 오전 | 위클리 세션 문서 자동 생성 | 함께 작성 → 스프린트 잠금 |
| 수요일 | "3분 셀프 체크" 토스트 | Now 진척률 + 차단요인 1줄 |
| 금요일 저녁 | 회고 문서 자동 생성 | 완료/미완/이월/Bury/끼어들기 통계 검토 |
| 다음 월요일 | 직전 스프린트 자동 closed → 새 스프린트 객체 | 다음 주 위클리 세션 |

---

## 3. 리뷰 시스템 (의사결정 일반 메커니즘)

### 3.1 설계 원칙

회의록의 "끼어들기"는 좁은 요구지만, 같은 구조를 일반화하면 **모든 팀 의사결정**(코딩 스탠다드 변경, 슬롭류 결정, 캐릭터 역할 결정 등)에 재사용 가능. 따라서 별도 기능이 아니라 **일반 메커니즘**으로 설계.

### 3.2 4가지 룰 (요청별 선택)

| 룰 | 동작 | 권장 시나리오 |
|---|---|---|
| 🌐 **전체 합의 (all_agree)** | 모든 팀원 승인 필요. 1명 거부 = 거부 | 코딩 스탠다드 변경, 프로젝트 구조 변경 |
| 📊 **다수결 (majority)** | 과반수 승인 + 거부 1명 = 즉시 거부 | 일반 끼어들기 |
| 👤 **특정인 승인 (specific_approver)** | 지정된 1명 이상 승인 시 통과 | 팀장 결재 명확 사안 |
| ⚡ **강제 (force)** | 리뷰 없이 즉시 통과 (사유 필수) | 긴급 핫픽스 |

제안자가 요청 생성 시 룰 선택. 룰 자체도 audit trail에 기록됨 (사후 "왜 그 룰을 썼나" 추적 가능).

### 3.3 데이터 구조

```javascript
reviewRequests[] = {
  id,
  type: 'intrusion' | 'standard_change' | 'decision',  // 재사용 가능

  rule: {
    type: 'all_agree' | 'majority' | 'specific_approver' | 'force',
    requiredApprovers: ['user_id_1', ...],   // specific_approver일 때
    minApprovals: 1                          // majority 임계값 (기본 과반수)
  },

  targetTaskId,        // 끼어들기 대상
  targetDocId,         // 문서 변경 대상
  sprintId,
  proposerId,
  proposerName,        // 캐시 (UI 표시용)
  reason,              // 필수
  createdAt,
  expiresAt,           // 기본 24시간

  votes: [{
    userId,
    userName,          // 캐시
    decision: 'approve' | 'reject' | 'skip',
    comment,           // 선택
    votedAt
  }],

  // === 필수 태깅 (audit trail) ===
  status: 'pending' | 'approved' | 'rejected' | 'expired' | 'forced',
  decidedAt,
  decisionTag: {
    rule_used: 'majority',
    approvers: [{id, name, comment}],
    rejecters: [{id, name, comment}],
    skippers:  [{id, name}],
    final_decision: 'approved',
    decided_at: '2026-05-27T14:30:00+09:00',
    proposer:  {id, name, reason}
  }
}
```

### 3.4 UI 흐름

**요청 생성 모달**:
1. 사유 입력 (필수)
2. 룰 선택 드롭다운 (4종)
3. 룰별 추가 입력:
   - specific_approver → 사용자 선택 UI
   - majority → 최소 승인 수 (기본: 응답자 과반)
4. 요청 생성

**카드 표시 (진행 중)**:
```
🔔 리뷰 중 · 룰: 다수결 (2/3 응답)
   제안자: 김OO · "긴급 버그 수정 필요"
   [내 투표 ▼]
```

**카드 표시 (결정 후)**:
```
✅ 승인 · 다수결 · 5/27 14:30
   승인: 김OO · 이OO  |  거부: 없음  |  불참: 박OO
```

### 3.5 자동 ADR 생성

모든 결정은 `03_결정 로그` 폴더에 자동 마크다운 문서 생성:

```
파일명: ADR-20260527-1430-끼어들기_긴급버그수정.md

# ADR-20260527-1430: 끼어들기 — 긴급 버그 수정

## 요청 정보
- 제안자: 김OO
- 요청 시각: 2026-05-27 14:15
- 사유: 사용자 보고 받은 크래시 버그 즉시 수정 필요

## 룰
- 선택된 룰: **다수결 (majority)**
- 임계값: 과반수 (2/3)

## 결정
- 최종 결정: **승인**
- 결정 시각: 2026-05-27 14:30 (15분 만에 결정됨)

## 투표 결과
| 팀원 | 결정 | 코멘트 |
|---|---|---|
| 김OO (제안자) | — | — |
| 이OO | ✅ 승인 | "재현 확인함, 진행" |
| 박OO | ⏭ 불참 | (응답 없음) |

## 영향
- 대상 카드: [[task:t-1762341098]] "크래시 버그 수정"
- 스프린트: sprint-2026W22
- Now 카드 수: 8 → 9
```

영구 보존, 검색 가능, 추후 패턴 분석 가능 ("우리 팀은 어떤 끼어들기를 자주 받는가").

### 3.6 재사용 사례

| 사용 시나리오 | type | 권장 룰 |
|---|---|---|
| 끼어들기 요청 | `intrusion` | majority |
| 코딩 스탠다드 변경 | `standard_change` | all_agree |
| 네이밍 컨벤션 변경 | `standard_change` | all_agree |
| 슬롭류 차용 결정 | `decision` | majority 또는 specific_approver |
| 캐릭터 역할 결정 (누적 vs 일시) | `decision` | majority |
| 카테고리 통폐합 | `decision` | all_agree |
| 긴급 핫픽스 | `intrusion` | force (사후 회고에서 검토) |

회의록의 **모든 큰 의사결정**이 하나의 시스템에 누적됨.

---

## 4. 인증 (Google + GitHub OAuth)

### 4.1 Supabase 설정

1. **Google Cloud Console**: OAuth 2.0 Client ID 발급 (Web Application)
   - Authorized redirect URIs: `https://<supabase-project>.supabase.co/auth/v1/callback`
2. **GitHub Developer Settings**: OAuth App 생성
   - Authorization callback URL: 위와 동일
3. **Supabase 대시보드 → Authentication → Providers**: Google, GitHub 둘 다 활성화 + 발급받은 Client ID/Secret 입력
4. **Identity linking 활성화**: Authentication → Settings → "Link identities" ON
   - 같은 이메일이면 Google/GitHub 어느 쪽으로 로그인해도 동일 사용자로 인식

### 4.2 코드

```javascript
// 로그인 UI (모달 또는 페이지)
<button onclick="signIn('google')">🔵 Google로 로그인</button>
<button onclick="signIn('github')">⚫ GitHub로 로그인</button>

async function signIn(provider) {
  const { data, error } = await supabaseClient.auth.signInWithOAuth({
    provider,
    options: { redirectTo: window.location.origin }
  });
  if (error) showToast('로그인 실패: ' + error.message, 'error');
}

// 현재 사용자 확인 (앱 시작 시)
const { data: { user } } = await supabaseClient.auth.getUser();
if (!user) showLoginModal();

// 로그아웃
await supabaseClient.auth.signOut();

// 세션 변화 구독
supabaseClient.auth.onAuthStateChange((event, session) => {
  if (event === 'SIGNED_IN') updateUIWithUser(session.user);
  if (event === 'SIGNED_OUT') showLoginModal();
});
```

### 4.3 Tauri 데스크톱에서의 OAuth 처리

Tauri는 시스템 브라우저로 OAuth를 거치고 callback을 받는 두 가지 방식:

**방식 A: Deep-link (권장)**
- `tauri-plugin-deep-link` v2 사용
- 커스텀 프로토콜 등록: `tda://auth-callback`
- Google/GitHub OAuth 콜백 URL을 `tda://auth-callback`으로 설정
- 시스템이 콜백을 앱으로 라우팅 → 세션 토큰 추출 → Supabase 클라이언트에 저장

**방식 B: Localhost 임시 서버**
- Tauri 내장 HTTP 서버로 임시 포트 listening
- 콜백 URL을 `http://localhost:<port>/callback`로 설정
- 콜백 받으면 토큰 추출 후 서버 종료
- 포트 충돌 가능성 있음

권장: 방식 A. 한 번 설정으로 영구 작동.

### 4.4 RLS 자동 해결

리포 공개 시 Supabase URL과 anon key 노출됨 (정상). 하지만 **인증된 사용자만 데이터 접근**하도록 RLS 룰 설정:

```sql
-- 예: tasks 테이블
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allowed team members only"
  ON tasks FOR ALL
  USING (auth.uid() IN (
    SELECT id FROM auth.users
    WHERE email IN ('member1@gmail.com', 'member2@gmail.com', 'member3@gmail.com')
  ));
```

또는 별도 `allowed_users` 테이블 만들어 관리.

### 4.5 부가 효과

- 코멘트·카드 작성자 자동 표시 (현재 v39는 익명)
- `header-online-count`가 실제 누구인지 표시 가능
- 리뷰 시스템 투표자 신원 명확
- 카드 변경 이력 추적 가능 (누가 언제 무엇을 바꿨는가)

---

## 5. 위키 문서 시스템

### 5.1 5종 문서 유형 (누적 방식 기준)

| 유형 | 누적 방식 | 예시 |
|---|---|---|
| 📐 **기획 (plan)** | 진화함 · 버전 표시 | 비전, 디자인 필러, 캐릭터·스토리 |
| ⚙️ **기술 (tech)** | 안정적 · 변경 시 ADR 강제 | 아키텍처, 코딩 스탠다드, 네이밍 컨벤션 |
| 🧭 **결정 (decision/ADR)** | 순수 누적 · 시간순 | 슬롭류 결정, 캐릭터 역할 결정, 끼어들기 결정 (자동 생성) |
| 🎨 **레퍼런스 (reference)** | 누적 · 이미지+감정 슬롯 | 하이라이트 장면, 감정 방향성 |
| 🗓 **운영 (ops)** | 시간순 누적 · 자동 생성 가능 | 회의록, 위클리 세션, 회고 |

### 5.2 폴더 자동 시드 구조

```
📐 01_기획 (Plan)
   ├ 비전·디자인 필러
   ├ 캐릭터·스토리
   └ 게임 시스템

⚙️ 02_기술 (Tech)
   ├ 아키텍처
   ├ 코딩 스탠다드 ★잠금
   ├ 네이밍 컨벤션 ★잠금
   └ API·데이터 스키마

🧭 03_결정 로그 (ADR)
   └ YYYYMMDD-HHMM-제목 형식, 시간순 자동 정렬

🎨 04_레퍼런스 (Ref)
   ├ 하이라이트 장면
   └ 감정 방향성

🗓 05_운영 (Ops)
   ├ 회의록
   ├ 위클리 세션
   └ 스프린트 회고
```

기존 v39 문서는 손대지 않음. 빈 폴더만 추가, 미분류 문서는 `01_기획` 기본값.

### 5.3 누적성 시각화

사이드바 각 유형 옆에 **"최근 7일 추가 N개"** 배지. ADR/Ref/Ops는 자동 시간순(새 항목 위). Plan/Tech는 명시적 버전 + 변경 이력 패널.

### 5.4 기술 문서 잠금 메커니즘

`02_기술` 하위 코딩 스탠다드 · 네이밍 컨벤션 문서는 **잠금 토글** 기본 ON:
1. 수정 시도 → 모달: "이 문서는 잠금 상태입니다"
2. "변경 요청" 버튼 → §3 리뷰 시스템으로 `type: 'standard_change'` 요청 생성
3. 권장 룰: `all_agree` (전체 합의)
4. 승인 시 수정 가능 + ADR 자동 생성

회의록의 "함부로 못 바꿈" 강제 실현.

### 5.5 양방향 링크

5측면 태그 제거 후 cross-cutting은 **직접 링크**로 처리:
- 카드 ↔ 문서: `linkedDocIds`, `linkedTaskIds` 필드 양방향 동기화
- 위키링크 문법: `[[doc:ID]]`, `[[task:ID]]` 입력 시 자동 양방향 등록
- 카드 하단 "📄 연결된 문서" 패널
- 문서 사이드 "🔗 연결된 태스크" 패널

### 5.6 레퍼런스 카드 전용 레이아웃 (감정 방향성)

`docType='reference'` 문서는 일반 마크다운이 아닌 전용 레이아웃:

```
┌─────────────────────────────────────┐
│ [이미지/GIF 슬롯]                    │  ← 기존 GIF picker 재활용
├─────────────────────────────────────┤
│ 장면 설명 (마크다운)                 │
├─────────────────────────────────────┤
│ 💭 이 장면이 주는 감정              │  ← 전용 필드
│    (예: "긴장감 속 잠깐의 안도")    │
├─────────────────────────────────────┤
│ 🎯 개발 방향성에 주는 시사점        │  ← 전용 필드
│    (예: "피격 후 0.3초 슬로우 모션") │
├─────────────────────────────────────┤
│ 🔗 연관 태스크 [자동 표시]          │
└─────────────────────────────────────┘
```

회의록의 "느껴지는 감정적인 부분에서의 방향성"이 명시되는 자리.

### 5.7 ADR 템플릿

수동 작성용 ADR (리뷰 시스템 통과 외 결정용):

```markdown
# ADR-YYYYMMDD-HHMM: [제목]

## 맥락 (Context)
무엇이 결정을 강제했는가? 어떤 상황인가?

## 선택지 (Options)
- A: ...
- B: ...
- C: ...

## 결정 (Decision)
선택: B
근거:
- ...

## 영향 (Consequences)
- 긍정: ...
- 부정: ...
- 연관 태스크: [[task:t-xxx]]
- 연관 문서: [[doc:d-xxx]]

## 되돌릴 조건 (Reversal Trigger)
다음 조건이 발생하면 이 결정을 재검토:
- ...
```

---

## 6. 데이터 모델 변경 (v39 → v40)

### 6.1 docs[]

```diff
{
  id, projectId, title, content, parentId, sortOrder,
+ docType: 'plan' | 'tech' | 'decision' | 'reference' | 'ops',
+ linkedTaskIds: [],
+ linkedDocIds: [],
+ isLocked: boolean,
+ refMeta: {                       // docType='reference'일 때만
+   imageUrl: '',
+   emotion: '',
+   directionImplication: ''
+ },
+ createdBy: userId,               // OAuth 사용자 ID
+ updatedBy: userId,
+ updateHistory: []
}
```

### 6.2 tasks[]

```diff
{
  id, catId, priority, dev, catBadge, title, desc, details, scripts,
  status, isStarred, time, isCollapsed, comments,
+ sprintId: 'sprint-2026W22' | null,
+ zone: 'now' | 'shelf' | 'buried',
+ buryReason: '',
+ buryHistory: [{date, reason, revivedAt, byUserId}],
+ carryoverCount: 0,
+ doneAt: timestamp,                  // Buried 자동 이동 계산용
+ linkedDocIds: [],
+ createdBy: userId,
+ assignees: [userId, ...]
}
```

### 6.3 새 객체: sprints[]

```javascript
{
  id, weekLabel, goal, milestoneCriteria[],
  status: 'planning' | 'active' | 'review' | 'closed',
  intrusionCount, intrusionLog[],
  startDate, endDate, projectId
}
```

### 6.4 새 객체: reviewRequests[]

§3.3 참조.

### 6.5 새 객체: users[] (Supabase auth와 연동)

```javascript
{
  id,                  // Supabase auth.uid()
  email,
  displayName,
  avatarUrl,
  provider: 'google' | 'github',
  role: 'admin' | 'member',
  joinedAt
}
```

### 6.6 무파괴 마이그레이션

```javascript
v39 데이터 로드 시:
  if (!task.zone) {
    task.zone = task.status === 'completed' ? 'buried'
              : task.status === 'progress'  ? 'now'
              : 'shelf';
  }
  task.sprintId = null;   // 첫 스프린트 플래닝에서 수동 할당
  task.doneAt = task.status === 'completed' ? (task.time || Date.now()) : null;
```

기존 데이터 깨지지 않음.

---

## 7. 배포 인프라 (Tauri + GitHub Pages)

### 7.1 단일 소스, 두 산출물

같은 HTML이 두 채널로 배포:
- **GitHub Pages**: 브라우저 접속 (PWA 설치 가능)
- **Tauri .msi**: Windows 데스크톱 앱 (Obsidian/Claude 방식)

### 7.2 리포 구조

```
tda-dashboard/                       (GitHub 공개 리포)
├── public/                          ← GitHub Pages 서빙 대상
│   ├── index.html                   ← v40 HTML (단일 소스)
│   ├── manifest.json                ← PWA 폴백
│   ├── sw.js
│   ├── icon-192.png
│   └── icon-512.png
├── src-tauri/                       ← Tauri 래퍼
│   ├── tauri.conf.json              ← frontendDist: "../public"
│   ├── Cargo.toml
│   ├── src/main.rs                  ← 거의 빈 파일 (창만 띄움)
│   └── icons/icon.ico
├── .github/workflows/
│   ├── deploy-pages.yml             ← public/ 변경 시 자동 배포
│   └── release.yml                  ← 태그 푸시 시 .msi 자동 빌드
├── docs/                            ← 본 보고서 등 문서 (선택)
│   └── v40-master-plan.md
└── README.md
```

### 7.3 tauri.conf.json 핵심 설정

```json
{
  "build": {
    "frontendDist": "../public",
    "devUrl": null
  },
  "bundle": {
    "active": true,
    "targets": ["msi"],
    "identifier": "com.tda.dashboard",
    "icon": ["src-tauri/icons/icon.ico"]
  },
  "app": {
    "windows": [{
      "title": "TDA Dashboard",
      "width": 1400, "height": 900,
      "minWidth": 800, "minHeight": 600,
      "decorations": true
    }]
  },
  "plugins": {
    "updater": {
      "endpoints": ["https://<username>.github.io/tda-dashboard/latest.json"],
      "pubkey": "<공개키>"
    },
    "deep-link": {
      "schemes": ["tda"]
    }
  }
}
```

### 7.4 GitHub Actions 워크플로

**deploy-pages.yml** (Pages 자동 배포):
```yaml
name: Deploy to Pages
on:
  push:
    branches: [main]
    paths: ['public/**']
permissions:
  pages: write
  id-token: write
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/upload-pages-artifact@v3
        with: { path: './public' }
      - uses: actions/deploy-pages@v4
```

**release.yml** (Windows .msi 자동 빌드):
```yaml
name: Release
on:
  push:
    tags: ['v*']
jobs:
  build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dtolnay/rust-toolchain@stable
      - uses: actions/setup-node@v4
      - run: npm install
      - run: npm run tauri build
      - uses: softprops/action-gh-release@v2
        with:
          files: |
            src-tauri/target/release/bundle/msi/*.msi
            latest.json
```

### 7.5 워크플로

1. **개발**: v40 HTML 수정 → `public/index.html`에 커밋·푸시
2. **즉시 반영**: GitHub Pages 자동 배포 (~1분), 브라우저 사용자는 새로고침으로 최신
3. **데스크톱 릴리스**: 안정화 후 `git tag v40.0.1` 푸시 → Actions가 .msi 빌드 → Release 페이지 자동 게시
4. **자동 업데이트**: 데스크톱 사용자는 앱 시작 시 새 버전 알림 → 클릭 한 번에 업데이트

### 7.6 사전 점검 사항

| 항목 | 상태 | 대응 |
|---|---|---|
| Supabase RLS | **점검 필요** | v39 코드의 RLS 설정 확인 후 §4.4 룰 적용 |
| WebView2 (Windows) | Win11 기본 / Win10 대부분 | 부재 시 부트스트래퍼 옵션 (+1MB) |
| GitHub Actions 무료 한도 | 월 2000분 (private) / 무제한 (public) | 공개 리포라 무제한 |
| 코드 사이닝 | 불필요 (내부 2~3명) | SmartScreen "더 정보" → "실행" 1회 |

---

## 8. 4일 실행 일정 (병렬 트랙)

| 날짜 | Track A: v40 HTML | Track B: 배포 인프라 |
|---|---|---|
| **5/26 월** | 데이터 모델 확장 + 위키 폴더 시드 + **Google/GitHub OAuth + 사용자 객체** | GitHub 리포 생성 + Tauri scaffold (`npm create tauri-app`) + 아이콘 |
| **5/27 화** | 3-Zone 칸반 + Sprint Lock + **reviewRequests 데이터 구조 + 4가지 룰 로직** | GitHub Actions 2개 워크플로 + Pages 활성화 |
| **5/28 수** | 양방향 링크 + 레퍼런스 카드 + **리뷰 모달 + 투표 UI + 알림 뱃지** | 첫 .msi 빌드 + 팀원 PC 설치 검증 + **Tauri deep-link OAuth 설정** |
| **5/29 목** | 회고 자동화 + **리뷰 결과 통합 (자동 ADR 생성) + 기술 문서 잠금** + 모바일 검증 | Tauri updater 설정 + 자동 업데이트 첫 사이클 검증 |

### 8.1 Day별 완료 정의

- **Day 1 끝**: 두 사용자가 OAuth로 로그인하면 헤더에 이름이 보임. 위키에 5개 폴더 자동 생성됨.
- **Day 2 끝**: 새 카드 추가 시 Shelf로 들어감. 스프린트 잠금 상태에서 Now에 직접 추가 불가. .msi 한 번 빌드되어 다운받을 수 있음.
- **Day 3 끝**: 끼어들기 요청 → 다른 사용자에게 알림 뜸 → 투표 가능. 한 명의 PC에 데스크톱 앱 설치 완료.
- **Day 4 끝**: 끼어들기 결정이 자동으로 ADR로 기록됨. 회고 자동 생성. 자동 업데이트로 새 버전 받기 동작.

---

## 9. v40.1로 미루는 것 (의도적 제외)

| 항목 | 이유 |
|---|---|
| 이메일/Slack 알림 | 토스트 + 상단 뱃지로 충분, 외부 연동은 다음 버전 |
| 간트차트 / 번다운 차트 | 1주 스프린트에서는 과잉, 진척률 게이지로 충분 |
| 권한·롤 세분화 | admin/member 2단계로 충분 |
| 모바일 네이티브 앱 (iOS/Android) | Tauri v2가 지원하지만 PWA로도 충분, 수요 확인 후 |
| 코드 사이닝 | 외부 배포 시점에 |
| 다국어 지원 | 한국어만 |
| 5측면 태그 시스템 | **결정으로 제거됨**, 양방향 링크로 충분 |

---

## 10. 첫 위클리 세션 안건 (5/26 월 오전)

이 문서를 가지고 30분 세션:

### 10.1 확인 (10분)
- [ ] 본 보고서의 §0 결정 사항 10개 모두 확인
- [ ] 추가 의견·수정 사항 있는가?

### 10.2 첫 스프린트 정의 (10분)
- [ ] sprint-2026W22 (5/26~6/1) 목표 1줄: ____________
- [ ] 최소구현 체크리스트 3~5개: ____________
- [ ] Track A / Track B 담당 배분: Dev A → ___ / Dev B → ___

### 10.3 운영 약속 (10분)
- [ ] 위클리 세션 정례화: 매주 월요일 오전 ___시
- [ ] 수요일 셀프 체크 알림 받기로 합의
- [ ] 금요일 회고 문서 검토 시간: ___
- [ ] 끼어들기 시 기본 룰 합의 (대부분 majority? all_agree?)

### 10.4 OAuth 가입 (즉시)
- [ ] 각자 Google 또는 GitHub로 첫 로그인 → users 테이블 등재
- [ ] RLS 룰 적용을 위한 이메일 목록 확정

---

## 11. 본 문서의 위상

- **5/26 첫 위클리 세션 문서**로 사용 (`05_운영/위클리 세션/2026-W22.md` 위치)
- **v40 정책의 단일 출처(Single Source of Truth)**
- 모든 결정의 근거가 여기 있음. 변경 시 §3 리뷰 시스템을 통한 `standard_change` 요청 권장 (룰: `all_agree`)
- 이전 `TDA_v40_방향성_보고서.md`는 본 문서로 흡수·대체됨

---

## 끝맺음

회의록의 7개 논의 항목, 시급 3개 항목, 중요 1개 항목이 모두 v40 한 번의 업그레이드에 매핑되어 있음. 핵심 가치는:

1. **칸반의 "덜 보여주기" 점프업** — 쌓이는 문제를 구조적으로 해결
2. **누적성 중심 문서 시스템** — 명시+누적되는 형식 실현
3. **일반화된 리뷰 메커니즘** — 끼어들기를 넘어 모든 의사결정으로
4. **신원 기반 audit trail** — Google/GitHub OAuth로 누가·언제·왜 명확
5. **Tauri 데스크톱 배포** — Obsidian 수준의 사용 경험

세부 룰(스프린트 길이, Buried 기간, 리뷰 룰 등)은 첫 1~2 스프린트 운영 후 회고에서 조정 가능. 본 문서 §0 결정 사항을 출발점으로 5/26 Day 1 착수.
