/// ZIP restore: extract backup, copy files to the OBS config dir, restore
/// external assets, and rewrite cross-platform paths in JSON configs.
use std::collections::HashMap;
use std::io::Read;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

use crate::{assets, obs};

// ─── Path helpers ─────────────────────────────────────────────────────────────

fn needs_repath(original_path: &str) -> bool {
    if cfg!(target_os = "windows") {
        return original_path.starts_with('/')
            && !assets::looks_like_windows_abs_path(original_path);
    }
    assets::looks_like_windows_abs_path(original_path)
}

fn target_path_from_manifest(original_path: &str) -> Option<PathBuf> {
    if cfg!(target_os = "windows") {
        if original_path.starts_with('/')
            && !assets::looks_like_windows_abs_path(original_path)
        {
            return None;
        }
        if assets::looks_like_windows_abs_path(original_path) {
            return Some(PathBuf::from(original_path));
        }
        return Some(PathBuf::from(original_path));
    }
    if assets::looks_like_windows_abs_path(original_path) {
        return None;
    }
    let p = PathBuf::from(original_path);
    if p.is_absolute() { Some(p) } else { None }
}

fn fallback_asset_path(original_path: &str, restore_dir: &Path) -> PathBuf {
    let parts: Vec<String> = if assets::looks_like_windows_abs_path(original_path) {
        assets::windows_path_flat_parts(original_path)
    } else {
        PathBuf::from(original_path)
            .components()
            .map(|c| c.as_os_str().to_string_lossy().into_owned())
            .filter(|s| s != "/")
            .collect()
    };
    let mut result = restore_dir.to_path_buf();
    for p in &parts {
        result = result.join(p);
    }
    result
}

/// Convert an absolute path to a file:// URL string (for JSON matching).
fn path_to_file_url(path_str: &str) -> String {
    let p = PathBuf::from(path_str);
    // Percent-encode special characters.
    let s = p.to_string_lossy().replace('\\', "/");
    let encoded: String = s
        .chars()
        .flat_map(|c| {
            // Characters that are safe in a file URL path.
            if c.is_ascii_alphanumeric()
                || matches!(c, '-' | '_' | '.' | '~' | '/' | ':')
            {
                vec![c]
            } else {
                let mut buf = [0u8; 4];
                let s = c.encode_utf8(&mut buf);
                s.bytes().flat_map(|b| {
                    format!("%{b:02X}").chars().collect::<Vec<_>>()
                }).collect()
            }
        })
        .collect();
    if encoded.starts_with('/') {
        format!("file://{encoded}")
    } else {
        format!("file:///{encoded}")
    }
}

// ─── JSON re-pathing ──────────────────────────────────────────────────────────

/// Rewrite absolute paths in all OBS JSON config files based on `path_mapping`
/// (original_path → new_path).  Handles plain paths, backslash escaping, and
/// file:// URL variants exactly as the Python script does.
pub fn repath_obs_json_files(obs_config_dir: &Path, path_mapping: &HashMap<String, String>) {
    if path_mapping.is_empty() {
        return;
    }
    // Build replacement pairs: (old_json_repr, new_json_repr).
    let mut replacements: Vec<(String, String)> = Vec::new();
    for (old, new) in path_mapping {
        // JSON-encode the path strings (strips outer quotes via [1..-1]).
        let old_j = serde_json::to_string(old).unwrap_or_default();
        let new_j = serde_json::to_string(new).unwrap_or_default();
        let old_j = &old_j[1..old_j.len() - 1];
        let new_j = &new_j[1..new_j.len() - 1];
        replacements.push((old_j.to_string(), new_j.to_string()));

        // file:// URL variant.
        let old_url = path_to_file_url(old);
        let new_url = path_to_file_url(new);
        let old_u = serde_json::to_string(&old_url).unwrap_or_default();
        let new_u = serde_json::to_string(&new_url).unwrap_or_default();
        let old_u = &old_u[1..old_u.len() - 1];
        let new_u = &new_u[1..new_u.len() - 1];
        if old_u != old_j {
            replacements.push((old_u.to_string(), new_u.to_string()));
        }

        // Forward-slash variant of Windows paths.
        if assets::looks_like_windows_abs_path(old) {
            let old_fwd = old.replace('\\', "/");
            let new_fwd = new.replace('\\', "/");
            let old_f = serde_json::to_string(&old_fwd).unwrap_or_default();
            let new_f = serde_json::to_string(&new_fwd).unwrap_or_default();
            let old_f = &old_f[1..old_f.len() - 1];
            let new_f = &new_f[1..new_f.len() - 1];
            if old_f != old_j && old_f != old_u {
                replacements.push((old_f.to_string(), new_f.to_string()));
            }
        }
    }

    let mut repathed = 0usize;
    let walker = walkdir::WalkDir::new(obs_config_dir).follow_links(false);
    for entry in walker.into_iter().flatten() {
        if !entry.file_type().is_file() {
            continue;
        }
        let path = entry.path();
        if path.extension().map_or(true, |e| !e.eq_ignore_ascii_case("json")) {
            continue;
        }
        let Ok(text) = std::fs::read_to_string(path) else {
            continue;
        };
        let mut modified = text.clone();
        for (old, new) in &replacements {
            if modified.contains(old.as_str()) {
                modified = modified.replace(old.as_str(), new.as_str());
            }
        }
        if modified != text {
            if std::fs::write(path, modified.as_bytes()).is_ok() {
                repathed += 1;
            } else {
                eprintln!("  Warning: cannot rewrite paths in {}", path.display());
            }
        }
    }
    if repathed > 0 {
        eprintln!("  Auto re-pathed {repathed} JSON file(s).");
    }
}

