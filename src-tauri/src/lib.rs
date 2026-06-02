use async_nats::Client;
use futures_util::StreamExt;
use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeSet, HashSet},
    fs,
    path::{Path, PathBuf},
    sync::Mutex,
};
use tauri::async_runtime::JoinHandle;
use tauri::{Emitter, Manager};
use walkdir::WalkDir;

static STREAM_TASK: Lazy<Mutex<Option<JoinHandle<()>>>> = Lazy::new(|| Mutex::new(None));

fn default_volume() -> f32 {
    0.2
}

fn default_sound_enabled() -> bool {
    true
}

#[derive(Default, Serialize, Deserialize, Clone)]
struct FilterConfig {
    #[serde(default)]
    prefixes: Vec<String>,
    #[serde(default)]
    log_prefixes: Vec<String>,
    #[serde(default)]
    ding_prefixes: Vec<String>,
    #[serde(default)]
    muted_prefixes: Vec<String>,
    #[serde(default = "default_volume")]
    default_volume: f32,
    #[serde(default)]
    selected_types: Vec<String>,
    #[serde(default = "default_sound_enabled")]
    sound_enabled: bool,
}

#[derive(Serialize, Deserialize, Clone)]
struct EventView {
    r#type: String,
    kind: String,
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
            default_volume: default_volume(),
            selected_types: vec![],
            sound_enabled: true,
        });
    }

    let s = fs::read_to_string(p).map_err(|e| e.to_string())?;
    let mut cfg: FilterConfig = serde_json::from_str(&s).map_err(|e| e.to_string())?;

    if cfg.default_volume < 0.0 || cfg.default_volume > 1.0 {
        cfg.default_volume = default_volume();
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
#[allow(non_snake_case)]
fn set_filters(
    app: tauri::AppHandle,
    selected_types: Option<Vec<String>>,
    selectedTypes: Option<Vec<String>>,
    sound_enabled: Option<bool>,
    soundEnabled: Option<bool>,
    default_volume: Option<f32>,
    defaultVolume: Option<f32>,
    prefixes: Option<Vec<String>>,
    log_prefixes: Option<Vec<String>>,
    logPrefixes: Option<Vec<String>>,
    ding_prefixes: Option<Vec<String>>,
    dingPrefixes: Option<Vec<String>>,
    muted_prefixes: Option<Vec<String>>,
    mutedPrefixes: Option<Vec<String>>,
) -> Result<(), String> {
    let mut cfg = get_filters(app.clone())?;

    if let Some(mut values) = selected_types.or(selectedTypes) {
        values.sort();
        values.dedup();
        cfg.selected_types = values;
    }

    if let Some(enabled) = sound_enabled.or(soundEnabled) {
        cfg.sound_enabled = enabled;
    }

    if let Some(volume) = default_volume.or(defaultVolume) {
        cfg.default_volume = volume.clamp(0.0, 1.0);
    }

    if let Some(mut values) = prefixes {
        values.sort();
        values.dedup();
        cfg.prefixes = values;
    }

    if let Some(mut values) = log_prefixes.or(logPrefixes) {
        values.sort();
        values.dedup();
        cfg.log_prefixes = values;
    }

    if let Some(mut values) = ding_prefixes.or(dingPrefixes) {
        values.sort();
        values.dedup();
        cfg.ding_prefixes = values;
    }

    if let Some(mut values) = muted_prefixes.or(mutedPrefixes) {
        values.sort();
        values.dedup();
        cfg.muted_prefixes = values;
    }

    let p = config_path(&app);
    if let Some(parent) = p.parent() {
        fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    fs::write(
        p,
        serde_json::to_string_pretty(&cfg).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())
}

fn expand_tilde(path: &str) -> PathBuf {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Ok(home) = std::env::var("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(path)
}

fn dedup_paths(paths: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut seen = HashSet::<PathBuf>::new();
    let mut out = Vec::new();
    for path in paths {
        if seen.insert(path.clone()) {
            out.push(path);
        }
    }
    out
}

fn ancestor_chain(mut path: PathBuf) -> Vec<PathBuf> {
    if !path.is_dir() {
        let _ = path.pop();
    }

    let mut out = Vec::new();
    loop {
        out.push(path.clone());
        if !path.pop() {
            break;
        }
    }
    out
}

fn schema_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::<PathBuf>::new();

    if let Ok(env_root) = std::env::var("BLOODBANK_SCHEMA_ROOT") {
        candidates.push(expand_tilde(&env_root));
    }

    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd.join("../bloodbank/schemas/bloodbank/v1"));
        candidates.push(cwd.join("../../bloodbank/schemas/bloodbank/v1"));

        for ancestor in ancestor_chain(cwd) {
            candidates.push(ancestor.join("bloodbank/schemas/bloodbank/v1"));
        }
    }

    if let Ok(exe) = std::env::current_exe() {
        for ancestor in ancestor_chain(exe) {
            candidates.push(ancestor.join("bloodbank/schemas/bloodbank/v1"));
        }
    }

    candidates.push(
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../bloodbank/schemas/bloodbank/v1"),
    );

    dedup_paths(candidates)
}

