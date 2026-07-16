# Backup And Restore (BAR)
This is a simple script write in Python for OBS Studio for backup and restore your OBS Studio files. You can use this script for backup and restore from your local disk or a private GitHub repo.

The backup includes the OBS `obs-studio` config folder and also external source files referenced from JSON scene/config files when they are accessible on disk (for example overlays, images, videos, and other local media files used by sources).

By default, logs are excluded unless `Include logs` is enabled. Cache folders, crash dumps, temporary files, and lock files are still excluded.

For GitHub backups, very large external assets are skipped to stay within practical upload limits of the GitHub contents API.

## Stream key protection

The stream key stored in `basic/service.json` (Twitch, YouTube, Kick, etc.) is **automatically stripped** from every backup — both local and GitHub. The backup file never contains your stream key. When you restore on a new PC, simply re-enter your key in OBS Settings → Stream.

## Automatic re-pathing on restore

When restoring a backup created on a different PC or a different operating system (e.g. Windows → Linux), absolute paths embedded in scene JSON files (overlays, videos, fonts, browser sources…) are **automatically rewritten** to point to where the assets were actually placed on the new machine.

Assets whose original path is incompatible with the current OS are placed under the **Restored assets folder** (configurable in the script settings, defaults to `~/obs-restored-assets`), and all references in the JSON files are updated accordingly.

> [!IMPORTANT]
> Python 3.13+ is recommended to use this script

> [!NOTE]
> Go to `Tools` then `Scripts` to add this script with the `+` button.
>
> For getting GitHub token for accessing to your repo, you need to go [here](https://github.com/settings/tokens), then create a classic token, so go to `Token (classic)`, `Generate new token` then `Generate new token (classic)`. On `select scopes`, you just need to check the checkbox `repo`.
