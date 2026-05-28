# 🔐 Supabase Service Key 노출 대응 가이드

## 무엇이 일어났는가

`.env.example` 파일 안에 실제 `SUPABASE_SERVICE_KEY`(service_role JWT) 가 commit 되어 GitHub repo에 푸시됐습니다. GitHub의 secret scanner가 이를 자동 감지해 알림을 보냈습니다.

**현재 r122에서 한 것:**
- ✅ `.env.example` 의 키를 `REPLACE_WITH_YOUR_SERVICE_ROLE_KEY` placeholder로 복원
- ✅ `.env` (실제 키 파일)는 `.gitignore` 에 이미 등록되어 있어 절대 commit 안 됨

**아직 남은 위험:**
- ⚠ Git history(`02132b2d`, 그 이후 여러 commit) 에는 옛 키가 그대로 보존됨
- ⚠ 누구나 `git log -p` 또는 GitHub의 commit 페이지에서 옛 키를 볼 수 있음
- ⚠ **반드시 키를 로테이션 해야 합니다**

## 🚨 사용자가 즉시 할 일 (5분)

### 1) Supabase에서 service_role 키 재생성

[Supabase Dashboard](https://supabase.com/dashboard) → 프로젝트(`xrmfunjekgwhoxtltzrj`) → **Settings → API**

- `service_role` 키 옆 **"Generate a new secret"** 또는 **"Roll"** 버튼 클릭
- 새 키 복사
- 옛 키는 즉시 무효화됨

### 2) 로컬 `.env` 에 새 키 반영

```
notepad C:\projects\tda-dashboard\deepwiki-integration\server\.env
```

```env
SUPABASE_SERVICE_KEY=<새로 생성한 키 붙여넣기>
```

저장 후 `run.bat` 재시작.

### 3) (선택) Git history 정화

옛 키가 history에 남아 있어도 무효화됐으므로 보안상 큰 문제 없음. 다만 **완전히 지우고 싶다면:**

#### 옵션 A — `git filter-repo` (권장, 빠름)
```powershell
# 1) git-filter-repo 설치
pip install git-filter-repo

# 2) .env.example의 옛 키 모든 history에서 제거
# (※ 아래의 OLD_LEAKED_JWT 는 `git log -p 02132b2d -- deepwiki-integration/server/.env.example` 로 직접 확인해서 채우세요.
#  이 문서 자체가 키를 다시 노출하지 않도록 placeholder만 두었습니다.)
cd C:\projects\tda-dashboard
git filter-repo --replace-text <(echo "<OLD_LEAKED_JWT_FROM_GIT_HISTORY>==>REDACTED")

# 3) 강제 푸시 (모든 history 재작성됨)
git push --force origin main
```

#### 옵션 B — 그냥 키 로테이션만 (충분히 안전)
옛 키는 이미 Supabase에서 무효화됐으니 history에 남아있어도 사용 불가. 일반적으로 이것만으로 충분합니다.

## 📋 GitHub 알림 처리

이메일 알림의 **"Sign in to GitHub"** 클릭 → repo의 **Security → Secret scanning alerts** → 해당 alert를 **"Revoked"** 로 마킹.

## 🛡 향후 예방

- 절대 `.env.example` 에 실제 키 적지 말 것. placeholder만.
- 실제 키는 `.env` (gitignore됨) 에만 두기.
- 만약 실수로 키를 commit 했다면 **즉시 Supabase에서 키 로테이션** 후 새 키 적용.
- pre-commit hook으로 secret 검사 가능 (`git-secrets`, `trufflehog` 등).
