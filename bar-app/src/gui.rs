/// egui-based GUI for BAR — Backup And Restore.
///
/// Launched by `bar-gui` (src/main_gui.rs).  Backup and restore operations run
/// in a background thread so the UI stays responsive; results are reported back
/// via a shared `Arc<Mutex<…>>` and the egui repaint mechanism.
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use eframe::egui;

// ─── App state ────────────────────────────────────────────────────────────────

pub struct BarApp {
    // Backup fields
    backup_output: String,
    include_logs: bool,
    include_cache: bool,

    // Restore fields
    restore_zip: String,
    restore_assets: String,

    // Shared state between UI and worker threads
    log: Vec<String>,
    busy: Arc<Mutex<bool>>,
    result: Arc<Mutex<Option<Result<String, String>>>>,
}

impl Default for BarApp {
    fn default() -> Self {
        Self {
            backup_output: String::new(),
            include_logs: false,
            include_cache: false,
            restore_zip: String::new(),
            restore_assets: String::new(),
            log: Vec::new(),
            busy: Arc::new(Mutex::new(false)),
            result: Arc::new(Mutex::new(None)),
        }
    }
}

// ─── Worker helpers ───────────────────────────────────────────────────────────

impl BarApp {
    fn is_busy(&self) -> bool {
        *self.busy.lock().unwrap()
    }

    /// Check whether the background thread posted a result and, if so, display
    /// it in the log and clear the busy flag.
    fn poll_result(&mut self) {
        if let Ok(mut guard) = self.result.try_lock() {
            if let Some(res) = guard.take() {
                match res {
                    Ok(msg) => self.log.push(format!("✅  {msg}")),
                    Err(e) => self.log.push(format!("❌  {e}")),
                }
                *self.busy.lock().unwrap() = false;
            }
        }
    }

    fn run_backup(&mut self, ctx: egui::Context) {
        if self.is_busy() {
            return;
        }
        *self.busy.lock().unwrap() = true;
        self.log.push("⏳  Starting backup…".into());

        let output = if self.backup_output.trim().is_empty() {
            None
        } else {
            Some(PathBuf::from(self.backup_output.trim()))
        };
        let include_logs = self.include_logs;
        let include_cache = self.include_cache;
        let result = Arc::clone(&self.result);

        std::thread::spawn(move || {
            let output_dir = output.unwrap_or_else(|| {
                crate::obs::home_dir()
                    .unwrap_or_else(|_| PathBuf::from("."))
                    .join("obs-backups")
            });
            let res =
                crate::backup::create_local_backup_zip(&output_dir, include_logs, include_cache);
            let msg = match res {
                Ok(path) => {
                    let size = std::fs::metadata(&path)
                        .map(|m| m.len() as f64 / 1_048_576.0)
                        .unwrap_or(0.0);
                    Ok(format!(
                        "Backup saved: {} ({:.1} MB)",
                        path.display(),
                        size
                    ))
                }
                Err(e) => Err(e.to_string()),
            };
            *result.lock().unwrap() = Some(msg);
            ctx.request_repaint();
        });
    }

    fn run_restore(&mut self, ctx: egui::Context) {
        if self.is_busy() {
            return;
        }
        if self.restore_zip.trim().is_empty() {
            self.log.push("❌  Please select a backup ZIP file first.".into());
            return;
        }
        *self.busy.lock().unwrap() = true;
        self.log
            .push(format!("⏳  Restoring from {}…", self.restore_zip.trim()));

        let zip_path = PathBuf::from(self.restore_zip.trim());
        let assets_dir = if self.restore_assets.trim().is_empty() {
            None
        } else {
            Some(PathBuf::from(self.restore_assets.trim()))
        };
        let result = Arc::clone(&self.result);

        std::thread::spawn(move || {
            let res = crate::restore::restore_from_zip(&zip_path, assets_dir.as_deref());
            let msg = match res {
                Ok(()) => Ok("Restore complete — please restart OBS Studio.".into()),
                Err(e) => Err(e.to_string()),
            };
            *result.lock().unwrap() = Some(msg);
            ctx.request_repaint();
        });
    }
}

// ─── eframe::App ─────────────────────────────────────────────────────────────

impl eframe::App for BarApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        self.poll_result();

        egui::CentralPanel::default().show(ctx, |ui| {
            ui.heading("BAR — OBS Backup & Restore");
            ui.separator();

            let busy = self.is_busy();

            // ── Backup panel ──────────────────────────────────────────────────
            egui::Frame::group(ui.style()).show(ui, |ui| {
                ui.label(egui::RichText::new("Backup").strong().size(15.0));
                ui.add_space(4.0);

                ui.horizontal(|ui| {
                    ui.label("Output folder:");
                    ui.add(
                        egui::TextEdit::singleline(&mut self.backup_output)
                            .hint_text("default: ~/obs-backups")
                            .desired_width(280.0),
                    );
                    if ui.button("Browse…").clicked() {
                        if let Some(dir) = rfd::FileDialog::new().pick_folder() {
                            self.backup_output = dir.to_string_lossy().into_owned();
                        }
                    }
                });

                ui.horizontal(|ui| {
                    ui.checkbox(&mut self.include_logs, "Include logs");
                    ui.checkbox(&mut self.include_cache, "Include cache");
                });

                ui.add_space(4.0);
                if ui
                    .add_enabled(!busy, egui::Button::new("💾  Create Backup"))
                    .clicked()
                {
                    self.run_backup(ctx.clone());
                }
            });

            ui.add_space(8.0);

            // ── Restore panel ─────────────────────────────────────────────────
            egui::Frame::group(ui.style()).show(ui, |ui| {
                ui.label(egui::RichText::new("Restore").strong().size(15.0));
                ui.add_space(4.0);

                ui.horizontal(|ui| {
                    ui.label("ZIP file:      ");
                    ui.add(
                        egui::TextEdit::singleline(&mut self.restore_zip)
                            .hint_text("Select a backup .zip file")
                            .desired_width(280.0),
                    );
                    if ui.button("Browse…").clicked() {
                        if let Some(path) = rfd::FileDialog::new()
                            .add_filter("ZIP archive", &["zip"])
                            .pick_file()
                        {
                            self.restore_zip = path.to_string_lossy().into_owned();
                        }
                    }
                });

                ui.horizontal(|ui| {
                    ui.label("Assets folder:");
                    ui.add(
                        egui::TextEdit::singleline(&mut self.restore_assets)
                            .hint_text("default: ~/obs-restored-assets")
                            .desired_width(280.0),
                    );
                    if ui.button("Browse…").clicked() {
                        if let Some(dir) = rfd::FileDialog::new().pick_folder() {
                            self.restore_assets = dir.to_string_lossy().into_owned();
                        }
                    }
                });

                ui.add_space(4.0);
                if ui
                    .add_enabled(!busy, egui::Button::new("♻  Restore"))
                    .clicked()
                {
                    self.run_restore(ctx.clone());
                }
            });

            ui.add_space(8.0);

            // ── Log output ────────────────────────────────────────────────────
            ui.horizontal(|ui| {
                ui.label(egui::RichText::new("Output").strong());
                if ui.small_button("Clear").clicked() {
                    self.log.clear();
                }
                if busy {
                    ui.spinner();
                }
            });

            egui::ScrollArea::vertical()
                .max_height(160.0)
                .auto_shrink([false; 2])
                .show(ui, |ui| {
                    for line in &self.log {
                        ui.label(line);
                    }
                });
        });
    }
}
