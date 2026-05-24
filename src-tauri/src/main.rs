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

fn main() {
    let mut builder = tauri::Builder::default();

    // 데스크톱: single-instance로 2번째 실행(딥링크)을 기존 창에 라우팅
    #[cfg(desktop)]
    {
        builder = builder.plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            forward_auth_urls(app, argv);
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.set_focus();
            }
        }));
    }

    builder
        .plugin(tauri_plugin_deep_link::init())
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
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running TDA Dashboard");
}