// ─── External asset restore ───────────────────────────────────────────────────

fn restore_external_assets(
    backup_root: &Path,
    restore_assets_dir: &Path,
) -> Result<HashMap<String, String>> {
    let manifest_path = backup_root.join(assets::MANIFEST_FILE);
    if !manifest_path.exists() {
        return Ok(HashMap::new());
    }
    let manifest_text = std::fs::read_to_string(&manifest_path)
        .with_context(|| format!("Cannot read manifest: {}", manifest_path.display()))?;
    let manifest: assets::AssetsManifest =
        serde_json::from_str(&manifest_text).context("Cannot parse external assets manifest")?;

    let current_host = obs::get_hostname();
    if !manifest.source_host.is_empty() && manifest.source_host != current_host {
        eprintln!(
            "  Note: backup was made on '{}', restoring on '{current_host}' — paths may differ.",
            manifest.source_host
        );
    }

    let mut path_mapping = HashMap::new();
    for entry in &manifest.files {
        let original = &entry.original_path;
        let src = backup_root.join(&entry.backup_path);
        if !src.exists() {
            eprintln!("  Warning: missing external asset in backup: {}", entry.backup_path);
            continue;
        }
        let dest: PathBuf = if needs_repath(original) {
            let d = fallback_asset_path(original, restore_assets_dir);
            path_mapping.insert(original.clone(), d.to_string_lossy().into_owned());
            d
        } else {
            match target_path_from_manifest(original) {
                Some(d) => d,
                None => {
                    let d = fallback_asset_path(original, restore_assets_dir);
                    path_mapping.insert(original.clone(), d.to_string_lossy().into_owned());
                    d
                }
            }
        };
        if let Some(parent) = dest.parent() {
            if let Err(e) = std::fs::create_dir_all(parent) {
                eprintln!("  Warning: cannot create directory {}: {e}", parent.display());
                continue;
            }
        }
        if let Err(e) = std::fs::copy(&src, &dest) {
            eprintln!("  Warning: cannot restore '{original}': {e}");
        }
    }
    Ok(path_mapping)
}

// ─── Backup root resolution ───────────────────────────────────────────────────

fn find_nested_obs_studio(folder: &Path, max_depth: u32) -> Option<PathBuf> {
    let root_depth = folder.components().count();
    for entry in walkdir::WalkDir::new(folder)
        .max_depth(max_depth as usize)
        .into_iter()
        .flatten()
    {
        let path = entry.path();
        let depth = (path.components().count() as i64 - root_depth as i64) as u32;
        if path.file_name().map_or(false, |n| n == "obs-studio") {
            return Some(path.to_path_buf());
        }
        if depth >= max_depth {
            continue;
        }
    }
    None
}

/// Returns `(obs_config_root, backup_container_root)`.
fn resolve_backup_roots(folder: &Path) -> Result<(PathBuf, PathBuf)> {
    if folder.join("obs-studio").exists() {
        return Ok((folder.join("obs-studio"), folder.to_path_buf()));
    }
    if folder.file_name().map_or(false, |n| n == "obs-studio") {
        let parent = folder.parent().unwrap_or(folder).to_path_buf();
        return Ok((folder.to_path_buf(), parent));
    }
    if let Some(nested) = find_nested_obs_studio(folder, 2) {
        let parent = nested.parent().unwrap_or(folder).to_path_buf();
        return Ok((nested, parent));
    }
    anyhow::bail!(
        "Invalid backup structure: cannot find an obs-studio folder in {}",
        folder.display()
    )
}

// ─── Main restore entry point ─────────────────────────────────────────────────

