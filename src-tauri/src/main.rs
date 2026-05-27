// TDA Dashboard - Tauri entry point
// 단순 윈도우 래퍼. 모든 로직은 frontend(HTML)에 있음.
// [v40 Day3 청크4] OAuth deep-link(tda://auth-callback) 처리:
//   시스템 브라우저 OAuth → Supabase가 tda://auth-callback#access_token=... 으로 리다이렉트
//   → OS가 앱으로 라우팅 → 이 코드가 URL을 받아 webview의 window.__tdaAuthCallback(url) 호출
//   (프론트는 CDN 단일 HTML이라 Tauri JS API 없이 eval로 토큰 전달)

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::Manager;

// 받은 URL 중 tda:// 스킴을 webview로 전달
fn forward_auth_urls(app: &tauri::AppHandle, urls: Vec<String>) {
    let auth: Vec<String> = urls.into_iter().filter(|u| u.starts_with("tda://")).collect();
    if auth.is_empty() {
        return;
    }
    if let Some(win) = app.get_webview_window("main") {
        for url in auth {
            // serde_json으로 안전하게 문자열 리터럴 생성 (이스케이프 처리)
            let arg = serde_json::to_string(&url).unwrap_or_else(|_| "\"\"".to_string());
            let js = format!(
                "window.__tdaAuthCallback && window.__tdaAuthCallback({});",
                arg
            );
            let _ = win.eval(&js);
        }
        let _ = win.set_focus();
    }
}

// [r59] 자동 업데이트 — 프론트(단일 HTML)는 번들러가 없어 updater JS 플러그인을 못 쓰므로
//   Rust에서 체크/설치를 처리하고 invoke 커맨드로 노출. JS는 카드 UI만 띄움.
#[derive(serde::Serialize)]
struct UpdateInfo {
    version: String,
    current: String,
    notes: String,
}

#[cfg(desktop)]
#[tauri::command]
async fn update_check(app: tauri::AppHandle) -> Result<Option<UpdateInfo>, String> {
    use tauri_plugin_updater::UpdaterExt;
    let updater = app.updater().map_err(|e| e.to_string())?;
    match updater.check().await {
        Ok(Some(u)) => Ok(Some(UpdateInfo {
            version: u.version.clone(),
            current: u.current_version.clone(),
            notes: u.body.clone().unwrap_or_default(),
        })),
        Ok(None) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

#[cfg(desktop)]
#[tauri::command]
async fn update_install(app: tauri::AppHandle) -> Result<(), String> {
    use tauri_plugin_updater::UpdaterExt;
    let updater = app.updater().map_err(|e| e.to_string())?;
    if let Some(update) = updater.check().await.map_err(|e| e.to_string())? {
        update
            .download_and_install(|_chunk, _total| {}, || {})
            .await
            .map_err(|e| e.to_string())?;
        app.restart();
    }
    Ok(())
}

// 비데스크톱(모바일 등)에서도 generate_handler가 참조할 수 있게 no-op 정의
#[cfg(not(desktop))]
#[tauri::command]
async fn update_check(_app: tauri::AppHandle) -> Result<Option<UpdateInfo>, String> { Ok(None) }
#[cfg(not(desktop))]
#[tauri::command]
async fn update_install(_app: tauri::AppHandle) -> Result<(), String> { Ok(()) }

// [r60] OAuth URL을 시스템 기본 브라우저로 — 앱 웹뷰를 OAuth 페이지로 보내지 않고,
//   브라우저에서 로그인 후 tda://auth-callback 딥링크로 앱에 복귀(기존 forward_auth_urls).
#[tauri::command]
fn open_external(url: String) -> Result<(), String> {
    open::that(url).map_err(|e| e.to_string())
}

fn main() {
    let mut builder = tauri::Builder::default();

    // 데스크톱: single-instance로 2번째 실행(딥링크)을 기존 창에 라우팅 + 자동 업데이트
    #[cfg(desktop)]
    {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            forward_auth_urls(app, argv);
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.set_focus();
            }
        }));
        builder = builder.plugin(tauri_plugin_updater::Builder::new().build());
        builder = builder.plugin(tauri_plugin_process::init());
    }

    builder
        .plugin(tauri_plugin_opener::init()) // [r68] 시스템 브라우저 열기 (원격 페이지 ACL 허용)
        .plugin(tauri_plugin_deep_link::init())
        .invoke_handler(tauri::generate_handler![update_check, update_install, open_external])
        .setup(|app| {
            #[cfg(debug_assertions)]
            {
                println!("[TDA] Tauri started in debug mode");
            }
            #[cfg(desktop)]
            {
                use tauri_plugin_deep_link::DeepLinkExt;
                // 런타임 등록 (개발 환경/미등록 대비). 설치본은 conf의 schemes로 등록됨.
                let _ = app.deep_link().register_all();
                let handle = app.handle().clone();
                app.deep_link().on_open_url(move |event| {
                    let urls: Vec<String> = event.urls().iter().map(|u| u.to_string()).collect();
                    forward_auth_urls(&handle, urls);
                });
                // [r64] 진단용 devtools 자동 오픈 제거(릴리스 정리). devtools feature는 유지 →
                //   필요 시 우클릭 '검사'로 콘솔 확인 가능.
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running TDA Dashboard");
}
