# Tauri 데스크톱 OAuth(deep-link) & 자동 업데이트 설정

> Day 3 청크 4 / Day 4 Tauri 작업의 **사용자 액션 가이드**.
> 코드/설정은 이미 리포에 반영됨. 아래는 빌드·키·외부 콘솔에서 사람이 해야 하는 단계.

---

## 1. Deep-link OAuth (`tda://auth-callback`) — 코드 반영 완료

### 무엇이 들어갔나
- `src-tauri/Cargo.toml`: `tauri-plugin-deep-link`, (데스크톱) `tauri-plugin-single-instance`(deep-link feature)
- `src-tauri/tauri.conf.json`: `plugins.deep-link.desktop.schemes = ["tda"]`
- `src-tauri/src/main.rs`: 딥링크 수신 → `main` webview에 `window.__tdaAuthCallback(url)` eval, single-instance로 2번째 실행을 기존 창에 라우팅
- `public/index.html`:
  - `IS_TAURI` 감지(`tauri.localhost`/`tauri:`) → `signInWithProvider`가 `redirectTo: 'tda://auth-callback'` 사용
  - `window.__tdaAuthCallback(url)`: URL fragment에서 `access_token`/`refresh_token` 추출 → `supabaseClient.auth.setSession(...)`

### 동작 흐름
1. .msi 앱에서 Google/GitHub 로그인 클릭 → 시스템 브라우저 열림
2. 인증 후 Supabase가 `tda://auth-callback#access_token=...&refresh_token=...` 로 리다이렉트
3. OS가 `tda://`를 앱으로 전달 → `main.rs`가 받아 webview에 eval → `__tdaAuthCallback`이 세션 설정 → `SIGNED_IN` → `initSupabase()`

### ⚠️ 사용자가 해야 할 일
1. **Supabase 대시보드** → Authentication → URL Configuration → **Redirect URLs**에 추가:
   ```
   tda://auth-callback
   ```
   (기존 `https://namkuri.github.io/tda-dashboard/`는 브라우저용으로 유지)
   - Google/GitHub OAuth 콘솔은 **변경 불필요** (공급자는 Supabase 콜백으로만 리다이렉트, Supabase가 다시 앱으로 보냄)
2. **재빌드**: `npm run tauri build` (deep-link 스킴은 설치 시 OS에 등록됨 — dev에서는 `register_all()`이 런타임 등록)
3. **테스트**: 설치본 실행 → 로그인 → 브라우저 인증 → 앱으로 복귀하며 로그인 완료 확인
   - 개발 중 테스트: `npm run tauri dev` 후 로그인. Windows에서 딥링크 등록이 안 되면 한 번 설치본을 깔아 스킴 등록 후 재시도.

---

## 2. 자동 업데이트(updater) — **미적용**(빌드 안정성 위해 보류, 키 생성 필요)

updater는 **개인 서명키**가 있어야 동작하며, 키는 사람이 생성·보관해야 하므로 코드에 넣지 않았습니다.
아래를 따라 적용하세요.

### 2-1. 키 생성 (1회, 로컬)
```powershell
npm run tauri signer generate -- -w $HOME\.tauri\tda-updater.key
```
- 출력된 **공개키(public key)** 문자열 → 아래 `pubkey`에 사용
- 비공개키 파일(`tda-updater.key`)과 비밀번호는 **절대 커밋 금지**

### 2-2. `src-tauri/Cargo.toml`
```toml
tauri-plugin-updater = "2"
```

### 2-3. `src-tauri/src/main.rs` — 플러그인 등록
```rust
builder
    .plugin(tauri_plugin_updater::Builder::new().build())
    // ... 기존 deep-link/setup ...
```

### 2-4. `src-tauri/tauri.conf.json`
```jsonc
"plugins": {
  "deep-link": { "desktop": { "schemes": ["tda"] } },
  "updater": {
    "endpoints": ["https://namkuri.github.io/tda-dashboard/latest.json"],
    "pubkey": "여기에-2-1에서-생성된-공개키"
  }
},
"bundle": {
  "createUpdaterArtifacts": true,
  ...
}
```

### 2-5. GitHub Actions 서명 (`release.yml`)
태그 푸시 빌드 시 환경변수로 서명:
```yaml
env:
  TAURI_SIGNING_PRIVATE_KEY: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}
  TAURI_SIGNING_PRIVATE_KEY_PASSWORD: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY_PASSWORD }}
```
- GitHub 리포 Settings → Secrets → 위 2개 등록(개인키 파일 내용 + 비밀번호)
- 빌드 산출물의 `latest.json`을 Pages(`public/`) 또는 Release에 게시하면 앱이 시작 시 확인

### 2-6. 프론트/Rust에서 업데이트 확인 트리거
- Rust `setup`에서 `app.updater()?.check()` 또는 프론트에서 `@tauri-apps/plugin-updater` 사용

> 적용 후 `npm run tauri build`로 검증. pubkey/서명이 맞아야 업데이트가 적용됨.

---

## 3. 현재 상태 요약
- ✅ deep-link 코드/설정 반영 (사용자: Supabase Redirect URL 추가 + 재빌드 + 테스트)
- ⏸ updater: 위 2장 따라 키 생성 후 적용 (보류 — 빌드를 깨지 않기 위해 미반영)
