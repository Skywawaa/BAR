/// External-asset collection: scan OBS JSON files for absolute file paths that
/// live outside the OBS config directory and record them for backup / restore.
use std::path::{Path, PathBuf};

use anyhow::Result;

use crate::obs;

pub const EXTERNAL_ASSETS_DIR: &str = "external-assets";
pub const MANIFEST_FILE: &str = "external-assets.json";
pub const MANIFEST_VERSION: u32 = 1;

// ─── Structs ──────────────────────────────────────────────────────────────────

#[derive(Debug)]
pub struct ExternalAsset {
    pub original_path: String,
    pub backup_path: String,
    pub source_path: PathBuf,
}

#[derive(serde::Serialize, serde::Deserialize, Debug)]
pub struct ManifestEntry {
    pub original_path: String,
    pub backup_path: String,
}

#[derive(serde::Serialize, serde::Deserialize, Debug)]
pub struct AssetsManifest {
    pub version: u32,
    pub source_host: String,
    pub files: Vec<ManifestEntry>,
}

// ─── Path helpers ─────────────────────────────────────────────────────────────

pub fn looks_like_windows_abs_path(s: &str) -> bool {
    // Strip extended-length path prefix (\\?\) before checking so that
    // \\?\C:\... is recognised as a drive path rather than a UNC path.
    let s = if s.starts_with("\\\\?\\") { &s[4..] } else { s };
    // Drive letter path: C:\ or C:/
    let b = s.as_bytes();
    if b.len() >= 3
        && b[0].is_ascii_alphabetic()
        && b[1] == b':'
        && (b[2] == b'\\' || b[2] == b'/')
    {
        return true;
    }
    // UNC path: \\server
    s.starts_with("\\\\")
}

