# OBS Backup Restore And Restore (BAR)
# plugin for OBS Studio for backup & restore configs files on local disk or in a GitHub repo
# note: a GitHub token with repo access is required for GitHub backups/restores
# works well on Windows and Linux (not tested on macOS, but it should work)

import obspython as obs
import os
import sys
import platform
import time
import json
import base64
import tempfile
import zipfile
import shutil
import re
from pathlib import Path
from pathlib import PureWindowsPath
from urllib import request, parse, error

g_props = None

g_remote_files = []

K_DEST_TYPE = "dest_type"
K_LOCAL_DIR = "local_dir"
K_GH_TOKEN = "github_token"
K_GH_BRANCH = "github_branch"
K_GH_FOLDER = "github_folder"
K_GH_FETCH_REPOS = "github_fetch_repos"
K_GH_REPO_SELECT = "github_repo_select"
K_INCLUDE_LOGS = "include_logs"
K_INCLUDE_CACHE = "include_cache"
K_BACKUP_NOW = "backup_now"
K_STATUS = "status_text"
K_RESTORE_LOCAL_PATH = "restore_local_path"
K_RESTORE_LOCAL_BTN = "restore_local_btn"
K_REMOTE_REFRESH = "remote_refresh"
K_REMOTE_SELECT = "remote_select"
K_RESTORE_REMOTE_BTN = "restore_remote_btn"
EXTERNAL_ASSETS_DIR = "external-assets"
EXTERNAL_ASSETS_MANIFEST = "external-assets.json"

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


def create_local_backup_folder(base_dir: Path, include_logs: bool, include_cache: bool) -> Path:
    cfg = get_obs_config_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    backup_root = base_dir / make_backup_folder_name()
    target_root = backup_root / "obs-studio"
    for rel, src in iter_obs_files(include_logs, include_cache):
        dest = target_root / rel
        ensure_parent(dest)
        shutil.copy2(str(src), str(dest))
    external_assets = collect_external_assets(include_logs, include_cache)
    write_external_assets_backup(backup_root, external_assets)
    return backup_root


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
        if value.startswith("/") and _looks_like_windows_absolute_path(value[1:]):
            value = value[1:]
    value = os.path.expandvars(os.path.expanduser(value))
    candidate = Path(value)
    if not candidate.is_absolute():
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate.resolve()


def _backup_rel_for_external_asset(path: Path) -> str:
    raw = str(path)
    if _looks_like_windows_absolute_path(raw):
        pure = PureWindowsPath(raw)
        drive = (pure.drive or "drive").replace(":", "")
        parts = [p for p in pure.parts[1:] if p not in ("\\", "/")]
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
    for rel, src in iter_obs_files(include_logs, include_cache):
        if not rel.lower().endswith(".json"):
            continue
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
        rel = asset["backup_path"]
        dest = backup_root / rel
        ensure_parent(dest)
        shutil.copy2(str(src), str(dest))
        manifest.append({
            "original_path": asset["original_path"],
            "backup_path": rel,
        })
    manifest_path = backup_root / EXTERNAL_ASSETS_MANIFEST
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "files": manifest}, f, indent=2)


def _target_path_from_manifest(original_path: str):
    if sys.platform.startswith("win"):
        if original_path.startswith("/"):
            return None
        return Path(PureWindowsPath(original_path)) if _looks_like_windows_absolute_path(original_path) else Path(original_path)
    if _looks_like_windows_absolute_path(original_path):
        return None
    p = Path(original_path)
    if not p.is_absolute():
        return None
    return p


def _restore_external_assets_from_backup_root(backup_root: Path):
    manifest_path = backup_root / EXTERNAL_ASSETS_MANIFEST
    if not manifest_path.exists():
        return
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        warn(f"Unable to read external assets manifest: {e}")
        return
    for asset in manifest.get("files", []):
        original_path = asset.get("original_path", "")
        backup_path = asset.get("backup_path", "")
        if not original_path or not backup_path:
            continue
        src = backup_root / backup_path
        if not src.exists() or not src.is_file():
            warn(f"Missing external asset in backup: {backup_path}")
            continue
        dest = _target_path_from_manifest(original_path)
        if dest is None:
            warn(f"Skipping external asset for incompatible path: {original_path}")
            continue
        ensure_parent(dest)
        shutil.copy2(str(src), str(dest))


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


