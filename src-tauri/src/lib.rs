use async_nats::Client;
use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{collections::BTreeSet, fs, path::PathBuf, sync::Mutex};
use tauri::{Emitter, Manager};
use tauri::async_runtime::JoinHandle;
use walkdir::WalkDir;
use futures_util::StreamExt;

static STREAM_TASK: Lazy<Mutex<Option<JoinHandle<()>>>> = Lazy::new(|| Mutex::new(None));

#[derive(Default, Serialize, Deserialize, Clone)]
struct FilterConfig {
    prefixes: Vec<String>,
    log_prefixes: Vec<String>,
    ding_prefixes: Vec<String>,
    muted_prefixes: Vec<String>,
    default_volume: f32,
}

#[derive(Serialize, Deserialize, Clone)]
struct EventView {
    r#type: String,
    time: Option<String>,
    subject: Option<String>,
    raw: Value,
}

fn config_path(app: &tauri::AppHandle) -> PathBuf {
    app.path().app_config_dir().unwrap().join("filters.json")
}

#[tauri::command]
fn get_filters(app: tauri::AppHandle) -> Result<FilterConfig, String> {
    let p = config_path(&app);
    if !p.exists() {
        return Ok(FilterConfig {
            prefixes: vec![],
            log_prefixes: vec!["bloodbank.v1".to_string()],
            ding_prefixes: vec!["bloodbank.v1".to_string()],
            muted_prefixes: vec![],
            default_volume: 0.05,
        });
    }
    let s = fs::read_to_string(p).map_err(|e| e.to_string())?;
    let mut cfg: FilterConfig = serde_json::from_str(&s).map_err(|e| e.to_string())?;

    if cfg.default_volume <= 0.0 || cfg.default_volume > 1.0 {
        cfg.default_volume = 0.05;
    }
    if cfg.log_prefixes.is_empty() && !cfg.prefixes.is_empty() {
        cfg.log_prefixes = cfg.prefixes.clone();
    }
    if cfg.ding_prefixes.is_empty() && !cfg.prefixes.is_empty() {
        cfg.ding_prefixes = cfg.prefixes.clone();
    }

    Ok(cfg)
}

#[tauri::command]
fn set_filters(
    app: tauri::AppHandle,
    prefixes: Vec<String>,
    log_prefixes: Option<Vec<String>>,
    ding_prefixes: Option<Vec<String>>,
    muted_prefixes: Option<Vec<String>>,
    default_volume: Option<f32>,
) -> Result<(), String> {
    let mut cfg = FilterConfig {
        prefixes: prefixes.clone(),
        log_prefixes: log_prefixes.unwrap_or_else(|| prefixes.clone()),
        ding_prefixes: ding_prefixes.unwrap_or_else(|| prefixes.clone()),
        muted_prefixes: muted_prefixes.unwrap_or_default(),
        default_volume: default_volume.unwrap_or(0.05),
    };
    cfg.prefixes.sort();
    cfg.prefixes.dedup();
    cfg.log_prefixes.sort();
    cfg.log_prefixes.dedup();
    cfg.ding_prefixes.sort();
    cfg.ding_prefixes.dedup();
    cfg.muted_prefixes.sort();
    cfg.muted_prefixes.dedup();
    cfg.default_volume = cfg.default_volume.clamp(0.0, 1.0);
    let p = config_path(&app);
    if let Some(parent) = p.parent() { fs::create_dir_all(parent).map_err(|e| e.to_string())?; }
    fs::write(p, serde_json::to_string_pretty(&cfg).map_err(|e| e.to_string())?).map_err(|e| e.to_string())
}

#[tauri::command]
fn list_registered_event_types() -> Result<Vec<String>, String> {
    let mut candidates: Vec<PathBuf> = Vec::new();

    if let Ok(mut env_root) = std::env::var("BLOODBANK_SCHEMA_ROOT") {
        if env_root.starts_with("~/") {
            if let Ok(home) = std::env::var("HOME") {
                env_root = format!("{}/{}", home, &env_root[2..]);
            }
        }
        candidates.push(PathBuf::from(env_root));
    }

    candidates.push(PathBuf::from("../bloodbank/schemas/bloodbank/v1"));
    candidates.push(PathBuf::from("../../bloodbank/schemas/bloodbank/v1"));

    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd.join("../bloodbank/schemas/bloodbank/v1"));
        candidates.push(cwd.join("../../bloodbank/schemas/bloodbank/v1"));
    }

    let rootp = candidates
        .into_iter()
        .find(|p| p.exists() && p.is_dir())
        .ok_or_else(|| "Could not find bloodbank schema root; set BLOODBANK_SCHEMA_ROOT".to_string())?;

    let mut set = BTreeSet::<String>::new();
    for ent in WalkDir::new(rootp)
        .into_iter()
        .filter_map(Result::ok)
        .filter(|e| e.file_type().is_file())
    {
        let p = ent.path();
        if p.extension().and_then(|x| x.to_str()) != Some("json") {
            continue;
        }
        if let Ok(s) = fs::read_to_string(p) {
            if let Ok(v) = serde_json::from_str::<Value>(&s) {
                if let Some(t) = v
                    .get("properties")
                    .and_then(|x| x.get("type"))
                    .and_then(|x| x.get("const"))
                    .and_then(|x| x.as_str())
                {
                    set.insert(t.to_string());
                }
            }
        }
    }
    Ok(set.into_iter().collect())
}

#[tauri::command]
async fn start_event_stream(app: tauri::AppHandle) -> Result<(), String> {
    let mut lock = STREAM_TASK.lock().map_err(|_| "stream lock poisoned".to_string())?;
    if lock.is_some() { return Ok(()); }

    let app_handle = app.clone();
    let task = tauri::async_runtime::spawn(async move {
        let nats_url = std::env::var("BLOODBANK_NATS_URL").unwrap_or_else(|_| "nats://127.0.0.1:4222".into());
        match async_nats::connect(nats_url).await {
            Ok(client) => run_subscriber(client, app_handle).await,
            Err(_) => { let _ = app.emit("bloodbank://status", serde_json::json!({"connected": false})); }
        }
    });

    *lock = Some(task);
    Ok(())
}

async fn run_subscriber(client: Client, app: tauri::AppHandle) {
    let _ = app.emit("bloodbank://status", serde_json::json!({"connected": true}));
    let mut sub = match client.subscribe("bloodbank.evt.>").await {
        Ok(s) => s,
        Err(_) => { let _ = app.emit("bloodbank://status", serde_json::json!({"connected": false})); return; }
    };

    while let Some(msg) = sub.next().await {
        if let Ok(v) = serde_json::from_slice::<Value>(&msg.payload) {
            if let Some(t) = v.get("type").and_then(|x| x.as_str()) {
                let ev = EventView {
                    r#type: t.to_string(),
                    time: v.get("time").and_then(|x| x.as_str()).map(|s| s.to_string()),
                    subject: v.get("subject").and_then(|x| x.as_str()).map(|s| s.to_string()),
                    raw: v,
                };
                let _ = app.emit("bloodbank://event", ev);
            }
        }
    }
    let _ = app.emit("bloodbank://status", serde_json::json!({"connected": false}));
}

pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![
            get_filters,
            set_filters,
            list_registered_event_types,
            start_event_stream
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
