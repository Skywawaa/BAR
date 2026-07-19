// On Windows, suppress the console window so only the GUI is shown.
#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod assets;
mod backup;
mod gui;
mod obs;
mod restore;

use eframe::egui;

/// Show a modal error dialog on Windows so failures are never silent.
/// On other platforms the message is printed to stderr.
fn show_error(msg: &str) {
    #[cfg(target_os = "windows")]
    {
        use std::ffi::OsStr;
        use std::iter::once;
        use std::os::windows::ffi::OsStrExt;

        let text: Vec<u16> = OsStr::new(msg).encode_wide().chain(once(0)).collect();
        let caption: Vec<u16> = OsStr::new("BAR — Error")
            .encode_wide()
            .chain(once(0))
            .collect();

        // user32.dll is always present on Windows; no extra crate needed.
        #[link(name = "user32")]
        extern "system" {
            fn MessageBoxW(
                hwnd: *mut std::ffi::c_void,
                text: *const u16,
                caption: *const u16,
                mb_type: u32,
            ) -> i32;
        }

        // MB_OK (0x0) | MB_ICONERROR (0x10)
        unsafe { MessageBoxW(std::ptr::null_mut(), text.as_ptr(), caption.as_ptr(), 0x10) };
    }
    #[cfg(not(target_os = "windows"))]
    eprintln!("BAR error: {msg}");
}

fn main() {
    // Catch panics (e.g. GPU initialisation failure) and surface them to the
    // user instead of silently exiting.
    std::panic::set_hook(Box::new(|info| {
        show_error(&format!("BAR GUI crashed unexpectedly:\n\n{info}"));
    }));

    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title("BAR — OBS Backup & Restore")
            .with_inner_size([560.0, 520.0])
            .with_min_inner_size([400.0, 380.0]),
        ..Default::default()
    };
    if let Err(e) = eframe::run_native(
        "BAR",
        options,
        Box::new(|_cc| Ok(Box::new(gui::BarApp::default()))),
    ) {
        show_error(&format!(
            "BAR GUI failed to start.\n\n\
             This is usually caused by missing or outdated graphics drivers.\n\n\
             Error details: {e}"
        ));
    }
}
