use std::sync::Mutex;
use tauri::{Manager, State};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_shell::process::CommandChild;

struct ServerProcess(Mutex<Option<CommandChild>>);

#[tauri::command]
fn save_config(
    openrouter_key: String,
    gemini_key: String,
) -> Result<(), String> {
    let config_dir = dirs_next::home_dir()
        .ok_or("Cannot find home directory")?
        .join(".gemia");
    std::fs::create_dir_all(&config_dir).map_err(|e| e.to_string())?;
    let config = serde_json::json!({
        "openrouter_api_key": openrouter_key,
        "gemini_api_key": gemini_key,
    });
    let config_path = config_dir.join("config.json");
    std::fs::write(&config_path, serde_json::to_string_pretty(&config).unwrap())
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
fn config_exists() -> bool {
    dirs_next::home_dir()
        .map(|h| h.join(".gemia").join("config.json").exists())
        .unwrap_or(false)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_fs::init())
        .manage(ServerProcess(Mutex::new(None)))
        .setup(|app| {
            let shell = app.shell();
            // Start the Python sidecar server
            match shell.sidecar("gemia-server") {
                Ok(cmd) => {
                    match cmd.spawn() {
                        Ok((_rx, child)) => {
                            let state: State<ServerProcess> = app.state();
                            *state.0.lock().unwrap() = Some(child);
                            println!("gemia-server sidecar started");
                        }
                        Err(e) => {
                            eprintln!("Failed to spawn gemia-server sidecar: {}", e);
                        }
                    }
                }
                Err(e) => {
                    eprintln!("Sidecar not found: {}", e);
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![save_config, config_exists])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
