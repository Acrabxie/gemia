use std::sync::Mutex;
use tauri::{Manager, State};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandChild;

struct ServerProcess(Mutex<Option<CommandChild>>);

/// Generic HTTP relay — uses reqwest with no_proxy to bypass system proxy.
/// Returns JSON: {"status": <u16>, "body": <string>}
#[tauri::command]
async fn api_call(
    method: String,
    path: String,
    body: Option<String>,
) -> Result<serde_json::Value, String> {
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(std::time::Duration::from_secs(300))
        .build()
        .map_err(|e| e.to_string())?;

    let url = format!("http://127.0.0.1:7788{}", path);

    let req = match method.to_uppercase().as_str() {
        "GET" => client.get(&url),
        "POST" => {
            let r = client
                .post(&url)
                .header("Content-Type", "application/json");
            if let Some(b) = body { r.body(b) } else { r }
        }
        _ => return Err(format!("Unsupported method: {}", method)),
    };

    let resp = req.send().await.map_err(|e| format!("Request failed: {}", e))?;
    let status = resp.status().as_u16();
    let text = resp.text().await.map_err(|e| e.to_string())?;

    Ok(serde_json::json!({ "status": status, "body": text }))
}

/// Upload a video file to the Python server, bypassing system proxy.
/// src_path: absolute local file path selected by the user.
#[tauri::command]
async fn upload_video(src_path: String) -> Result<serde_json::Value, String> {
    let path = std::path::PathBuf::from(&src_path);
    if !path.exists() {
        return Err(format!("File not found: {}", src_path));
    }
    let filename = path
        .file_name()
        .ok_or("Invalid path")?
        .to_string_lossy()
        .to_string();

    let data = std::fs::read(&path).map_err(|e| e.to_string())?;

    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(std::time::Duration::from_secs(120))
        .build()
        .map_err(|e| e.to_string())?;

    let resp = client
        .post("http://127.0.0.1:7788/upload-video")
        .header("Content-Type", "application/octet-stream")
        .header("X-Filename", &filename)
        .body(data)
        .send()
        .await
        .map_err(|e| format!("Upload failed: {}", e))?;

    let status = resp.status().as_u16();
    let text = resp.text().await.map_err(|e| e.to_string())?;

    Ok(serde_json::json!({ "status": status, "body": text }))
}

/// Fetch a video file from the server and return it as base64 for the WebView.
/// server_rel_path: relative path like "outputs/task_xxx_out.mp4"
#[tauri::command]
async fn fetch_video_b64(server_rel_path: String) -> Result<String, String> {
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(std::time::Duration::from_secs(60))
        .build()
        .map_err(|e| e.to_string())?;

    let url = format!("http://127.0.0.1:7788/file/{}", server_rel_path);
    let resp = client.get(&url).send().await.map_err(|e| e.to_string())?;

    if !resp.status().is_success() {
        return Err(format!("Server returned {}", resp.status()));
    }

    let bytes = resp.bytes().await.map_err(|e| e.to_string())?;
    use base64::Engine as _;
    Ok(base64::engine::general_purpose::STANDARD.encode(&bytes))
}

/// Open a file or directory in Finder.
#[tauri::command]
async fn reveal_in_finder(path: String) -> Result<(), String> {
    std::process::Command::new("open")
        .arg("-R")
        .arg(&path)
        .spawn()
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn save_config(openrouter_key: String, gemini_key: String) -> Result<(), String> {
    let config_dir = dirs_next::home_dir()
        .ok_or("Cannot find home directory")?
        .join(".gemia");
    std::fs::create_dir_all(&config_dir).map_err(|e| e.to_string())?;
    let config = serde_json::json!({
        "openrouter_api_key": openrouter_key,
        "gemini_api_key": gemini_key,
    });
    std::fs::write(
        config_dir.join("config.json"),
        serde_json::to_string_pretty(&config).unwrap(),
    )
    .map_err(|e| e.to_string())
}

#[tauri::command]
fn config_exists() -> bool {
    let path = match dirs_next::home_dir() {
        Some(h) => h.join(".gemia").join("config.json"),
        None => return false,
    };
    if !path.exists() {
        return false;
    }
    // Validate that openrouter_api_key is present, non-empty, and not a placeholder
    let Ok(text) = std::fs::read_to_string(&path) else { return false };
    let Ok(json) = serde_json::from_str::<serde_json::Value>(&text) else { return false };
    let key = json.get("openrouter_api_key")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    !key.is_empty() && key != "test" && key != "sk-or-..." && key.len() > 10
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(ServerProcess(Mutex::new(None)))
        .setup(|app| {
            match app.shell().sidecar("gemia-server") {
                Ok(cmd) => match cmd.spawn() {
                    Ok((_rx, child)) => {
                        let state: State<ServerProcess> = app.state();
                        *state.0.lock().unwrap() = Some(child);
                        println!("gemia-server sidecar started on :7788");
                    }
                    Err(e) => eprintln!("Failed to spawn sidecar: {}", e),
                },
                Err(e) => eprintln!("Sidecar not found: {}", e),
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            api_call,
            upload_video,
            fetch_video_b64,
            reveal_in_finder,
            save_config,
            config_exists,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
