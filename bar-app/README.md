# BAR — Backup And Restore (standalone app)

A small Rust application that performs the same backup and restore of your OBS Studio
configuration as the `BAR.py` OBS script, but **without needing OBS to be running**.
It compiles to a single native executable (`.exe` on Windows, plain binary on Linux/macOS).

---

## Features

* **Backup** — creates a timestamped ZIP of your OBS `obs-studio` config folder.
* **External assets** — finds media files referenced from scene/config JSON files and includes them in the ZIP.
* **Stream-key stripping** — the stream key in `basic/service.json` is never stored in the backup.
* **Restore** — extracts a backup ZIP into your OBS config directory; backs up the existing config first.
* **Cross-platform re-pathing** — absolute paths in JSON configs are rewritten automatically when restoring a backup made on a different OS or PC.
* **Windows / Linux / macOS** — built from the same source.

---

## Build

### Prerequisites

* [Rust toolchain](https://rustup.rs/) ≥ 1.70

### Native build (same OS)

```sh
cd bar-app
cargo build --release
# The binary is at: target/release/bar   (or bar.exe on Windows)
```

### Cross-compile to Windows .exe from Linux

Install the MinGW cross-compiler and the Rust Windows target:

```sh
rustup target add x86_64-pc-windows-gnu
sudo apt-get install gcc-mingw-w64-x86-64   # or equivalent

cargo build --release --target x86_64-pc-windows-gnu
# Output: target/x86_64-pc-windows-gnu/release/bar.exe
```

---

## Usage

```
bar <COMMAND> [OPTIONS]
```

### Backup

```sh
# Backup to ~/obs-backups (default)
bar backup

# Backup to a specific folder, also include logs
bar backup --output D:\my-backups --include-logs

# Include cache files too (larger archive)
bar backup --output D:\my-backups --include-logs --include-cache
```

### Restore

```sh
# Restore from a ZIP created by this tool
bar restore obs-config-MY-PC-20241216-143022.zip

# Restore with a custom directory for assets whose original path is
# incompatible with this OS (cross-platform restore)
bar restore backup.zip --restore-assets D:\restored-assets
```

---

## What is backed up

| Included | Excluded by default |
|---|---|
| All OBS config files | `crashes/` |
| Scene JSON files | `logs/` (unless `--include-logs`) |
| Profiles, overlays, themes | `cache/`, `plugin_config/cef_cache/` (unless `--include-cache`) |
| External media files referenced in scenes | Temp files (`.tmp`, `.lock`) |

---

## Notes

* The backup ZIP has the structure `obs-config-<hostname>-<timestamp>/obs-studio/...`.
* A snapshot of the existing config is created at `obs-studio.before-restore-<timestamp>` before any restore.
* After restoring, **restart OBS Studio** to apply all changes.