pub fn restore_from_zip(zip_path: &Path, restore_assets_dir: Option<&Path>) -> Result<()> {
    let obs_cfg = obs::get_obs_config_dir()?;
    eprintln!("  OBS config: {}", obs_cfg.display());

    // Back up the current config before touching anything.
    if obs_cfg.exists() {
        let backup_name = format!("obs-studio.before-restore-{}", obs::get_timestamp());
        let backup_dir = obs_cfg.parent().unwrap_or(Path::new(".")).join(&backup_name);
        eprint!("  Backing up current config to {}...", backup_dir.display());
        match fs_extra_copy_dir(&obs_cfg, &backup_dir) {
            Ok(_) => eprintln!(" done."),
            Err(e) => eprintln!("\n  Warning: cannot back up current config: {e}"),
        }
    }

    // Extract ZIP into a temp directory.
    let tmp = tempfile::Builder::new()
        .prefix("bar-restore-")
        .tempdir()
        .context("Cannot create temp directory")?;
    let tmp_path = tmp.path().to_path_buf();

    {
        let zip_file = std::fs::File::open(zip_path)
            .with_context(|| format!("Cannot open {}", zip_path.display()))?;
        let mut archive =
            zip::ZipArchive::new(zip_file).context("Cannot read ZIP file")?;
        eprintln!("  Extracting {} entries...", archive.len());
        for i in 0..archive.len() {
            let mut entry = archive.by_index(i).context("Cannot read ZIP entry")?;
            let entry_name = entry.name().to_string();
            if entry_name.ends_with('/') {
                continue; // directory entry
            }
            // Sanitise: skip absolute paths and any component with '..'
            if entry_name.starts_with('/') || entry_name.starts_with("\\\\") {
                continue;
            }
            if entry_name
                .split(|c| c == '/' || c == '\\')
                .any(|seg| seg == "..")
            {
                continue;
            }
            let dest = tmp_path.join(&entry_name);
            if let Some(parent) = dest.parent() {
                std::fs::create_dir_all(parent)
                    .with_context(|| format!("Cannot create {}", parent.display()))?;
            }
            let mut out = std::fs::File::create(&dest)
                .with_context(|| format!("Cannot create {}", dest.display()))?;
            let mut buf = Vec::new();
            entry.read_to_end(&mut buf).context("Cannot read ZIP entry data")?;
            std::io::Write::write_all(&mut out, &buf)
                .with_context(|| format!("Cannot write {}", dest.display()))?;
        }
    }

    let (obs_root, backup_root) = resolve_backup_roots(&tmp_path)?;

    // Copy OBS config files.
    obs_cfg.exists().then(|| ());
    std::fs::create_dir_all(&obs_cfg).context("Cannot create OBS config directory")?;

    for entry in std::fs::read_dir(&obs_root)
        .with_context(|| format!("Cannot read {}", obs_root.display()))?
    {
        let entry = entry?;
        let dest = obs_cfg.join(entry.file_name());
        // Remove existing.
        if dest.exists() {
            if dest.is_dir() {
                let _ = std::fs::remove_dir_all(&dest);
            } else {
                let _ = std::fs::remove_file(&dest);
            }
        }
        let src = entry.path();
        if src.is_dir() {
            copy_dir_all(&src, &dest)
                .with_context(|| format!("Cannot copy {} → {}", src.display(), dest.display()))?;
        } else {
            std::fs::copy(&src, &dest)
                .with_context(|| format!("Cannot copy {} → {}", src.display(), dest.display()))?;
        }
    }
    eprintln!("  Config files restored.");

    // Restore external assets.
    let default_restore_dir = obs::home_dir()?.join("obs-restored-assets");
    let ra_dir = restore_assets_dir.unwrap_or(&default_restore_dir);
    let path_mapping = restore_external_assets(&backup_root, ra_dir)
        .unwrap_or_else(|e| {
            eprintln!("  Warning: external asset restore error: {e}");
            HashMap::new()
        });

    // Rewrite cross-platform paths.
    if !path_mapping.is_empty() {
        eprintln!("  Re-pathing JSON config files...");
        repath_obs_json_files(&obs_cfg, &path_mapping);
    }

    Ok(())
}

// ─── Utility: recursive directory copy ───────────────────────────────────────

fn copy_dir_all(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let ty = entry.file_type()?;
        let dest = dst.join(entry.file_name());
        if ty.is_dir() {
            copy_dir_all(&entry.path(), &dest)?;
        } else {
            std::fs::copy(entry.path(), dest)?;
        }
    }
    Ok(())
}

fn fs_extra_copy_dir(src: &Path, dst: &Path) -> std::io::Result<()> {
    copy_dir_all(src, dst)
}
