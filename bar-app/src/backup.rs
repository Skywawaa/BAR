/// ZIP backup creation: collect OBS files + external assets, strip the stream
/// key from service.json, and write the archive.
use std::io::Write;
use std::path::Path;

use anyhow::{Context, Result};
use zip::{write::SimpleFileOptions, CompressionMethod, ZipWriter};

use crate::{assets, obs};

// ─── Stream-key stripping ─────────────────────────────────────────────────────

pub fn strip_stream_key(rel_path: &str, data: Vec<u8>) -> Vec<u8> {
    if rel_path.replace('\\', "/") != "basic/service.json" {
        return data;
    }
    let Ok(text) = std::str::from_utf8(&data) else {
        return data;
    };
    let Ok(mut obj) = serde_json::from_str::<serde_json::Value>(text) else {
        return data;
    };
    if let Some(settings) = obj.get_mut("settings").and_then(|s| s.as_object_mut()) {
        if settings.remove("key").is_some() {
            eprintln!("  Stream key stripped from service.json.");
            if let Ok(out) = serde_json::to_vec_pretty(&obj) {
                return out;
            }
        }
    }
    data
}

// ─── Backup creation ──────────────────────────────────────────────────────────

/// Create a ZIP backup of the OBS config (and referenced external assets) in
/// `output_dir`.  Returns the path to the created ZIP file and a list of
/// warnings for files that could not be read (skipped rather than aborting).
pub fn create_local_backup_zip(
    output_dir: &Path,
    include_logs: bool,
    include_cache: bool,
    include_plugins: bool,
) -> Result<(std::path::PathBuf, Vec<String>)> {
    std::fs::create_dir_all(output_dir)
        .with_context(|| format!("Cannot create output directory: {}", output_dir.display()))?;

    let folder_name = obs::make_backup_folder_name();
    let zip_path = output_dir.join(format!("{folder_name}.zip"));

    let file = std::fs::File::create(&zip_path)
        .with_context(|| format!("Cannot create ZIP file: {}", zip_path.display()))?;
    let mut zw = ZipWriter::new(file);
    let options = SimpleFileOptions::default()
        .compression_method(CompressionMethod::Deflated)
        .compression_level(Some(6));

    let mut warnings: Vec<String> = Vec::new();

    // ── OBS config files ──
    let obs_files = obs::iter_obs_files(include_logs, include_cache)?;
    eprintln!("  Adding {} config files...", obs_files.len());
    for (rel, src) in &obs_files {
        let data = match std::fs::read(src) {
            Ok(d) => d,
            Err(e) => {
                let msg = format!("Skipped (cannot read): {} — {e}", src.display());
                eprintln!("  Warning: {msg}");
                warnings.push(msg);
                continue;
            }
        };
        let data = strip_stream_key(rel, data);
        let entry_name = format!("{folder_name}/obs-studio/{rel}");
        if let Err(e) = zw.start_file(&entry_name, options) {
            let msg = format!("Skipped (ZIP entry error): {entry_name} — {e}");
            eprintln!("  Warning: {msg}");
            warnings.push(msg);
            continue;
        }
        if let Err(e) = zw.write_all(&data) {
            let msg = format!("Skipped (write error): {entry_name} — {e}");
            eprintln!("  Warning: {msg}");
            warnings.push(msg);
        }
    }

    // ── External assets ──
    eprintln!("  Scanning for external assets...");
    let ext_assets = assets::collect_external_assets(include_logs, include_cache)?;
    if !ext_assets.is_empty() {
        eprintln!("  Adding {} external asset(s)...", ext_assets.len());
        let mut manifest_entries: Vec<assets::ManifestEntry> = Vec::new();
        for asset in &ext_assets {
            match std::fs::read(&asset.source_path) {
                Ok(data) => {
                    let entry_name = format!("{folder_name}/{}", asset.backup_path);
                    if zw.start_file(&entry_name, options).is_ok() {
                        let _ = zw.write_all(&data);
                        manifest_entries.push(assets::ManifestEntry {
                            original_path: asset.original_path.clone(),
                            backup_path: asset.backup_path.clone(),
                        });
                    }
                }
                Err(e) => {
                    let msg = format!(
                        "Skipped asset (cannot read): {} — {e}",
                        asset.source_path.display()
                    );
                    eprintln!("  Warning: {msg}");
                    warnings.push(msg);
                }
            }
        }
        // Write manifest.
        let manifest = assets::AssetsManifest {
            version: assets::MANIFEST_VERSION,
            source_host: obs::get_hostname(),
            files: manifest_entries,
        };
        let manifest_json = serde_json::to_vec_pretty(&manifest)
            .context("Cannot serialise external assets manifest")?;
        let entry_name = format!("{folder_name}/{}", assets::MANIFEST_FILE);
        zw.start_file(&entry_name, options)
            .context("Cannot write manifest ZIP entry")?;
        zw.write_all(&manifest_json)
            .context("Cannot write manifest data")?;
    }

    // ── OBS plugin files ──
    if include_plugins {
        let install_files = obs::iter_obs_install_files();
        if install_files.is_empty() {
            eprintln!("  Note: OBS installation directory not found; skipping plugin backup.");
        } else {
            eprintln!("  Adding {} OBS plugin/install file(s)...", install_files.len());
            for (rel, src) in &install_files {
                let data = match std::fs::read(src) {
                    Ok(d) => d,
                    Err(e) => {
                        let msg = format!(
                            "Skipped plugin file (cannot read): {} — {e}",
                            src.display()
                        );
                        eprintln!("  Warning: {msg}");
                        warnings.push(msg);
                        continue;
                    }
                };
                let entry_name = format!("{folder_name}/{rel}");
                if let Err(e) = zw.start_file(&entry_name, options) {
                    let msg = format!("Skipped plugin file (ZIP entry error): {entry_name} — {e}");
                    eprintln!("  Warning: {msg}");
                    warnings.push(msg);
                    continue;
                }
                if let Err(e) = zw.write_all(&data) {
                    let msg = format!("Skipped plugin file (write error): {entry_name} — {e}");
                    eprintln!("  Warning: {msg}");
                    warnings.push(msg);
                }
            }
        }
    }

    zw.finish().context("Cannot finalise ZIP file")?;
    Ok((zip_path, warnings))
}