def _gh_join(*parts: str) -> str:
    segs = []
    for p in parts:
        if not p:
            continue
        s = str(p).strip("/")
        if s:
            segs.append(s)
    return "/".join(segs)


def _settings_from_shadow():
    try:
        s = obs.obs_data_create()
        obs.obs_data_set_string(s, K_DEST_TYPE, g_get_str(K_DEST_TYPE, "local"))
        obs.obs_data_set_string(s, K_LOCAL_DIR, g_get_str(K_LOCAL_DIR, str(Path.home() / "obs-backups")))
        obs.obs_data_set_bool(s, K_INCLUDE_LOGS, g_get_bool(K_INCLUDE_LOGS, False))
        obs.obs_data_set_bool(s, K_INCLUDE_CACHE, g_get_bool(K_INCLUDE_CACHE, False))
        obs.obs_data_set_string(s, K_GH_TOKEN, g_get_str(K_GH_TOKEN, ""))
        obs.obs_data_set_string(s, K_GH_REPO_SELECT, g_get_str(K_GH_REPO_SELECT, ""))
        obs.obs_data_set_string(s, K_GH_BRANCH, g_get_str(K_GH_BRANCH, "main"))
        obs.obs_data_set_string(s, K_GH_FOLDER, g_get_str(K_GH_FOLDER, "obs-backups"))
        return s
    except Exception:
        return obs.obs_data_create()






class GitHubClient:
    def __init__(self, token: str):
        self.token = token.strip()
        self.base = "https://api.github.com"

    def _headers(self):
        h = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "BAR",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def put_file(self, owner_repo: str, branch: str, path_in_repo: str, content_bytes: bytes, message: str):
        url = f"{self.base}/repos/{owner_repo}/contents/{parse.quote(path_in_repo)}"
        data = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode("utf-8"),
            "branch": branch or "main",
        }
        body = json.dumps(data).encode("utf-8")
        req = request.Request(url, data=body, headers=self._headers(), method="PUT")
        try:
            with request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                detail = str(e)
            raise RuntimeError(f"GitHub upload failed: {e.code} {detail}")

    def list_dir(self, owner_repo: str, branch: str, path_in_repo: str):
        quoted = parse.quote(path_in_repo) if path_in_repo else ""
        url = f"{self.base}/repos/{owner_repo}/contents"
        if quoted:
            url += f"/{quoted}"
        url += f"?ref={parse.quote(branch or 'main')}"
        req = request.Request(url, headers=self._headers())
        try:
            with request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list):
                    return data
                
                return [data]
        except error.HTTPError as e:
            if e.code == 404:
                return []
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                detail = str(e)
            raise RuntimeError(f"GitHub list failed: {e.code} {detail}")

    def get_file_content_b64(self, owner_repo: str, branch: str, path_in_repo: str):
        quoted = parse.quote(path_in_repo) if path_in_repo else ""
        url = f"{self.base}/repos/{owner_repo}/contents"
        if quoted:
            url += f"/{quoted}"
        url += f"?ref={parse.quote(branch or 'main')}"
        req = request.Request(url, headers=self._headers())
        try:
            with request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and data.get("encoding") == "base64":
                    return data.get("content", ""), data
                raise RuntimeError("Unexpected content response from GitHub")
        except error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8")
            except Exception:
                detail = str(e)
            raise RuntimeError(f"GitHub download failed: {e.code} {detail}")

    def list_user_repos(self):
        repos = []
        page = 1
        while True:
            url = f"{self.base}/user/repos?per_page=100&page={page}&type=all&sort=full_name"
            req = request.Request(url, headers=self._headers())
            try:
                with request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if not data:
                        break
                    repos.extend(data)
            except error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8")
                except Exception:
                    detail = str(e)
                raise RuntimeError(f"GitHub repo list failed: {e.code} {detail}")
            page += 1
        return repos






