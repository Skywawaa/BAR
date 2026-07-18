mod assets;
mod backup;
mod obs;
mod restore;

use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(
    name = "bar",
    version,
    about = "OBS Backup And Restore — standalone app\n\
             Backup and restore your OBS Studio configuration without needing OBS to run.",
    long_about = None,
)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Create a ZIP backup of the OBS Studio configuration
    Backup {
        /// Output directory for the backup ZIP (default: ~/obs-backups)
        #[arg(short, long, value_name = "DIR")]
        output: Option<PathBuf>,
        /// Include OBS log files in the backup
        #[arg(long)]
        include_logs: bool,
        /// Include cache files in the backup (produces a larger archive)
        #[arg(long)]
        include_cache: bool,
        /// Include OBS plugin binaries from the installation directory
        /// (obs-plugins/ and data/obs-plugins/; Windows only)
        #[arg(long)]
        include_plugins: bool,
    },
    /// Restore OBS Studio configuration from a backup ZIP
    Restore {
        /// Path to the backup ZIP file
        zip_file: PathBuf,
        /// Directory where assets are placed when their original path is
        /// incompatible with this OS (default: ~/obs-restored-assets)
        #[arg(long, value_name = "DIR")]
        restore_assets: Option<PathBuf>,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Commands::Backup {
            output,
            include_logs,
            include_cache,
            include_plugins,
        } => {
            let obs_cfg = obs::get_obs_config_dir()?;
            let output_dir = output.unwrap_or_else(|| {
                obs::home_dir()
                    .unwrap_or_else(|_| PathBuf::from("."))
                    .join("obs-backups")
            });

            eprintln!("OBS config : {}", obs_cfg.display());
            eprintln!("Output dir : {}", output_dir.display());

            let zip_path =
                backup::create_local_backup_zip(&output_dir, include_logs, include_cache, include_plugins)?;

            let (zip_path, warnings) = zip_path;
            let size_mb = std::fs::metadata(&zip_path)
                .map(|m| m.len() as f64 / 1_048_576.0)
                .unwrap_or(0.0);
            eprintln!();
            println!(
                "Backup complete: {} ({:.1} MB)",
                zip_path.display(),
                size_mb
            );
            if !warnings.is_empty() {
                eprintln!();
                eprintln!("⚠  {} file(s) were skipped:", warnings.len());
                for w in &warnings {
                    eprintln!("   • {w}");
                }
            }
        }

        Commands::Restore {
            zip_file,
            restore_assets,
        } => {
            anyhow::ensure!(
                zip_file.exists(),
                "File not found: {}",
                zip_file.display()
            );
            anyhow::ensure!(
                zip_file.extension().map_or(false, |e| e.eq_ignore_ascii_case("zip")),
                "Expected a .zip file, got: {}",
                zip_file.display()
            );

            eprintln!("Restoring from: {}", zip_file.display());
            restore::restore_from_zip(&zip_file, restore_assets.as_deref())?;

            eprintln!();
            println!("Restore complete.");
            println!("Please restart OBS Studio to apply the restored configuration.");
        }
    }

    Ok(())
}
