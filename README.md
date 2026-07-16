# Backup And Restore (BAR)
This is a simple script write in Python for OBS Studio for backup and restore your OBS Studio files. You can use this script for backup and restore from your local disk or a private GitHub repo.

The backup includes the OBS `obs-studio` config folder and also external source files referenced from JSON scene/config files when they are accessible on disk (for example overlays, images, videos, and other local media files used by sources).

By default, logs are excluded unless `Include logs` is enabled. Cache folders, crash dumps, temporary files, and lock files are still excluded.

For GitHub backups, very large external assets are skipped to stay within practical upload limits of the GitHub contents API.

> [!IMPORTANT]
> Python 3.13+ is recommended to use this script

> [!NOTE]
> Go to `Tools` then `Scripts` to add this script with the `+` button.
>
> For getting GitHub token for accessing to your repo, you need to go [here](https://github.com/settings/tokens), then create a classic token, so go to `Token (classic)`, `Generate new token` then `Generate new token (classic)`. On `select scopes`, you just need to check the checkbox `repo`.
