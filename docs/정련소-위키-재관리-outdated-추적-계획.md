# 정련소 생성 문서 재관리 — outdated 추적·일괄 갱신/삭제 계획

> 작성 r269 시점. 목적: 정련소가 생성한 위키/일반 문서를, **원본(vault)·기준 문서가 바뀌면**
> LLM 이 변경분을 토대로 **일괄 추적해 삭제·수정**할 수 있게 한다(요청 #3). 구현 전 설계 보고.

---

## 1. 목표
- 원본 연구문서(vault)가 **v1→v2** 로 바뀌거나, 어떤 **새 기준 문서**가 생기면,
  그로부터 파생된 정련소 생성 문서(위키/일반)를 **자동 추적** → 영향받는 문서를
  **갱신/삭제/deprecated** 로 일괄 처리.
- 사용자는 "무엇이 outdated 되었는지" 한눈에 보고, **선별 채택**해 반영.

---

## 2. 현재 기반 (r269 에서 확보된 메타데이터)
생성 위키 문서 `wiki_docs.meta` + frontmatter 에 이미 기록됨:
- `refineryManaged: true` — 정련소 관리 대상 표식(재관리 대상 필터).
- `refinerySessionId` / `refinerySessionTitle` / `streamId` — 어느 세션·도출에서 나왔는지.
- `vaultSources: [{id,title}]` / `vaultSourceIds` — **파생 원본 vault 문서**(역추적 키).
- `originNodeIds` — 분해 키워드 노드.
- `tags`, `wikiTax`, `folderPath`, `generatedAt`, `docKind`.

→ "이 위키 문서는 vault X·Y 에서, 세션 S 로, T 시점에 생성" 을 이미 알 수 있다.

---

## 3. outdated 판정 방법
변경 감지에 필요한 **추가 기록(소스 스냅샷)** — 생성 시점의 원본 상태를 박제:
- 메타에 `vaultSnapshot: { vaultId: { updatedAt, hash, version } }` 추가
  (생성 시 각 파생 vault 문서의 `updated_at` + 본문 해시 + version 라벨 저장).
- **판정**: 현재 vault 문서의 `updated_at`/hash 가 스냅샷과 다르면 → 그 문서에서
  파생된 위키 문서는 **outdated 후보**.
- **신규 기준 문서**: 사용자가 "이 문서를 새 기준으로" 지정 → 그 문서와 관련된
  기존 생성 문서를 영향 후보로 모음(키워드/제목 겹침 + 같은 folderPath).

> r269 는 vaultSources(id/title)까지만 기록 → **vaultSnapshot(updatedAt/hash) 추가가
> 1순위 선행작업**. (작은 메타 추가, wiki_compose 에서 채움.)

---

## 4. 재관리 UI / 플로우
신규 페이지(또는 정련소 내 '재관리' 탭) `rfsReconcileView`:
1. **추적 목록**: `wiki_docs` 중 `meta.refineryManaged=true` 를 **세션/원본별 그룹**으로.
   각 행: 문서·파생원본·생성시점·현재 outdated 여부 배지(🟢최신 / 🟠변경됨 / 🔴원본삭제).
2. **변경 소스 패널**: outdated 사유(어떤 vault 가 언제→언제 바뀜, diff 요약).
3. **일괄 액션**(체크 후): **🔄 LLM 갱신**(변경분 반영해 재작성) / **🗑 삭제** /
   **⚠ deprecated 표시**(`is_deprecated=true`, 보존).
4. **갱신 미리보기**: 재작성 결과를 적용 전 diff 로 확인 후 채택.

---

## 5. LLM 역할 (변경분 캐치 → 영향 식별 → 제안)
- 백엔드 `refinery/reconcile.py`:
  - `detect_outdated(project_id)` — 스냅샷 vs 현재 비교(로직) → outdated 문서 목록 + 사유.
  - `reconcile_doc(doc, changed_sources, mode, model)` — LLM 이 **변경된 원본 발췌 +
    기존 위키 본문**을 받아, ⓐ 갱신본 재작성(옵시디언 링크·메타 보존) 또는 ⓑ "삭제 권고"
    판단 + 근거. 일괄은 문서별 SSE.
  - 옵션: "신규 기준 문서" 모드 — 기준 문서를 컨텍스트로 영향 문서 재정렬.
- 라우트: `/refinery/reconcile/scan`(outdated 스캔) · `/refinery/reconcile/apply`(갱신/삭제 일괄).

---

## 6. 데이터/스키마
- `wiki_docs.meta.vaultSnapshot` (jsonb 내부 키 — 신규 컬럼 불요).
- (선택) `wiki_docs` 에 `is_deprecated`(이미 존재) 활용.
- 일반 문서(연구문서 보관분)도 `meta.refineryManaged` 가 있으면 동일 추적.

---

## 7. 구현 단계
- **A**: wiki_compose 에 `vaultSnapshot`(updatedAt/hash/version) 기록 추가.
- **B**: `reconcile.detect_outdated` (로직 스냅샷 비교) + `/reconcile/scan` 라우트.
- **C**: 재관리 UI(추적 목록 + outdated 배지 + 변경 소스 패널).
- **D**: `reconcile_doc`(LLM 갱신/삭제 제안) + `/reconcile/apply` + 미리보기 diff.
- **E**: 신규 기준 문서 모드 + 일반문서 포함.

---

## 8. 리스크 / 고려
- **오삭제 방지**: 삭제는 기본 deprecated(보존) → 사용자 명시 확인 후 hard delete.
- **수동 편집 보존**: 사용자가 손본 위키 본문을 LLM 갱신이 덮어쓰지 않도록,
  옵시디언 user-section 보존 패턴(linker) 재사용 또는 diff 채택 단위 분리.
- **해시 비용**: 본문 해시는 생성/스캔 시 1회 — 가벼움.
- **LLM 검증 한계**: 실제 갱신 품질은 GPU 런타임 검증 필요(타 정련소 기능과 동일).

---

## 한 줄 요약
r269 메타데이터(파생 vault·세션·생성시점)에 **vaultSnapshot(변경감지용)** 만 더하면,
"원본 바뀐 위키 문서"를 로직으로 추려내고 LLM 으로 일괄 갱신/삭제하는 **재관리 페이지**를
세울 수 있다. 삭제는 보존(deprecated) 우선, 수동 편집은 보호한다.