pub fn windows_path_flat_parts(raw: &str) -> Vec<String> {
    // Strip extended-length path prefix (\\?\) before splitting.
    // \\?\C:\...   → C:\...   (drive path)
    // \\?\UNC\s\r  → \\s\r   (UNC path — re-add leading \\)
    let raw: &str = if let Some(rest) = raw.strip_prefix("\\\\?\\") {
        if let Some(unc) = rest.strip_prefix("UNC\\") {
            // We can't easily return an owned String here, so just use
            // unc directly (server\share\... without the leading \\).
            // The caller in backup_rel_for_external_asset handles UNC.
            unc
        } else {
            rest
        }
    } else {
        raw
    };

    if raw.starts_with("\\\\") {
        // UNC: \\server\share\...
        return raw
            .trim_start_matches('\\')
            .split(|c| c == '\\' || c == '/')
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect();
    }
    // Drive: C:\Users\...
    let mut parts: Vec<String> = Vec::new();
    if raw.len() >= 2 && raw.as_bytes()[1] == b':' {
        // push drive letter without colon
        parts.push(raw[..1].to_string());
        for p in raw[2..].split(|c| c == '\\' || c == '/') {
            if !p.is_empty() {
                parts.push(p.to_string());
            }
        }
    } else {
        parts = raw
            .split(|c| c == '\\' || c == '/')
            .filter(|s| !s.is_empty())
            .map(str::to_string)
            .collect();
    }
    parts
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = &s[i + 1..i + 3];
            if let Ok(b) = u8::from_str_radix(hex, 16) {
                out.push(b);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Convert a raw JSON string value to a canonical PathBuf if it looks like an
/// absolute path that exists on disk.  Returns None otherwise.
pub fn normalize_asset_path(value: &str) -> Option<PathBuf> {
    let mut v = value.trim().to_string();
    if v.is_empty() {
        return None;
    }
    if v.starts_with("file://") {
        // Strip "file://" and percent-decode.
        let without_scheme = &v["file://".len()..];
        let decoded = percent_decode(without_scheme);
        // On Windows, file:///C:/... → /C:/... → strip leading /
        if cfg!(target_os = "windows")
            && decoded.starts_with('/')
            && decoded.len() > 2
            && looks_like_windows_abs_path(&decoded[1..])
        {
            v = decoded[1..].to_string();
        } else {
            v = decoded;
        }
    }
    let candidate = PathBuf::from(&v);
    if !candidate.is_absolute() {
        return None;
    }
    if !candidate.exists() || !candidate.is_file() {
        return None;
    }
    let canonical = candidate.canonicalize().ok()?;
    // Rust's canonicalize() on Windows returns extended-length paths
    // (\\?\C:\...).  Strip that prefix so original_path in manifests and
    // repath replacements match what OBS config files actually contain.
    #[cfg(target_os = "windows")]
    let canonical = {
        let s = canonical.to_string_lossy();
        if let Some(rest) = s.strip_prefix("\\\\?\\") {
            if let Some(unc) = rest.strip_prefix("UNC\\") {
                PathBuf::from(format!("\\\\{unc}"))
            } else {
                PathBuf::from(rest)
            }
        } else {
            canonical
        }
    };
    Some(canonical)
}

/// Recursively collect all string leaves from a JSON value.
pub fn iter_nested_strings(value: &serde_json::Value, out: &mut Vec<String>) {
    match value {
        serde_json::Value::String(s) => out.push(s.clone()),
        serde_json::Value::Array(arr) => arr.iter().for_each(|v| iter_nested_strings(v, out)),
        serde_json::Value::Object(map) => {
            map.values().for_each(|v| iter_nested_strings(v, out))
        }
        _ => {}
    }
}

/// Map an absolute asset path to a stable backup-relative posix path.
pub fn backup_rel_for_external_asset(path: &Path) -> String {
    let raw = path.to_string_lossy();
    if looks_like_windows_abs_path(&raw) {
        let parts = windows_path_flat_parts(&raw);
        if raw.starts_with("\\\\") {
            // UNC: external-assets/windows-unc/server/share/...
            let mut rel = PathBuf::from(EXTERNAL_ASSETS_DIR).join("windows-unc");
            for p in &parts {
                rel = rel.join(p);
            }
            return rel.to_string_lossy().replace('\\', "/");
        }
        // Drive: external-assets/windows/C/Users/...
        let mut rel = PathBuf::from(EXTERNAL_ASSETS_DIR).join("windows");
        for p in &parts {
            rel = rel.join(p);
        }
        return rel.to_string_lossy().replace('\\', "/");
    }
    // Posix: external-assets/posix/home/user/overlay.png
    let parts: Vec<_> = path.components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .filter(|s| s != "/")
        .collect();
    let mut rel = PathBuf::from(EXTERNAL_ASSETS_DIR).join("posix");
    for p in &parts {
        rel = rel.join(p);
    }
    rel.to_string_lossy().replace('\\', "/")
}

// ─── Collection ───────────────────────────────────────────────────────────────

/// Scan all OBS JSON config files and return every referenced external asset
/// (absolute path, exists, outside the OBS config dir).
pub fn collect_external_assets(
    include_logs: bool,
    include_cache: bool,
) -> Result<Vec<ExternalAsset>> {
    let cfg = obs::get_obs_config_dir()?.canonicalize().unwrap_or_else(|_| {
        obs::get_obs_config_dir().unwrap()
    });

    let mut assets: Vec<ExternalAsset> = Vec::new();
    let mut seen = std::collections::HashSet::new();

    let files = obs::iter_obs_files(include_logs, include_cache)?;
    let json_files: Vec<_> = files
        .into_iter()
        .filter(|(rel, _)| rel.to_lowercase().ends_with(".json"))
        .collect();

    let total = json_files.len();
    for (i, (_rel, src)) in json_files.iter().enumerate() {
        if (i + 1) % 50 == 0 || i + 1 == total {
            eprint!("\r  Scanning JSON files for assets... {}/{total}", i + 1);
        }
        let content = match std::fs::read_to_string(src) {
            Ok(c) => c,
            Err(_) => continue,
        };
        let value: serde_json::Value = match serde_json::from_str(&content) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let mut strings = Vec::new();
        iter_nested_strings(&value, &mut strings);
        for s in strings {
            let Some(asset_path) = normalize_asset_path(&s) else {
                continue;
            };
            // Skip assets that live inside the OBS config dir.
            if asset_path.starts_with(&cfg) {
                continue;
            }
            let key = asset_path.to_string_lossy().into_owned();
            if seen.contains(&key) {
                continue;
            }
            seen.insert(key.clone());
            assets.push(ExternalAsset {
                original_path: key,
                backup_path: backup_rel_for_external_asset(&asset_path),
                source_path: asset_path,
            });
        }
    }
    if total > 0 {
        eprintln!();
    }
    Ok(assets)
}
