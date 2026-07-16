/// OBS config directory detection, file iteration, timestamp/hostname utilities,
/// and INI parsing for scene-collection name lookup.
use std::collections::HashSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use walkdir::WalkDir;

// ─── Home / OBS config directories ───────────────────────────────────────────

pub fn home_dir() -> Result<PathBuf> {
    let var = if cfg!(target_os = "windows") {
        "USERPROFILE"
    } else {
        "HOME"
    };
    std::env::var(var)
        .map(PathBuf::from)
        .with_context(|| format!("{var} is not set; cannot determine home directory"))
}

pub fn get_obs_config_dir() -> Result<PathBuf> {
    #[cfg(target_os = "windows")]
    {
        let appdata =
            std::env::var("APPDATA").context("APPDATA is not set; cannot locate OBS config")?;
        return Ok(PathBuf::from(appdata).join("obs-studio"));
    }

    #[cfg(target_os = "macos")]
    {
        return Ok(home_dir()?
            .join("Library")
            .join("Application Support")
            .join("obs-studio"));
    }

    // Linux / BSD / other
    #[allow(unreachable_code)]
    {
        if let Ok(xdg) = std::env::var("XDG_CONFIG_HOME") {
            if !xdg.is_empty() {
                return Ok(PathBuf::from(xdg).join("obs-studio"));
            }
        }
        Ok(home_dir()?.join(".config").join("obs-studio"))
    }
}

// ─── Hostname / timestamp ─────────────────────────────────────────────────────

pub fn get_hostname() -> String {
    if cfg!(target_os = "windows") {
        if let Ok(h) = std::env::var("COMPUTERNAME") {
            if !h.is_empty() {
                return h;
            }
        }
    }
    if let Ok(h) = std::env::var("HOSTNAME") {
        if !h.is_empty() {
            return h;
        }
    }
    if let Ok(out) = std::process::Command::new("hostname").output() {
        let h = String::from_utf8_lossy(&out.stdout).trim().to_string();
        if !h.is_empty() {
            return h;
        }
    }
    "host".to_string()
}

pub fn get_timestamp() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let (y, mo, d, h, mi, s) = unix_to_datetime(secs);
    format!("{y:04}{mo:02}{d:02}-{h:02}{mi:02}{s:02}")
}

