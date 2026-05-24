# TDA Dashboard

> Team Dev Archive — Wiki & Task Management System
> pB-3 COMBAT 모듈을 위한 협업 도구

## 🌐 사용 방법

### 브라우저 (가장 빠름)
👉 https://namkuri.github.io/tda-dashboard/

Chrome/Edge 주소창 우측의 "앱 설치" 아이콘을 누르면 PWA로 설치되어 데스크톱 앱처럼 동작합니다.

### Windows 데스크톱 앱
[Releases](https://github.com/namkuri/tda-dashboard/releases) 페이지에서 최신 `.msi` 다운로드 후 실행.

> ⚠️ 첫 실행 시 Windows SmartScreen 경고: "더 정보" → "실행" 클릭 (코드 사이닝 미적용 상태)

## 🏗 구조

```
tda-dashboard/
├── public/              # GitHub Pages 서빙 대상 = Tauri frontend
│   ├── index.html       # v40 메인 (Supabase + OAuth + Kanban + Wiki)
│   ├── manifest.json    # PWA manifest
│   ├── sw.js            # Service Worker (오프라인 캐시)
│   └── icon-*.png       # PWA 아이콘
├── src-tauri/           # Windows .msi 빌드 래퍼
│   ├── tauri.conf.json
│   ├── Cargo.toml
│   └── src/main.rs
└── .github/workflows/
    ├── deploy-pages.yml  # public/ 변경 시 자동 Pages 배포
    └── release.yml       # 태그 푸시 시 자동 .msi 빌드
```

## 🔧 로컬 개발

### 웹 (브라우저)
```bash
cd public
python -m http.server 3000
# 또는: npx serve -p 3000
```

→ http://localhost:3000

### 데스크톱 (Tauri)
**전제조건**:
- Node.js 20+
- Rust stable (https://rustup.rs)
- Windows: WebView2 (Win11 기본 탑재)

```bash
npm install
npm run tauri:dev    # 개발 모드
npm run tauri:build  # .msi 빌드 (결과: src-tauri/target/release/bundle/msi/)
```

## 🚀 배포

### Pages (자동)
`public/` 폴더에 푸시하면 GitHub Actions가 자동 배포 → ~1분 후 Pages 갱신.

### .msi (수동 트리거)
```bash
git tag v40.0.1
git push origin v40.0.1
```
→ Actions가 Windows runner에서 .msi 빌드 → Release 페이지 자동 게시.

## 🔐 환경 설정

### Supabase
- 프로젝트: `xrmfunjekgwhoxtltzrj`
- 인증: Google OAuth + GitHub OAuth
- 콜백 URL: `https://xrmfunjekgwhoxtltzrj.supabase.co/auth/v1/callback`
- RLS: 인증된 사용자만 접근 가능

### Anon Key
`public/index.html` 상단의 `SUPABASE_ANON_KEY_DEFAULT` 상수에 하드코딩.
공개 키이므로 리포 공개해도 안전 (RLS가 보호).

## 📋 문서

- [v40 통합 마스터 보고서](./docs/TDA_v40_통합_마스터_보고서.md)
- [Supabase 마이그레이션 SQL](./docs/tda_v40_supabase_migration.sql)

## 📜 라이센스

Internal use only (팀 2-3명 내부 협업 도구).