def do_backup_now(props=None, prop=None):
    try:
        dest_type = g_get_str(K_DEST_TYPE, "local")
        include_logs = g_get_bool(K_INCLUDE_LOGS, False)
        include_cache = g_get_bool(K_INCLUDE_CACHE, False)
        g_set(K_INCLUDE_LOGS, include_logs)
        g_set(K_INCLUDE_CACHE, include_cache)

        if dest_type == "local":
            target_dir = Path(g_get_str(K_LOCAL_DIR, str(Path.home() / "obs-backups")))
            info(f"Creating local backup in {target_dir}")
            backup_root = create_local_backup_folder(target_dir, include_logs, include_cache)
            info(f"Backup created: {backup_root}")
        else:
            token = g_get_str(K_GH_TOKEN)
            repo = g_get_str(K_GH_REPO_SELECT)
            branch = g_get_str(K_GH_BRANCH, "main")
            folder = g_get_str(K_GH_FOLDER, "obs-backups")
            if not token or not repo:
                raise RuntimeError("GitHub token and repo required.")
            client = GitHubClient(token)
            backup_name = make_backup_folder_name()
            base_in_repo = _gh_join(folder, backup_name, "obs-studio")
            count = 0
            for rel, src in iter_obs_files(include_logs, include_cache):
                repo_path = _gh_join(base_in_repo, rel)
                with open(src, "rb") as f:
                    data = f.read()
                client.put_file(repo, branch, repo_path, data, message=f"OBS backup {backup_name}: {rel}")
                count += 1
                if count % 50 == 0:
                    info(f"Uploading... {count} files")
            external_assets = collect_external_assets(include_logs, include_cache)
            if external_assets:
                manifest = []
                for asset in external_assets:
                    repo_path = _gh_join(folder, backup_name, asset["backup_path"])
                    with open(asset["source_path"], "rb") as f:
                        data = f.read()
                    client.put_file(repo, branch, repo_path, data, message=f"OBS backup {backup_name}: {asset['backup_path']}")
                    manifest.append({
                        "original_path": asset["original_path"],
                        "backup_path": asset["backup_path"],
                    })
                    count += 1
                manifest_bytes = json.dumps({"version": 1, "files": manifest}, indent=2).encode("utf-8")
                manifest_path = _gh_join(folder, backup_name, EXTERNAL_ASSETS_MANIFEST)
                client.put_file(repo, branch, manifest_path, manifest_bytes, message=f"OBS backup {backup_name}: {EXTERNAL_ASSETS_MANIFEST}")
                count += 1
            info(f"Upload complete. {count} files to {repo}/{base_in_repo}")
        return True
    except Exception as e:
        err(f"Backup failed: {e}")
        return False


def do_refresh_remote(props=None, prop=None):
    try:
        token = g_get_str(K_GH_TOKEN)
        repo = g_get_str(K_GH_REPO_SELECT)
        branch = g_get_str(K_GH_BRANCH, "main")
        folder = g_get_str(K_GH_FOLDER, "obs-backups")
        if not token or not repo:
            raise RuntimeError("GitHub token and repo required.")
        client = GitHubClient(token)
        items = client.list_dir(repo, branch, folder)
        
        files = [it for it in items if it.get("type") == "dir"]
        files.sort(key=lambda x: x.get("name", ""), reverse=True)
        global g_remote_files
        g_remote_files = files
        
        if g_props:
            p = obs.obs_properties_get(g_props, K_REMOTE_SELECT)
            if p is not None:
                obs.obs_property_list_clear(p)
                for i, it in enumerate(files):
                    obs.obs_property_list_add_string(p, it.get("name", f"dir-{i}"), it.get("path", ""))
        info(f"Remote backups (folders): {len(files)}")
        return True
    except Exception as e:
        err(f"Refresh failed: {e}")
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

    
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(str(zip_file_path), "r") as zf:
            zf.extractall(td)
        config_root, backup_root = _resolve_backup_roots(Path(td))
        info(f"Restoring to {cfg}")
        for item in config_root.iterdir():
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
                shutil.copytree(str(item), str(dest))
            else:
                ensure_parent(dest)
                shutil.copy2(str(item), str(dest))
        _restore_external_assets_from_backup_root(backup_root)


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