fn extract_type_from_schema(path: &Path) -> Option<String> {
    let raw = fs::read_to_string(path).ok()?;
    let v: Value = serde_json::from_str(&raw).ok()?;
    v.get("properties")
        .and_then(|x| x.get("type"))
        .and_then(|x| x.get("const"))
        .and_then(|x| x.as_str())
        .map(ToString::to_string)
}

#[tauri::command]
fn list_registered_event_types() -> Result<Vec<String>, String> {
    let mut set = BTreeSet::<String>::new();

    for root in schema_candidates() {
        if !root.is_dir() {
            continue;
        }

        for ent in WalkDir::new(root)
            .into_iter()
            .filter_map(Result::ok)
            .filter(|e| e.file_type().is_file())
        {
            let path = ent.path();
            if path.extension().and_then(|x| x.to_str()) != Some("json") {
                continue;
            }
            if let Some(event_type) = extract_type_from_schema(path) {
                set.insert(event_type);
            }
        }

        if !set.is_empty() {
            break;
        }
    }

    Ok(set.into_iter().collect())
}

fn kind_from_subject(subject: &str) -> Option<&'static str> {
    let marker = subject.split('.').nth(1)?;
    match marker {
        "evt" => Some("event"),
        "cmd" => Some("command"),
        "rpy" => Some("reply"),
        _ => None,
    }
}

#[tauri::command]
async fn start_event_stream(app: tauri::AppHandle) -> Result<(), String> {
    let mut lock = STREAM_TASK
        .lock()
        .map_err(|_| "stream lock poisoned".to_string())?;
    if lock.is_some() {
        return Ok(());
    }

    let app_handle = app.clone();
    let task = tauri::async_runtime::spawn(async move {
        let nats_url =
            std::env::var("BLOODBANK_NATS_URL").unwrap_or_else(|_| "nats://127.0.0.1:4222".into());
        match async_nats::connect(nats_url).await {
            Ok(client) => run_subscriber(client, app_handle).await,
            Err(_) => {
                let _ = app.emit(
                    "bloodbank://status",
                    serde_json::json!({"connected": false}),
                );
            }
        }
    });

    *lock = Some(task);
    Ok(())
}

async fn run_subscriber(client: Client, app: tauri::AppHandle) {
    let _ = app.emit("bloodbank://status", serde_json::json!({"connected": true}));

    let mut sub = match client.subscribe("bloodbank.>".to_string()).await {
        Ok(s) => s,
        Err(_) => {
            let _ = app.emit(
                "bloodbank://status",
                serde_json::json!({"connected": false}),
            );
            return;
        }
    };

    while let Some(msg) = sub.next().await {
        let mut value = match serde_json::from_slice::<Value>(&msg.payload) {
            Ok(v) => v,
            Err(_) => continue,
        };

        let event_type = match value.get("type").and_then(|x| x.as_str()) {
            Some(t) if !t.is_empty() => t.to_string(),
            _ => continue,
        };

        let subject = msg.subject.to_string();
        if value.get("subject").is_none() {
            if let Some(obj) = value.as_object_mut() {
                obj.insert("subject".to_string(), Value::String(subject.clone()));
            }
        }

        let inferred_kind = kind_from_subject(&subject).unwrap_or("event").to_string();
        let kind = value
            .get("kind")
            .and_then(|x| x.as_str())
            .unwrap_or(&inferred_kind)
            .to_string();

        let ev = EventView {
            r#type: event_type,
            kind,
            time: value
                .get("time")
                .and_then(|x| x.as_str())
                .map(|s| s.to_string()),
            subject: Some(
                value
                    .get("subject")
                    .and_then(|x| x.as_str())
                    .unwrap_or(&subject)
                    .to_string(),
            ),
            raw: value,
        };

        let _ = app.emit("bloodbank://event", ev);
    }

    let _ = app.emit(
        "bloodbank://status",
        serde_json::json!({"connected": false}),
    );
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
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
