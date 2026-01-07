#![cfg_attr(
    all(not(debug_assertions), target_os = "windows"),
    windows_subsystem = "windows"
)]

mod rabbitmq;

use std::sync::Arc;
use tokio::sync::Mutex;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_os::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(Arc::new(Mutex::new(rabbitmq::RabbitState::new())))
        .invoke_handler(tauri::generate_handler![
            rabbitmq::rabbitmq_connect,
            rabbitmq::rabbitmq_disconnect,
            rabbitmq::rabbitmq_default_config,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