def _resolve_backup_roots(folder: Path):
    if (folder / "obs-studio").exists():
        return folder / "obs-studio", folder
    if folder.name == "obs-studio":
        return folder, folder.parent
    candidates = list(folder.glob("*/obs-studio"))
    if candidates:
        return candidates[0], candidates[0].parent
    raise RuntimeError("Invalid backup folder.")


def _restore_from_folder(folder: Path):
    if not folder.exists() or not folder.is_dir():
        raise RuntimeError("Invalid backup folder.")
    root, backup_root = _resolve_backup_roots(folder)
    pairs = []
    start = Path(root)
    for p in start.rglob("*"):
        if p.is_file():
            rel = p.relative_to(start).as_posix()
            with open(p, "rb") as f:
                pairs.append((rel, f.read()))
    _restore_pairs_to_config(pairs)
    _restore_external_assets_from_backup_root(backup_root)


def do_restore_local(props=None, prop=None):
    try:
        path = g_get_str(K_RESTORE_LOCAL_PATH)
        if not path:
            raise RuntimeError("Select a backup folder or a .zip.")
        p = Path(path)
        if not p.exists():
            raise RuntimeError("Path not found.")
        if p.is_file() and p.suffix.lower() == ".zip":
            _extract_zip_to_config(p)
        else:
            _restore_from_folder(p)
        info("Local restore complete. Restart OBS to fully apply.")
        return True
    except Exception as e:
        err(f"Local restore failed: {e}")
        return False


def do_restore_remote(props=None, prop=None):
    try:
        token = g_get_str(K_GH_TOKEN)
        repo = g_get_str(K_GH_REPO_SELECT)
        branch = g_get_str(K_GH_BRANCH, "main")
        sel_path = g_get_str(K_REMOTE_SELECT)
        if not token or not repo or not sel_path:
            raise RuntimeError("Token, repo, and selection required.")
        client = GitHubClient(token)
        
        base = sel_path.rstrip("/") + "/obs-studio"

        def _collect_pairs(root_path: str):
            pairs = []
            stack = [root_path]
            while stack:
                path = stack.pop()
                items = client.list_dir(repo, branch, path)
                for it in items:
                    t = it.get("type")
                    if t == "dir":
                        stack.append(it.get("path"))
                    elif t == "file":
                        content_b64, _ = client.get_file_content_b64(repo, branch, it.get("path"))
                        data = base64.b64decode(content_b64)
                        rel = it.get("path", "")[len(base.strip("/")) + 1:]
                        pairs.append((rel, data))
            return pairs

        pairs = _collect_pairs(base)
        if not pairs:
            raise RuntimeError("No files found in the remote backup.")
        _restore_pairs_to_config(pairs)
        manifest_path = sel_path.rstrip("/") + "/" + EXTERNAL_ASSETS_MANIFEST
        try:
            content_b64, _ = client.get_file_content_b64(repo, branch, manifest_path)
            manifest = json.loads(base64.b64decode(content_b64).decode("utf-8"))
            with tempfile.TemporaryDirectory() as td:
                backup_root = Path(td)
                for asset in manifest.get("files", []):
                    backup_path = asset.get("backup_path", "")
                    if not backup_path:
                        continue
                    asset_b64, _ = client.get_file_content_b64(repo, branch, sel_path.rstrip("/") + "/" + backup_path)
                    dest = backup_root / backup_path
                    ensure_parent(dest)
                    with open(dest, "wb") as f:
                        f.write(base64.b64decode(asset_b64))
                with open(backup_root / EXTERNAL_ASSETS_MANIFEST, "w", encoding="utf-8") as f:
                    json.dump(manifest, f)
                _restore_external_assets_from_backup_root(backup_root)
        except Exception as e:
            warn(f"Unable to restore external assets from remote backup: {e}")
        info("Remote restore complete. Restart OBS.")
        return True
    except Exception as e:
        err(f"Remote restore failed: {e}")
        return False






