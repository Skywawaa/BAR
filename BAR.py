# OBS Backup Restore And Restore (BAR)
# plugin for OBS Studio for backup & restore configs files on local disk
# works well on Windows and Linux (not tested on macOS, but it should work)

import obspython as obs
import configparser
import os
import sys
import platform
import time
import json
import tempfile
import zipfile
import shutil
import re
from pathlib import Path
from pathlib import PureWindowsPath
from urllib import parse

g_props = None

K_LOCAL_DIR = "local_dir"
K_INCLUDE_LOGS = "include_logs"
K_INCLUDE_CACHE = "include_cache"
K_BACKUP_NOW = "backup_now"
K_STATUS = "status_text"
K_RESTORE_LOCAL_PATH = "restore_local_path"
K_RESTORE_LOCAL_BTN = "restore_local_btn"
EXTERNAL_ASSETS_DIR = "external-assets"
EXTERNAL_ASSETS_MANIFEST_FILE = "external-assets.json"
EXTERNAL_ASSETS_MANIFEST_VERSION = 1
LOG_PROGRESS_INTERVAL = 50

def _log(level, msg):
    try:
        if level == obs.LOG_WARNING:
            obs.script_log(obs.LOG_WARNING, msg)
        elif level == obs.LOG_ERROR:
            obs.script_log(obs.LOG_ERROR, msg)
        else:
            obs.script_log(obs.LOG_INFO, msg)
    except Exception:
        print(msg)


def info(msg):
    _log(obs.LOG_INFO, msg)


def warn(msg):
    _log(obs.LOG_WARNING, msg)


def err(msg):
    _log(obs.LOG_ERROR, msg)


def now_stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def hostname():
    try:
        return platform.node() or "host"
    except Exception:
        return "host"


def get_obs_config_dir() -> Path:
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise RuntimeError("APPDATA not set; cannot locate OBS config")
        return Path(appdata) / "obs-studio"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "obs-studio"
    else:
        
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            return Path(xdg) / "obs-studio"
        return Path.home() / ".config" / "obs-studio"


def _excluded_dirs(include_logs: bool, include_cache: bool):
    excludes = {"crashes", "plugin_config/cef_cache", "cache"}
    if not include_logs:
        excludes.add("logs")
    if include_cache:
        
        excludes.discard("cache")
        excludes.discard("plugin_config/cef_cache")
    return excludes


def iter_obs_files(include_logs: bool, include_cache: bool):
    """Yield tuples (rel_path_posix, absolute_path) for files to back up."""
    cfg = get_obs_config_dir()
    if not cfg.exists():
        raise FileNotFoundError(f"OBS config not found at {cfg}")

    excludes = _excluded_dirs(include_logs, include_cache)
    for root, dirs, files in os.walk(str(cfg)):
        root_path = Path(root)
        pruned = []
        for d in list(dirs):
            if d.startswith("."):
                pruned.append(d)
                continue
            rel = (root_path / d).relative_to(cfg).as_posix()
            excluded = False
            for ex in excludes:
                if rel == ex or rel.startswith(ex.rstrip("/") + "/") or d == ex:
                    excluded = True
                    break
            if excluded:
                pruned.append(d)
                continue
        for d in pruned:
            dirs.remove(d)

        for f in files:
            if f.endswith(".tmp") or f.endswith(".lock"):
                continue
            p = root_path / f
            rel = p.relative_to(cfg).as_posix()
            yield rel, p


def make_backup_folder_name():
    return f"obs-config-{hostname()}-{now_stamp()}"


