// On Windows, suppress the console window so only the GUI is shown.
#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod assets;
mod backup;
mod gui;
mod obs;
mod restore;

use eframe::egui;

fn main() -> eframe::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("BAR — OBS Backup & Restore")
            .with_inner_size([560.0, 520.0])
            .with_min_inner_size([400.0, 380.0]),
        ..Default::default()
    };
    eframe::run_native(
        "BAR",
        options,
        Box::new(|_cc| Ok(Box::new(gui::BarApp::default()))),
    )
}