def script_description():
    return (
        "OBS Backup And Restore script can restore or backup from a local folder or from GitHub. (you will need token for GitHub backup/restore)\n"
        "External source files referenced by OBS scene/config JSON files are also included when accessible on disk.\n"
        "If you choose GitHub, you can restore a existing backup from a GitHub repo, so you will need to fetch present backups to select one.\n"
        "NOTE: you need to fetch accessible GitHub repos after entering a token to select one.\n\n"
        "BAR by celestial04_"
    )


def _on_dest_type_modified(props, p, settings):
    _refresh_visibility(props, settings)
    _refresh_enables(props, settings)
    return True


def _refresh_visibility(props, settings):
    dest = obs.obs_data_get_string(settings, K_DEST_TYPE)
    is_local = (dest == "local")
    _set_prop_visible(props, K_LOCAL_DIR, is_local)
    for k in (K_GH_TOKEN, K_GH_REPO_SELECT, K_GH_FETCH_REPOS, K_GH_BRANCH, K_GH_FOLDER, K_REMOTE_REFRESH, K_REMOTE_SELECT, K_RESTORE_REMOTE_BTN):
        _set_prop_visible(props, k, not is_local)
    _set_prop_visible(props, K_RESTORE_LOCAL_PATH, is_local)
    _set_prop_visible(props, K_RESTORE_LOCAL_BTN, is_local)
    _refresh_enables(props, settings)


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


def _refresh_enables(props, settings):
    dest = obs.obs_data_get_string(settings, K_DEST_TYPE)
    enable_backup = True
    if dest == "github":
        selected = obs.obs_data_get_string(settings, K_GH_REPO_SELECT)
        enable_backup = bool(selected)
    _set_prop_enabled(props, K_BACKUP_NOW, enable_backup)
    enable_remote = (dest == "github") and bool(obs.obs_data_get_string(settings, K_GH_REPO_SELECT))
    _set_prop_enabled(props, K_REMOTE_REFRESH, enable_remote)
    _set_prop_enabled(props, K_RESTORE_REMOTE_BTN, enable_remote)


def _on_fetch_repos(props, prop):
    try:
        token = g_get_str(K_GH_TOKEN)
        if not token:
            err("GitHub token required to fetch repos.")
            return True
        client = GitHubClient(token)
        repos = client.list_user_repos()
        lst = obs.obs_properties_get(props, K_GH_REPO_SELECT)
        if lst is not None:
            obs.obs_property_list_clear(lst)
            for r in sorted(repos, key=lambda x: x.get("full_name", "").lower()):
                full = r.get("full_name", "")
                if full:
                    obs.obs_property_list_add_string(lst, full, full)
        info(f"Loaded {len(repos)} repos.")
        try:
            obs.obs_property_set_enabled(obs.obs_properties_get(props, K_BACKUP_NOW), False)
        except Exception:
            pass
        try:
            obs.obs_property_set_enabled(obs.obs_properties_get(props, K_REMOTE_REFRESH), False)
            obs.obs_property_set_enabled(obs.obs_properties_get(props, K_RESTORE_REMOTE_BTN), False)
        except Exception:
            pass
        return True
    except Exception as e:
        err(f"Fetching repos failed: {e}")
        return True


def _on_repo_select_modified(props, p, settings):
    _refresh_enables(props, settings)
    return True


