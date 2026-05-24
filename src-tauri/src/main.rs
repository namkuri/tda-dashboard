// TDA Dashboard - Tauri entry point
// 단순 윈도우 래퍼. 모든 로직은 frontend(HTML)에 있음.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .setup(|_app| {
            #[cfg(debug_assertions)]
            {
                println!("[TDA] Tauri started in debug mode");
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running TDA Dashboard");
}