fn unix_to_datetime(secs: u64) -> (u32, u32, u32, u32, u32, u32) {
    let sec = (secs % 60) as u32;
    let min = ((secs / 60) % 60) as u32;
    let hour = ((secs / 3600) % 24) as u32;
    let mut days = secs / 86400;

    let mut year = 1970u32;
    loop {
        let dy: u64 = if is_leap(year) { 366 } else { 365 };
        if days < dy {
            break;
        }
        days -= dy;
        year += 1;
    }

    let month_days: [u64; 12] = if is_leap(year) {
        [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    } else {
        [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    };

    let mut month = 1u32;
    for &md in &month_days {
        if days < md {
            break;
        }
        days -= md;
        month += 1;
    }
    (year, month, (days + 1) as u32, hour, min, sec)
}

fn is_leap(y: u32) -> bool {
    (y % 4 == 0 && y % 100 != 0) || y % 400 == 0
}

pub fn make_backup_folder_name() -> String {
    format!("obs-config-{}-{}", get_hostname(), get_timestamp())
}

// ─── File iteration ───────────────────────────────────────────────────────────

fn build_excludes(include_logs: bool, include_cache: bool) -> HashSet<String> {
    let mut ex: HashSet<String> = ["crashes", "plugin_config/cef_cache"]
        .iter()
        .map(|s| s.to_string())
        .collect();
    if !include_logs {
        ex.insert("logs".into());
    }
    if !include_cache {
        ex.insert("cache".into());
    }
    ex
}

/// Returns true if a directory (given by its relative posix path from the OBS
/// config root and its bare name) should be skipped.
fn should_exclude_dir(rel: &str, name: &str, excludes: &HashSet<String>) -> bool {
    for ex in excludes {
        if ex.contains('/') {
            // Nested path — match by full relative path.
            if rel == ex || rel.starts_with(&format!("{ex}/")) {
                return true;
            }
        } else {
            // Simple name — prune any directory with this name at any depth.
            if name == ex {
                return true;
            }
        }
    }
    false
}

/// Yields `(rel_posix, absolute_path)` for every OBS config file that should
/// be included in a backup.
pub fn iter_obs_files(
    include_logs: bool,
    include_cache: bool,
) -> Result<Vec<(String, PathBuf)>> {
    let cfg = get_obs_config_dir()?;
    anyhow::ensure!(cfg.exists(), "OBS config not found at {}", cfg.display());

    let excludes = build_excludes(include_logs, include_cache);
    let cfg2 = cfg.clone();
    let excludes2 = excludes.clone();

    let walker = WalkDir::new(&cfg)
        .follow_links(false)
        .into_iter()
        .filter_entry(move |e| {
            let path = e.path();
            if path == cfg2 {
                return true; // always descend into root
            }
            // Skip hidden entries.
            let name = e.file_name().to_string_lossy();
            if name.starts_with('.') {
                return false;
            }
            if e.file_type().is_dir() {
                let rel = path
                    .strip_prefix(&cfg2)
                    .map(|r| r.to_string_lossy().replace('\\', "/"))
                    .unwrap_or_default();
                if should_exclude_dir(&rel, &name, &excludes2) {
                    return false;
                }
            }
            true
        });

    let mut result = Vec::new();
    for entry in walker {
        let entry = entry.context("Error walking OBS config directory")?;
        if entry.file_type().is_dir() {
            continue;
        }
        let path = entry.path().to_path_buf();
        let rel = path
            .strip_prefix(&cfg)
            .map(|r| r.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default();
        if rel.ends_with(".tmp") || rel.ends_with(".lock") {
            continue;
        }
        result.push((rel, path));
    }
    Ok(result)
}

// ─── INI / scene-collection helpers ──────────────────────────────────────────

#[allow(dead_code)]
pub fn read_ini_value(ini_path: &Path, section: &str, key: &str) -> Option<String> {
    let content = std::fs::read_to_string(ini_path).ok()?;
    let mut cur_section = String::new();
    for raw in content.lines() {
        let line = raw.trim();
        // Strip BOM from first line.
        let line = line.trim_start_matches('\u{feff}');
        if line.starts_with('[') && line.ends_with(']') {
            cur_section = line[1..line.len() - 1].to_string();
        } else if cur_section.eq_ignore_ascii_case(section) {
            if let Some(eq) = line.find('=') {
                if line[..eq].trim().eq_ignore_ascii_case(key) {
                    return Some(line[eq + 1..].trim().to_string());
                }
            }
        }
    }
    None
}

/// Returns the name of the active scene collection from the backup's global.ini,
/// falling back to the stem of the first JSON in basic/scenes/.
#[allow(dead_code)]
pub fn get_active_collection_name(obs_config_root: &Path) -> Option<String> {
    let global_ini = obs_config_root.join("global.ini");
    if global_ini.exists() {
        for key in &["SceneCollectionFile", "SceneCollection"] {
            if let Some(name) = read_ini_value(&global_ini, "Basic", key) {
                if !name.is_empty() {
                    return Some(name);
                }
            }
        }
    }
    let scenes_dir = obs_config_root.join("basic").join("scenes");
    if scenes_dir.exists() {
        let mut files: Vec<_> = scenes_dir
            .read_dir()
            .ok()?
            .flatten()
            .filter(|e| {
                e.path()
                    .extension()
                    .map_or(false, |x| x.eq_ignore_ascii_case("json"))
            })
            .collect();
        files.sort_by_key(|e| e.file_name());
        if let Some(first) = files.first() {
            return first.path().file_stem().map(|s| s.to_string_lossy().into_owned());
        }
    }
    None
}