def script_properties():
    global g_props
    props = obs.obs_properties_create()

    p_dest = obs.obs_properties_add_list(
        props,
        K_DEST_TYPE,
        "Backup destination",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_list_add_string(p_dest, "Local folder", "local")
    obs.obs_property_list_add_string(p_dest, "GitHub (private)", "github")
    obs.obs_property_set_modified_callback(p_dest, _on_dest_type_modified)

    obs.obs_properties_add_path(props, K_LOCAL_DIR, "Local backup folder", obs.OBS_PATH_DIRECTORY, "", str(Path.home()))

    obs.obs_properties_add_bool(props, K_INCLUDE_LOGS, "Include logs")
    obs.obs_properties_add_bool(props, K_INCLUDE_CACHE, "Include caches (larger)")

    obs.obs_properties_add_text(props, K_GH_TOKEN, "GitHub Token (repo)", obs.OBS_TEXT_PASSWORD)
    obs.obs_properties_add_button(props, K_GH_FETCH_REPOS, "Fetch accessible repos", _on_fetch_repos)
    repo_list = obs.obs_properties_add_list(props, K_GH_REPO_SELECT, "Repository", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_set_modified_callback(repo_list, _on_repo_select_modified)
    obs.obs_properties_add_text(props, K_GH_BRANCH, "Branch", obs.OBS_TEXT_DEFAULT)
    obs.obs_properties_add_text(props, K_GH_FOLDER, "Folder in repo (optional)", obs.OBS_TEXT_DEFAULT)

    obs.obs_properties_add_button(props, K_BACKUP_NOW, "Backup now", do_backup_now)

    obs.obs_properties_add_path(props, K_RESTORE_LOCAL_PATH, "Backup folder/zip (local)", obs.OBS_PATH_DIRECTORY, "", str(Path.home()))
    obs.obs_properties_add_button(props, K_RESTORE_LOCAL_BTN, "Restore from local folder", do_restore_local)

    obs.obs_properties_add_button(props, K_REMOTE_REFRESH, "List remote backups", do_refresh_remote)
    obs.obs_properties_add_list(props, K_REMOTE_SELECT, "Remote backup", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_properties_add_button(props, K_RESTORE_REMOTE_BTN, "Restore from GitHub", do_restore_remote)

    g_props = props
    try:
        tmp = _settings_from_shadow()
        _refresh_visibility(props, tmp)
        _refresh_enables(props, tmp)
    except Exception:
        pass
    return props


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, K_DEST_TYPE, "local")
    obs.obs_data_set_default_string(settings, K_LOCAL_DIR, str(Path.home() / "obs-backups"))
    obs.obs_data_set_default_bool(settings, K_INCLUDE_LOGS, False)
    obs.obs_data_set_default_bool(settings, K_INCLUDE_CACHE, False)
    obs.obs_data_set_default_string(settings, K_GH_BRANCH, "main")
    obs.obs_data_set_default_string(settings, K_GH_FOLDER, "obs-backups")
    obs.obs_data_set_default_string(settings, K_GH_REPO_SELECT, "")


def script_update(settings):
    g_set(K_DEST_TYPE, obs.obs_data_get_string(settings, K_DEST_TYPE))
    g_set(K_LOCAL_DIR, obs.obs_data_get_string(settings, K_LOCAL_DIR))
    g_set(K_INCLUDE_LOGS, obs.obs_data_get_bool(settings, K_INCLUDE_LOGS))
    g_set(K_INCLUDE_CACHE, obs.obs_data_get_bool(settings, K_INCLUDE_CACHE))
    g_set(K_GH_TOKEN, obs.obs_data_get_string(settings, K_GH_TOKEN))
    g_set(K_GH_REPO_SELECT, obs.obs_data_get_string(settings, K_GH_REPO_SELECT))
    g_set(K_GH_BRANCH, obs.obs_data_get_string(settings, K_GH_BRANCH))
    g_set(K_GH_FOLDER, obs.obs_data_get_string(settings, K_GH_FOLDER))
    g_set(K_RESTORE_LOCAL_PATH, obs.obs_data_get_string(settings, K_RESTORE_LOCAL_PATH))

    if g_props is not None:
        _refresh_visibility(g_props, settings)
        _refresh_enables(g_props, settings)


def script_load(settings):
    info("OBS Backup & Restore script loaded.")
    try:
        if g_props is not None:
            _refresh_visibility(g_props, settings)
            _refresh_enables(g_props, settings)
        else:
            pass
    except Exception:
        pass


def script_unload():
    info("OBS Backup & Restore script unloaded.")