def create_local_backup_zip(base_dir: Path, include_logs: bool, include_cache: bool) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    folder_name = make_backup_folder_name()
    zip_path = base_dir / f"{folder_name}.zip"
    with zipfile.ZipFile(str(zip_path), "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, src in iter_obs_files(include_logs, include_cache):
            with open(src, "rb") as f:
                data = f.read()
            data = _strip_stream_key(rel, data)
            zf.writestr(f"{folder_name}/obs-studio/{rel}", data)
        external_assets = collect_external_assets(include_logs, include_cache)
        if external_assets:
            manifest = []
            for asset in external_assets:
                backup_rel_path = asset["backup_path"]
                try:
                    with open(asset["source_path"], "rb") as f:
                        zf.writestr(f"{folder_name}/{backup_rel_path}", f.read())
                    manifest.append({
                        "original_path": asset["original_path"],
                        "backup_path": backup_rel_path,
                    })
                except Exception as e:
                    warn(f"Could not include external asset {asset['source_path']}: {e}")
            manifest_json = json.dumps(
                {"version": EXTERNAL_ASSETS_MANIFEST_VERSION, "source_host": hostname(), "files": manifest},
                indent=2,
            )
            zf.writestr(f"{folder_name}/{EXTERNAL_ASSETS_MANIFEST_FILE}", manifest_json)
    return zip_path


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def _iter_nested_strings(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _iter_nested_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_nested_strings(child)
    elif isinstance(value, str):
        yield value


def _looks_like_windows_absolute_path(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith("\\\\")


def _normalize_asset_path(raw_value: str):
    value = (raw_value or "").strip()
    if not value:
        return None
    if value.startswith("file://"):
        parsed = parse.urlparse(value)
        value = parse.unquote(parsed.path or "")
        if parsed.netloc:
            value = f"//{parsed.netloc}{value}"
        if sys.platform.startswith("win") and value.startswith("/") and _looks_like_windows_absolute_path(value[1:]):
            value = value[1:]
    value = os.path.expandvars(os.path.expanduser(value))
    candidate = Path(value)
    if not candidate.is_absolute():
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate.resolve()


def _backup_rel_for_external_asset(path: Path) -> str:
    """Map an absolute asset path to a stable backup-relative path."""
    raw = str(path)
    if _looks_like_windows_absolute_path(raw):
        flat = _windows_path_flat_parts(raw)
        if raw.startswith("\\\\"):
            server = flat[0] if len(flat) > 0 else "unc"
            share = flat[1] if len(flat) > 1 else "share"
            parts = flat[2:]
            rel = Path(EXTERNAL_ASSETS_DIR) / "windows-unc" / server / share
        else:
            drive = flat[0] if flat else "drive"
            parts = flat[1:]
            rel = Path(EXTERNAL_ASSETS_DIR) / "windows" / drive
        for part in parts:
            rel /= part
        return rel.as_posix()
    parts = [p for p in path.parts if p != "/"]
    rel = Path(EXTERNAL_ASSETS_DIR) / "posix"
    for part in parts:
        rel /= part
    return rel.as_posix()


def collect_external_assets(include_logs: bool, include_cache: bool):
    cfg = get_obs_config_dir().resolve()
    assets = []
    seen = set()
    scanned_json_files = 0
    for rel, src in iter_obs_files(include_logs, include_cache):
        rel_lower = rel.lower()
        if not rel_lower.endswith(".json"):
            continue
        scanned_json_files += 1
        if scanned_json_files % LOG_PROGRESS_INTERVAL == 0:
            info(f"Scanning JSON files for external assets... {scanned_json_files}")
        try:
            with open(src, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            warn(f"Skipping JSON asset scan for {src}: {e}")
            continue
        for value in _iter_nested_strings(payload):
            asset_path = _normalize_asset_path(value)
            if asset_path is None or _is_relative_to(asset_path, cfg):
                continue
            key = str(asset_path)
            if key in seen:
                continue
            seen.add(key)
            assets.append({
                "original_path": key,
                "backup_path": _backup_rel_for_external_asset(asset_path),
                "source_path": asset_path,
            })
    return assets


def write_external_assets_backup(backup_root: Path, assets):
    if not assets:
        return
    manifest = []
    for asset in assets:
        src = asset["source_path"]
        backup_rel_path = asset["backup_path"]
        dest = backup_root / backup_rel_path
        ensure_parent(dest)
        shutil.copy2(str(src), str(dest))
        manifest.append({
            "original_path": asset["original_path"],
            "backup_path": backup_rel_path,
        })
    manifest_path = backup_root / EXTERNAL_ASSETS_MANIFEST_FILE
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"version": EXTERNAL_ASSETS_MANIFEST_VERSION, "source_host": hostname(), "files": manifest}, f, indent=2)


def _target_path_from_manifest(original_path: str):
    if sys.platform.startswith("win"):
        if original_path.startswith("/"):
            return None
        if _looks_like_windows_absolute_path(original_path):
            return Path(PureWindowsPath(original_path))
        return Path(original_path)
    if _looks_like_windows_absolute_path(original_path):
        return None
    p = Path(original_path)
    if not p.is_absolute():
        return None
    return p


def _windows_path_flat_parts(raw: str) -> list:
    """Return all path components of a Windows absolute path as a flat list.

    For a drive path like ``C:\\Users\\Alice\\file.png`` this returns
    ``['C', 'Users', 'Alice', 'file.png']``.  For a UNC path like
    ``\\\\server\\share\\file.png`` it returns
    ``['server', 'share', 'file.png']``.
    """
    if raw.startswith("\\\\"):
        return [p for p in re.split(r"[\\/]+", raw.lstrip("\\")) if p]
    pure = PureWindowsPath(raw)
    drive = (pure.drive or "drive").replace(":", "")
    return [drive] + [p for p in pure.parts[1:] if p not in ("\\", "/")]


def _needs_repath(original_path: str) -> bool:
    """Return True if original_path is incompatible with the current OS (cross-platform restore)."""
    if sys.platform.startswith("win"):
        return original_path.startswith("/") and not _looks_like_windows_absolute_path(original_path)
    return _looks_like_windows_absolute_path(original_path)


def _fallback_asset_path(original_path: str, restore_dir: Path) -> Path:
    """Compute a mirrored path under restore_dir for an asset that cannot use its original location."""
    if _looks_like_windows_absolute_path(original_path):
        parts = _windows_path_flat_parts(original_path)
    else:
        parts = [p for p in Path(original_path).parts if p != "/"]
    result = restore_dir
    for part in parts:
        result /= part
    return result


def _path_to_file_url(path_str: str) -> str:
    """Convert an absolute path string to a file:// URL."""
    try:
        return Path(path_str).as_uri()
    except Exception:
        clean = path_str.replace("\\", "/")
        if not clean.startswith("/"):
            clean = "/" + clean
        return "file://" + parse.quote(clean)


def _repath_obs_json_files(obs_config_dir: Path, path_mapping: dict):
    """Rewrite absolute paths in all OBS JSON config files based on path_mapping.

    path_mapping maps original_path -> new_actual_path (both plain strings).
    Handles plain paths, Windows backslash-escaped paths, and file:// URL variants.
    """
    if not path_mapping:
        return
    replacements = []
    for old_path, new_path in path_mapping.items():
        old_json = json.dumps(old_path)[1:-1]
        new_json = json.dumps(new_path)[1:-1]
        replacements.append((old_json, new_json))
        old_url = _path_to_file_url(old_path)
        new_url = _path_to_file_url(new_path)
        old_url_json = json.dumps(old_url)[1:-1]
        new_url_json = json.dumps(new_url)[1:-1]
        if old_url_json != old_json:
            replacements.append((old_url_json, new_url_json))
        if _looks_like_windows_absolute_path(old_path):
            old_fwd = old_path.replace("\\", "/")
            new_fwd = new_path.replace("\\", "/")
            old_fwd_json = json.dumps(old_fwd)[1:-1]
            new_fwd_json = json.dumps(new_fwd)[1:-1]
            if old_fwd_json not in (old_json, old_url_json):
                replacements.append((old_fwd_json, new_fwd_json))
    repathed = 0
    for json_file in obs_config_dir.rglob("*.json"):
        try:
            text = json_file.read_text(encoding="utf-8")
        except Exception:
            continue
        modified = text
        for old_str, new_str in replacements:
            if old_str in modified:
                modified = modified.replace(old_str, new_str)
        if modified != text:
            try:
                json_file.write_text(modified, encoding="utf-8")
                repathed += 1
            except Exception as e:
                warn(f"Could not rewrite paths in {json_file}: {e}")
    if repathed > 0:
        info(f"Auto re-pathed {repathed} JSON file(s).")


def _strip_stream_key(rel_path: str, data_bytes: bytes) -> bytes:
    """Strip the stream key from service.json backup data so it is never stored."""
    if rel_path.replace("\\", "/") != "basic/service.json":
        return data_bytes
    try:
        obj = json.loads(data_bytes.decode("utf-8"))
        if isinstance(obj, dict):
            settings = obj.get("settings")
            if isinstance(settings, dict) and "key" in settings:
                del settings["key"]
                info("Stream key stripped from service.json backup.")
                return json.dumps(obj, indent=4, ensure_ascii=False).encode("utf-8")
    except Exception as e:
        warn(f"Could not strip stream key from service.json: {e}")
    return data_bytes


def _active_collection_name_from_backup(obs_config_root: Path):
    """Return the scene collection name that was active at backup time, from global.ini.

    Falls back to the filename stem of the first JSON file found in basic/scenes/.
    Returns None if no scene collection can be determined.
    """
    global_ini = obs_config_root / "global.ini"
    if global_ini.exists():
        try:
            cp = configparser.RawConfigParser(strict=False)
            # strict=False: OBS-generated INI files can contain duplicate keys in some
            # edge cases; lenient parsing avoids crashing on those rare files.
            cp.read(str(global_ini), encoding="utf-8-sig")
            # SceneCollectionFile is the on-disk stem; SceneCollection is the display name.
            # Try both so either OBS version works.
            name = (cp.get("Basic", "SceneCollectionFile", fallback=None) or
                    cp.get("Basic", "SceneCollection", fallback=None))
            if name and name.strip():
                return name.strip()
        except Exception as e:
            warn(f"Could not read SceneCollection from backup global.ini: {e}")
    scenes_dir = obs_config_root / "basic" / "scenes"
    if scenes_dir.exists():
        files = sorted(scenes_dir.glob("*.json"))
        if files:
            return files[0].stem
    return None


def _trigger_scene_reload(cfg: Path, collection_name: str, collection_json: bytes):
    """Force OBS to load the named scene collection from the restored files.

    When the currently active collection has the *same* name as the restored one,
    OBS would overwrite the restored file before loading it (it always saves the
    current collection on a collection-switch).  We work around this with a
    short-lived dummy collection so the restored file is never clobbered:

      1. Write a minimal dummy collection JSON to basic/scenes/__BAR_RESTORE_TEMP__.json
      2. Switch OBS to the dummy  →  OBS saves the stale in-memory state under
         collection_name.json *before* loading the dummy.
      3. Re-write the restored content to collection_name.json  (replaces the stale save).
      4. Switch OBS back to collection_name  →  OBS loads the just-written restored file.
      5. Delete the dummy file.

    For collections with a *different* name the simple one-step switch is enough
    (OBS saves the current collection under *its* name, which does not touch ours).
    """
    set_fn = getattr(obs, "obs_frontend_set_current_scene_collection", None)
    get_fn = getattr(obs, "obs_frontend_get_current_scene_collection", None)
    if set_fn is None or get_fn is None:
        warn("OBS frontend scene API not available; restart OBS to load the restored scene collection.")
        return

    try:
        current = get_fn()
        scenes_dir = cfg / "basic" / "scenes"

        if current != collection_name:
            # Different name: simple switch — OBS saves the current collection
            # under its own name (does not touch our restored file), then loads ours.
            set_fn(collection_name)
            info(f"Scene collection '{collection_name}' loaded from restored backup.")
            return

        # Same-name case: use the dummy-collection trick described above.
        temp_name = "__BAR_RESTORE_TEMP__"
        temp_file = scenes_dir / f"{temp_name}.json"
        try:
            ensure_parent(temp_file)
            with open(temp_file, "wb") as f:
                f.write(json.dumps({
                    # Minimal structure accepted by all OBS versions: a name and an
                    # empty sources array.  OBS fills in any missing fields itself.
                    "name": temp_name,
                    "current_program_scene": "",
                    "current_preview_scene": "",
                    "sources": [],
                }).encode("utf-8"))
            # Step 2: switch to dummy; OBS saves stale state to collection_name.json
            set_fn(temp_name)
            # Step 3: overwrite the just-saved stale file with our restored content
            target_file = scenes_dir / f"{collection_name}.json"
            with open(target_file, "wb") as f:
                f.write(collection_json)
            # Step 4: switch back; OBS loads our restored file
            set_fn(collection_name)
            info(f"Scene collection '{collection_name}' reloaded from restored backup.")
        finally:
            try:
                temp_file.unlink()
            except Exception:
                pass
    except Exception as e:
        warn(f"Could not reload scene collection via OBS API: {e}")


def _restore_external_assets_from_backup_root(backup_root: Path, restore_assets_dir: Path = None) -> dict:
    """Restore external assets from backup and return a mapping of original_path -> actual_restored_path.

    When the original path is incompatible with the current OS, assets are placed under
    restore_assets_dir (defaults to ~/obs-restored-assets) and the mapping is populated
    so that JSON config files can be re-pathed afterwards.
    """
    manifest_path = backup_root / EXTERNAL_ASSETS_MANIFEST_FILE
    if not manifest_path.exists():
        return {}
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        warn(f"Unable to read external assets manifest: {e}")
        return {}
    source_host = manifest.get("source_host")
    current_host = hostname()
    if source_host and source_host != current_host:
        warn(f"External asset restore is running on host '{current_host}', but the backup was created on host '{source_host}'. Paths may differ between machines.")
    if restore_assets_dir is None:
        restore_assets_dir = Path.home() / "obs-restored-assets"
    path_mapping = {}
    for asset in manifest.get("files", []):
        original_path = asset.get("original_path", "")
        backup_path = asset.get("backup_path", "")
        if not original_path or not backup_path:
            continue
        src = backup_root / backup_path
        if not src.exists() or not src.is_file():
            warn(f"Missing external asset in backup: {backup_path}")
            continue
        if _needs_repath(original_path):
            dest = _fallback_asset_path(original_path, restore_assets_dir)
            path_mapping[original_path] = str(dest)
        else:
            dest = _target_path_from_manifest(original_path)
            if dest is None:
                dest = _fallback_asset_path(original_path, restore_assets_dir)
                path_mapping[original_path] = str(dest)
        try:
            ensure_parent(dest)
            shutil.copy2(str(src), str(dest))
        except (OSError, shutil.Error) as e:
            warn(f"Could not restore external asset '{original_path}': {e}")
    return path_mapping


def g_get(settings_key, default=None):
    try:
        settings = obs.obs_data_create_from_json(obs.obs_data_get_json(obs.obs_data_create()))
        
    except Exception:
        pass
    
    return _shadow_settings.get(settings_key, default)


_shadow_settings = {}  


def g_set(k, v):
    _shadow_settings[k] = v


def g_get_str(k, default=""):
    v = g_get(k, default)
    return str(v) if v is not None else default


def g_get_bool(k, default=False):
    v = g_get(k, default)
    return bool(v)


def ensure_parent(p: Path):
    p.parent.mkdir(parents=True, exist_ok=True)


def _update_status(msg: str):
    """Log msg and, when the properties panel is open, update the status label."""
    info(msg)
    global g_props
    if g_props is not None:
        try:
            prop = obs.obs_properties_get(g_props, K_STATUS)
            if prop is not None:
                obs.obs_property_set_description(prop, msg)
        except Exception:
            pass


def _settings_from_shadow():
    try:
        s = obs.obs_data_create()
        obs.obs_data_set_string(s, K_LOCAL_DIR, g_get_str(K_LOCAL_DIR, str(Path.home() / "obs-backups")))
        obs.obs_data_set_bool(s, K_INCLUDE_LOGS, g_get_bool(K_INCLUDE_LOGS, False))
        obs.obs_data_set_bool(s, K_INCLUDE_CACHE, g_get_bool(K_INCLUDE_CACHE, False))
        return s
    except Exception:
        return obs.obs_data_create()




def do_backup_now(props=None, prop=None):
    try:
        include_logs = g_get_bool(K_INCLUDE_LOGS, False)
        include_cache = g_get_bool(K_INCLUDE_CACHE, False)
        g_set(K_INCLUDE_LOGS, include_logs)
        g_set(K_INCLUDE_CACHE, include_cache)

        target_dir = Path(g_get_str(K_LOCAL_DIR, str(Path.home() / "obs-backups")))
        _update_status("Backup in progress…")
        zip_path = create_local_backup_zip(target_dir, include_logs, include_cache)
        _update_status(f"Backup created: {zip_path}")
        return True
    except Exception as e:
        _update_status(f"Backup failed: {e}")
        err(f"Backup failed: {e}")
        return False


def _extract_zip_to_config(zip_file_path: Path):
    cfg = get_obs_config_dir()
    if not cfg.exists():
        cfg.mkdir(parents=True, exist_ok=True)
    backup_dir = cfg.parent / f"obs-studio.before-restore-{now_stamp()}"
    try:
        if cfg.exists():
            info(f"Backing up current config to {backup_dir}")
            if backup_dir.exists():
                shutil.rmtree(str(backup_dir), ignore_errors=True)
            shutil.copytree(str(cfg), str(backup_dir))
    except Exception as e:
        warn(f"Unable to backup previous state: {e}")

    # Capture the target scene collection JSON *inside* the temp-dir context so we
    # can reload OBS after the context closes and the temp dir is gone.
    target_collection_name = None
    target_collection_json = None

    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(str(zip_file_path), "r") as zf:
            zf.extractall(td)
        obs_config_root, backup_container_root = _resolve_backup_roots(Path(td))

        # Determine which collection to reload after the files land on disk.
        col_name = _active_collection_name_from_backup(obs_config_root)
        if col_name:
            col_file = obs_config_root / "basic" / "scenes" / f"{col_name}.json"
            if col_file.exists():
                try:
                    with open(col_file, "rb") as f:
                        target_collection_name = col_name
                        target_collection_json = f.read()
                except Exception as e:
                    warn(f"Could not read backup scene collection '{col_name}': {e}")

        info(f"Restoring to {cfg}")
        for item in obs_config_root.iterdir():
            dest = cfg / item.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(str(dest), ignore_errors=True)
                else:
                    try:
                        dest.unlink()
                    except Exception:
                        pass
            if item.is_dir():
                try:
                    shutil.copytree(str(item), str(dest), dirs_exist_ok=True)
                except TypeError:
                    # Python < 3.8 fallback: destination must not exist
                    try:
                        if dest.exists():
                            shutil.rmtree(str(dest))
                        shutil.copytree(str(item), str(dest))
                    except (OSError, shutil.Error) as e:
                        warn(f"Could not copy {item.name} to config dir: {e}")
            else:
                ensure_parent(dest)
                shutil.copy2(str(item), str(dest))
        path_mapping = _restore_external_assets_from_backup_root(backup_container_root)
        if path_mapping:
            _repath_obs_json_files(cfg, path_mapping)

    # Trigger OBS to load the restored scene collection immediately so the restored
    # files are not overwritten by OBS's stale in-memory state on the next shutdown.
    if target_collection_name and target_collection_json:
        _trigger_scene_reload(cfg, target_collection_name, target_collection_json)


def _backup_current_config():
    cfg = get_obs_config_dir()
    if not cfg.exists():
        return
    backup_dir = cfg.parent / f"obs-studio.before-restore-{now_stamp()}"
    try:
        info(f"Backing up current config to {backup_dir}")
        if backup_dir.exists():
            shutil.rmtree(str(backup_dir), ignore_errors=True)
        shutil.copytree(str(cfg), str(backup_dir))
    except Exception as e:
        warn(f"Unable to backup previous state: {e}")


def _restore_pairs_to_config(pairs):
    cfg = get_obs_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    _backup_current_config()
    for rel, data in pairs:
        dest = cfg / rel
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(str(dest), ignore_errors=True)
            else:
                try:
                    dest.unlink()
                except Exception:
                    pass
        ensure_parent(dest)
        with open(dest, "wb") as f:
            f.write(data)


def _find_nested_obs_studio(folder: Path, max_depth: int = 2):
    """Return the first nested obs-studio directory up to max_depth, or None."""
    root_depth = len(folder.parts)
    for root, dirs, _files in os.walk(folder):
        current = Path(root)
        depth = len(current.parts) - root_depth
        if current.name == "obs-studio":
            return current
        if depth >= max_depth:
            dirs[:] = []
    return None


def _resolve_backup_roots(folder: Path):
    if (folder / "obs-studio").exists():
        return folder / "obs-studio", folder
    if folder.name == "obs-studio":
        return folder, folder.parent
    nested = _find_nested_obs_studio(folder)
    if nested is not None:
        return nested, nested.parent
    raise RuntimeError("Invalid backup structure: could not find an obs-studio folder in the selected backup directory.")


def _restore_from_folder(folder: Path):
    if not folder.exists() or not folder.is_dir():
        raise RuntimeError("Invalid backup folder.")
    root, backup_root = _resolve_backup_roots(folder)

    # Capture target scene collection before writing to cfg (the source is stable here).
    col_name = _active_collection_name_from_backup(root)
    target_collection_name = None
    target_collection_json = None
    if col_name:
        col_file = root / "basic" / "scenes" / f"{col_name}.json"
        if col_file.exists():
            try:
                with open(col_file, "rb") as f:
                    target_collection_name = col_name
                    target_collection_json = f.read()
            except Exception as e:
                warn(f"Could not read backup scene collection '{col_name}': {e}")

    pairs = []
    start = Path(root)
    for p in start.rglob("*"):
        if p.is_file():
            rel = p.relative_to(start).as_posix()
            with open(p, "rb") as f:
                pairs.append((rel, f.read()))
    _restore_pairs_to_config(pairs)
    path_mapping = _restore_external_assets_from_backup_root(backup_root)
    if path_mapping:
        _repath_obs_json_files(get_obs_config_dir(), path_mapping)

    if target_collection_name and target_collection_json:
        _trigger_scene_reload(get_obs_config_dir(), target_collection_name, target_collection_json)


def do_restore_local(props=None, prop=None):
    try:
        path = g_get_str(K_RESTORE_LOCAL_PATH)
        if not path:
            raise RuntimeError("Select a .zip backup file.")
        p = Path(path)
        if not p.exists():
            raise RuntimeError("File not found.")
        if not p.is_file() or p.suffix.lower() != ".zip":
            raise RuntimeError("Please select a valid .zip backup file.")
        _update_status("Restore in progress…")
        _extract_zip_to_config(p)
        _update_status("Restore complete — scene collection reloaded. Restart OBS to apply remaining settings.")
        return True
    except Exception as e:
        _update_status(f"Restore failed: {e}")
        err(f"Local restore failed: {e}")
        return False


def script_description():
    return (
        "OBS Backup And Restore script - backup & restore OBS config files to/from a local folder.\n"
        "External source files referenced by OBS scene/config JSON files are also included when accessible on disk.\n\n"
        "BAR by celestial04_"
    )


def _set_prop_visible(props, name, visible: bool):
    pr = obs.obs_properties_get(props, name)
    if pr is not None:
        obs.obs_property_set_visible(pr, visible)


def _set_prop_enabled(props, name, enabled: bool):
    pr = obs.obs_properties_get(props, name)
    if pr is not None:
        try:
            obs.obs_property_set_enabled(pr, enabled)
        except Exception:
            pass


def script_properties():
    global g_props
    props = obs.obs_properties_create()

    obs.obs_properties_add_path(props, K_LOCAL_DIR, "Local backup folder", obs.OBS_PATH_DIRECTORY, "", str(Path.home()))

    obs.obs_properties_add_bool(props, K_INCLUDE_LOGS, "Include logs")
    obs.obs_properties_add_bool(props, K_INCLUDE_CACHE, "Include caches (larger)")

    obs.obs_properties_add_button(props, K_BACKUP_NOW, "Backup now", do_backup_now)

    obs.obs_properties_add_path(props, K_RESTORE_LOCAL_PATH, "Backup zip file", obs.OBS_PATH_FILE, "ZIP Files (*.zip)", str(Path.home()))
    obs.obs_properties_add_button(props, K_RESTORE_LOCAL_BTN, "Restore from zip", do_restore_local)

    try:
        obs.obs_properties_add_text(props, K_STATUS, "Ready", obs.OBS_TEXT_INFO)
    except Exception:
        obs.obs_properties_add_text(props, K_STATUS, "Ready", obs.OBS_TEXT_DEFAULT)

    g_props = props
    return props


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, K_LOCAL_DIR, str(Path.home() / "obs-backups"))
    obs.obs_data_set_default_bool(settings, K_INCLUDE_LOGS, False)
    obs.obs_data_set_default_bool(settings, K_INCLUDE_CACHE, False)


def script_update(settings):
    g_set(K_LOCAL_DIR, obs.obs_data_get_string(settings, K_LOCAL_DIR))
    g_set(K_INCLUDE_LOGS, obs.obs_data_get_bool(settings, K_INCLUDE_LOGS))
    g_set(K_INCLUDE_CACHE, obs.obs_data_get_bool(settings, K_INCLUDE_CACHE))
    g_set(K_RESTORE_LOCAL_PATH, obs.obs_data_get_string(settings, K_RESTORE_LOCAL_PATH))


def script_load(settings):
    info("OBS Backup & Restore script loaded.")


def script_unload():
    info("OBS Backup & Restore script unloaded.")
