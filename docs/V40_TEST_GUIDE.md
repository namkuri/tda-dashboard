# TDA v40 통합 테스트 가이드

> 라이브(브라우저) + .msi에서 v40 전체 기능을 점검하는 체크리스트.
> 코드 검증(구문/단위)은 완료. 아래는 **사람이 실제 환경에서** 확인할 항목.

## 0. 사전 설정 (1회)
- [ ] **Supabase SQL** 실행 (스프린트 히스토리):
      `ALTER TABLE public.sprints ADD COLUMN IF NOT EXISTS history jsonb DEFAULT '[]'::jsonb;`
- [ ] (Tauri OAuth 쓸 경우) Supabase Auth Redirect URLs에 `tda://auth-callback` 추가
- [ ] 시크릿 창에서 https://namkuri.github.io/tda-dashboard/ 접속 (캐시 영향 배제)

## 1. Day 1 — 인증/기반
- [ ] Google/GitHub 로그인 → 헤더 우측에 이름/아바타 표시
- [ ] 빈 프로젝트면 위키에 5개 폴더(01_기획~05_운영) 자동 생성
- [ ] 콘솔에 `[v40 auth event] ...`, `[rt-*] 연결됨` 정상 로그

## 2. Day 2 — 3-Zone & Sprint
- [ ] 카드 Zone 배지 클릭으로 Shelf↔Now↔Buried 순환 (스프린트 없을 때)
- [ ] 상단 필터: Now만 / Now+Shelf(기본) / 전체(Buried 포함)
- [ ] 카테고리 헤더에 Zone 카운트(📌📦🗑) 표시
- [ ] 스프린트 띠: [+ 스프린트 시작] → confirm/목표 → 🏃 띠 전환 (라벨 `2026 Wxx`, 월~일 범위)
- [ ] 완료 후 3일 경과 카드 자동 Buried (시간 경과 필요)

## 3. Day 3 — 링크 / 리뷰 / ADR / intrusion
**링크**
- [ ] 문서 본문에 `[[task:<카드ID>]]` 저장 → 파란 링크 렌더, 클릭 시 카드로 점프(하이라이트)
- [ ] 카드 "🔗 연결" / 문서 "+태스크/+문서" 피커로 연결 → 양쪽에 칩, ✕로 해제

**리뷰(2인 이상 권장)**
- [ ] 헤더 🗳 → 인박스 → "+ 새 결정 요청" → 사유+룰(다수결)+만료 → 생성 → 뱃지 증가
- [ ] 카드/문서에서 "결정 요청" → 대상이 자동 지정
- [ ] 다른 계정 승인/거부 → 과반 도달 시 자동 ✅/❌ 확정 + 명단 표시 + 양쪽 토스트
- [ ] `force` 룰 → 즉시 통과
- [ ] **ADR 자동 생성**: 결정 직후 05/03_결정 로그에 `ADR-...` 문서 생성, 본문 `[[task:]]` 클릭 동작

**intrusion (Sprint Lock)**
- [ ] 스프린트 활성 중 Shelf 카드 Zone 클릭 → "🔒 Now 이동은 intrusion 리뷰 필요" + 생성 모달
- [ ] 승인 시 카드 Now로 이동 + 스프린트 띠 `⚡ intrusion N` 증가

## 4. Day 4 — 회고 / Bury / 잠금
- [ ] 카드 🗑(처내기) → 사유 필수 입력 → Buried (사유 비우면 거부)
- [ ] 스프린트 [종료] → 미완료 카드 Shelf로 이월(carryover) + 05_운영에 "회고: ..." 자동 생성(완료율·통계)
- [ ] 문서 "🔓 잠금" → 잠긴 문서 편집창 readOnly + "🔒 변경 요청" → standard_change(all_agree) 리뷰 → 승인 시 자동 잠금 해제

## 5. Tauri (.msi)
- [ ] `npm run tauri build` 성공 (`cargo check`는 통과 확인됨)
- [ ] 설치본에서 OAuth 로그인 → 브라우저 인증 → `tda://` 복귀하며 로그인 완료
- [ ] (updater 적용 시) 새 태그 릴리스 → 자동 업데이트 알림

## 6. 멀티유저 실시간
- [ ] 두 기기에서 동시 접속 → 카드/문서/스프린트/리뷰 변경이 상대에 즉시 반영 + 토스트

## 알려진 제약
- 솔로(팀원 1명): majority/all_agree는 다른 투표자가 없어 통과 불가 → `force` 사용
- Tauri deep-link 실제 OAuth 왕복은 빌드+Supabase 설정 후에만 확인 가능
- 모바일 PWA 설치(iOS) 별도 검증 필요
