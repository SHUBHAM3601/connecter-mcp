"""Connecter MCP server: tools for Design Connected's Connecter asset manager."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import journal
from .catalog import COMMANDS
from .client import ConnecterClient, ConnecterError
from .config import CONFIG_FILE, Settings
from .db import WorkspaceDb
from .files import MODEL_EXTS, FileIndex, classify_images, max_texture_paths

mcp = MCPServer(
    "connecter",
    instructions=(
        "Tools for Design Connected's Connecter asset manager. Tags are written as paths like "
        "'09_MISC/DOOR HANDLES'. Assets are referenced by full file path or GUID. Tools that move files "
        "or change many assets default to dry_run=True: show the user the plan, then call again with "
        "dry_run=False. Every change is journaled and can be reverted with `undo`."
    ),
)
client = ConnecterClient()
index = FileIndex()
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# ---------------------------------------------------------------- helpers

def _asset_ref(asset: str) -> dict:
    return {"AssetGuid": asset} if GUID_RE.match(asset.strip()) else {"AssetFullPath": asset}


def _tag_path(tag: str) -> list[str]:
    return [p.strip() for p in re.split(r"[/\\>]", tag) if p.strip()]


def _tags() -> list[dict]:
    return client.request("GetTags")["Tags"]


def _tag_guid(tag: str, tags: list[dict] | None = None) -> str:
    want = [p.lower() for p in _tag_path(tag)]
    for t in tags or _tags():
        if [p.lower() for p in t["TagPath"]] == want:
            return t["Guid"]
    raise ConnecterError(f"Tag '{tag}' not found. Use list_tags to see the tag tree.")


def _db() -> WorkspaceDb:
    if not client.paths.db_path or not client.paths.db_path.exists():
        client.refresh()
    if not client.paths.db_path or not client.paths.db_path.exists():
        raise ConnecterError("Connecter's workspace database was not found")
    return WorkspaceDb(client.paths.db_path)


def _get_asset(asset: str) -> dict:
    return client.request("GetAsset", _asset_ref(asset))["Asset"]


def _search_roots() -> list[Path]:
    roots = [Path(r["FullPath"]) for r in client.request("GetRootFolders")["RootFolders"]]
    return roots + Settings.load().search_dirs


def _backup(label: str) -> str | None:
    if client.paths.db_path and client.paths.db_path.exists():
        return str(journal.backup_workspace(client.paths.db_path, label))
    return None


# ---------------------------------------------------------------- system

@mcp.tool()
def connecter_status() -> dict:
    """Connecter's install, version, workspace, message folder, indexed root folders and asset counts."""
    client.refresh()
    p = client.paths
    status = {
        "running": client.is_running(),
        "version": p.version,
        "install_dir": str(p.install_dir) if p.install_dir else None,
        "workspace": str(p.workspace_dir) if p.workspace_dir else None,
        "message_folder": str(p.buffer_dir) if p.buffer_dir else None,
        "json_port": p.json_port,
        "config_file": str(CONFIG_FILE),
    }
    if status["running"]:
        status["root_folders"] = [r["FullPath"] for r in client.request("GetRootFolders")["RootFolders"]]
        status["tag_count"] = len(_tags())
    with _db() as db:
        assets = db.assets()
        status["catalogued_assets"] = len(assets)
        status["tagged_assets"] = sum(1 for a in assets if a.tags)
        status["assets_with_previews"] = sum(1 for a in assets if a.previews)
    return status


@mcp.tool()
def connecter_call(command: str, payload: dict | None = None, expect_reply: bool = True) -> dict:
    """Send any raw Connecter integration command (see list_commands) and return its reply payload.
    Use the dedicated tools when one exists; this is for commands they don't cover."""
    if not expect_reply:
        client.send(command, payload or {})
        return {"sent": command}
    return client.request(command, payload or {})


@mcp.tool()
def list_commands(group: str | None = None) -> dict:
    """List Connecter's integration commands and their request fields, optionally for one group."""
    if group:
        return {k: v for k, v in COMMANDS.items() if group.lower() in k.lower()}
    return COMMANDS


@mcp.tool()
def read_log(lines: int = 60, errors_only: bool = False) -> list[str]:
    """Readable lines from the end of Connecter's log (errors, watcher overflows, unreadable files)."""
    data = client.paths.log_path.read_bytes()
    text = re.sub(r"[^\x20-\x7e\n]+", " ", data.decode("latin-1"))
    out = [re.sub(r"\s{2,}", " ", line).strip() for line in re.split(r"\n|(?= [A-Z][A-Za-z<>_]{3,}[ \d]*(?:d__\d+)? )", text)]
    out = [line for line in out if len(line) > 12]
    if errors_only:
        out = [line for line in out if re.search(r"error|exception|could not|failed", line, re.I)]
    return out[-lines:]


@mcp.tool()
def restart_connecter(confirm: bool = False) -> dict:
    """Close Connecter gracefully and start it again from its normal working folder.
    Needs confirm=True. Use after a file-watcher overflow or when Connecter misbehaves."""
    if not confirm:
        return {"dry_run": True, "message": "Call again with confirm=True to restart Connecter."}
    if not _close_connecter():
        return {"restarted": False, "message": "Connecter did not close (a dialog may be open). Close it manually and retry."}
    return {"restarted": True, **_launch_connecter()}


def _close_connecter() -> bool:
    """Ask Connecter to close like clicking X; True once it has exited."""
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "$p=Get-Process Connecter -ErrorAction SilentlyContinue; if($p){[void]$p.CloseMainWindow(); $p.WaitForExit(45000)|Out-Null}"],
                   check=False, capture_output=True)
    return subprocess.run(["powershell", "-NoProfile", "-Command", "[bool](Get-Process Connecter -ErrorAction SilentlyContinue)"],
                          capture_output=True, text=True).stdout.strip() != "True"


def _launch_connecter() -> dict:
    """Start Connecter from its normal working folder (elsewhere, its 3ds Max integration check fails)."""
    exe = client.paths.exe
    if not exe or not exe.exists():
        raise ConnecterError("Connecter.exe not found")
    launch_dir = Settings.load().launch_dir or _shortcut_dir() or exe.parent
    subprocess.Popen([str(exe)], cwd=str(launch_dir), creationflags=subprocess.DETACHED_PROCESS)
    for _ in range(60):
        time.sleep(1)
        if client.is_running():
            return {"working_dir": str(launch_dir)}
    return {"working_dir": str(launch_dir), "message": "Started, but the API is not answering yet."}


def _shortcut_dir() -> Path | None:
    lnk = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Design Connected\Connecter.lnk"
    if not lnk.exists():
        return None
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').WorkingDirectory"],
                         capture_output=True, text=True).stdout.strip()
    return Path(out) if out and Path(out).exists() else None


# ---------------------------------------------------------------- assets

@mcp.tool()
def search_assets(query: str = "", tag: str = "", folder: str = "", untagged: bool = False,
                  no_previews: bool = False, extension: str = "", limit: int = 50) -> dict:
    """Search catalogued assets by words in the path/description, tag (includes sub-tags),
    folder, missing tags or previews, and file extension (e.g. '.max')."""
    words = [w.lower() for w in query.split()]
    tag_l, folder_l = tag.lower().strip("/"), folder.lower().rstrip("\\")
    with _db() as db:
        hits = []
        for a in db.assets():
            hay = f"{a.path} {a.description or ''}".lower()
            if words and not all(w in hay for w in words):
                continue
            if tag_l and not any(t.lower() == tag_l or t.lower().startswith(tag_l + "/") for t in a.tags):
                continue
            if folder_l and not a.path.lower().startswith(folder_l):
                continue
            if untagged and a.tags or no_previews and a.previews:
                continue
            if extension and not a.path.lower().endswith(extension.lower()):
                continue
            hits.append(a.to_dict())
    return {"total": len(hits), "assets": hits[:limit]}


@mcp.tool()
def get_asset(asset: str) -> dict:
    """Everything Connecter knows about one asset (path or GUID): tags, previews, description, custom properties."""
    info = _get_asset(asset)
    ref = {"AssetGuid": info["Guid"]}
    info["Description"] = client.request("GetAssetDescription", ref).get("Description")
    try:
        info["CustomProperties"] = client.request("GetCustomPropertiesForAsset", ref).get("CustomProperties")
    except ConnecterError:
        pass
    info["FileExists"] = Path(info["FileFullPath"]).exists()
    return info


@mcp.tool()
def catalogue_asset(asset_path: str, tags: list[str] | None = None, previews: list[str] | None = None,
                    description: str | None = None, auto_hide_previews: bool = True,
                    replace_tags: bool = False, replace_previews: bool = False) -> dict:
    """Add (or update) an asset in Connecter with tags, preview images and a description, the same way
    Connecter's own DCC plugins do. Works for files Connecter has not catalogued yet. Previews are
    hidden from the asset grid by default. Returns the asset as Connecter now reports it."""
    all_tags = _tags()
    guids = [_tag_guid(t, all_tags) for t in tags or []]
    before = None
    try:
        before = _get_asset(asset_path)
        before["Description"] = client.request("GetAssetDescription", {"AssetGuid": before["Guid"]}).get("Description")
    except ConnecterError:
        pass
    if before and replace_tags:
        client.request("UntagAssetFromAllTags", {"AssetGuid": before["Guid"]})
    payload = {"AssetFullPath": asset_path, "AutoHidePreviews": auto_hide_previews,
               "RemoveOldPreviews": replace_previews, "RemoveOldTags": replace_tags,
               "Description": description if description is not None else (before or {}).get("Description") or "",
               "Previews": [{"FullPath": p} for p in previews or []], "Tags": [{"Guid": g} for g in guids]}
    if not client.drop_message("CatalogueAsset", payload):
        raise ConnecterError("Connecter did not pick up the message. Is it running?")
    after = None
    for _ in range(20):
        time.sleep(0.5)
        try:
            after = _get_asset(asset_path)
            if all(g in {t["Guid"] for t in after["Tags"]} for g in guids):
                break
        except ConnecterError:
            continue
    if not after:
        raise ConnecterError("Message sent, but Connecter does not report the asset yet")
    added = [g for g in guids if not before or g not in {t["Guid"] for t in before["Tags"]}]
    undo = [{"type": "UntagAsset", "payload": {"AssetGuid": after["Guid"], "TagGuids": added}}] if added else []
    if before is not None and description is not None:
        undo.append({"type": "SetAssetDescription", "payload": {"AssetGuid": after["Guid"], "Description": before.get("Description") or ""}})
    if previews:
        undo.append({"type": "RemoveCustomPreviewsFromAsset", "payload": {"AssetGuid": after["Guid"], "PreviewFullPaths": previews}})
    jid = journal.record("catalogue_asset", {"asset": asset_path, "tags": tags, "previews": previews, "description": description}, undo)
    return {"journal_id": jid, "asset": after}


@mcp.tool()
def set_description(asset: str, description: str) -> dict:
    """Set an asset's description (searchable in Connecter)."""
    ref = _asset_ref(asset)
    old = client.request("GetAssetDescription", ref).get("Description") or ""
    client.request("SetAssetDescription", {**ref, "Description": description})
    jid = journal.record("set_description", {"asset": asset, "description": description},
                         [{"type": "SetAssetDescription", "payload": {**ref, "Description": old}}])
    return {"journal_id": jid, "old": old, "new": description}


@mcp.tool()
def tag_assets(assets: list[str], tag: str) -> dict:
    """Add one tag (e.g. '06_LIGHTS/WALL LIGHTS') to several assets (paths or GUIDs)."""
    guid = _tag_guid(tag)
    paths = [a for a in assets if not GUID_RE.match(a)]
    guids = [a for a in assets if GUID_RE.match(a)]
    r = client.request("TagAssets", {"TagGuid": guid, "AssetFullPaths": paths, "AssetGuids": guids})
    jid = journal.record("tag_assets", {"assets": assets, "tag": tag},
                         [{"type": "UntagAssets", "payload": {"TagGuid": guid, "AssetFullPaths": paths, "AssetGuids": guids}}])
    return {"journal_id": jid, "tagged": r.get("TaggedAssetsCount")}


@mcp.tool()
def untag_assets(assets: list[str], tag: str) -> dict:
    """Remove one tag from several assets."""
    guid = _tag_guid(tag)
    paths = [a for a in assets if not GUID_RE.match(a)]
    guids = [a for a in assets if GUID_RE.match(a)]
    r = client.request("UntagAssets", {"TagGuid": guid, "AssetFullPaths": paths, "AssetGuids": guids})
    jid = journal.record("untag_assets", {"assets": assets, "tag": tag},
                         [{"type": "TagAssets", "payload": {"TagGuid": guid, "AssetFullPaths": paths, "AssetGuids": guids}}])
    return {"journal_id": jid, "untagged": r.get("UntaggedAssetsCount")}


@mcp.tool()
def set_previews(asset: str, images: list[str], replace: bool = False, auto_hide: bool = True) -> dict:
    """Attach preview images to an asset (first image becomes the thumbnail when replace=True).
    Source images are hidden from the asset grid unless auto_hide=False."""
    info = _get_asset(asset)
    payload = {"AssetFullPath": info["FileFullPath"], "Previews": [{"FullPath": i} for i in images],
               "RemoveOldPreviews": replace, "AutoHidePreviews": auto_hide}
    if not client.drop_message("AddPreviewsToAsset", payload):
        raise ConnecterError("Connecter did not pick up the message")
    time.sleep(1.5)
    after = client.request("GetAssetCustomPreviews", {"AssetGuid": info["Guid"]})["CustomPreviews"]
    jid = journal.record("set_previews", {"asset": asset, "images": images, "replace": replace},
                         [{"type": "RemoveCustomPreviewsFromAsset", "payload": {"AssetGuid": info["Guid"], "PreviewFullPaths": images}}])
    return {"journal_id": jid, "previews": after}


@mcp.tool()
def remove_previews(asset: str, images: list[str]) -> dict:
    """Detach preview images (by their original file path or preview GUID) from an asset."""
    guids = [i for i in images if GUID_RE.match(i)]
    paths = [i for i in images if not GUID_RE.match(i)]
    r = client.request("RemoveCustomPreviewsFromAsset", {**_asset_ref(asset), "PreviewGuids": guids, "PreviewFullPaths": paths})
    journal.record("remove_previews", {"asset": asset, "images": images})
    return r


@mcp.tool()
def show_in_connecter(assets: list[str], isolate: bool = False) -> dict:
    """Bring Connecter to the front and select one asset, or isolate several in the grid."""
    if isolate or len(assets) > 1:
        return client.request("IsolateView", {"AssetFullPaths": assets, "ActivateMainWindow": True})
    return client.request("LocateAsset", {"AssetFullPath": assets[0], "ActivateMainWindow": True})


@mcp.tool()
def asset_properties(asset: str, set_name: str | None = None, set_value: str | None = None) -> dict:
    """Read an asset's custom properties, or set one (creates it on the asset if missing).
    The property definition must exist in Connecter (create it in Connecter or via connecter_call)."""
    ref = _asset_ref(asset)
    if set_name:
        client.request("SetAssetCustomPropertyValue", {**ref, "DefinitionName": set_name, "Value": set_value,
                                                       "AddNewPropertyToAssetIfNotFound": True})
        journal.record("asset_properties", {"asset": asset, "name": set_name, "value": set_value})
    return client.request("GetCustomPropertiesForAsset", ref)


@mcp.tool()
def asset_versions(asset: str) -> dict:
    """Version-control history of an asset, if Connecter's versioning is used."""
    return client.request("GetAssetVersions", _asset_ref(asset))


# ---------------------------------------------------------------- tags

@mcp.tool()
def list_tags(filter: str = "") -> list[dict]:
    """The tag tree as paths with GUIDs and asset counts, optionally filtered by text."""
    with _db() as db:
        counts: dict[str, int] = {}
        for a in db.assets():
            for t in a.tags:
                counts[t] = counts.get(t, 0) + 1
    out = []
    for t in _tags():
        path = "/".join(t["TagPath"])
        if filter.lower() in path.lower():
            out.append({"tag": path, "guid": t["Guid"], "assets": counts.get(path, 0)})
    return sorted(out, key=lambda r: r["tag"])


@mcp.tool()
def create_tag(tag: str) -> dict:
    """Create a tag from a path, e.g. '09_MISC/LANDSCAPE' creates LANDSCAPE under 09_MISC."""
    parts = _tag_path(tag)
    payload = {"Name": parts[-1]}
    if len(parts) > 1:
        payload["ParentTagGuid"] = _tag_guid("/".join(parts[:-1]))
    t = client.request("CreateTag", payload)["Tag"]
    jid = journal.record("create_tag", {"tag": tag}, [{"type": "DeleteTag", "payload": {"TagGuid": t["Guid"]}}])
    return {"journal_id": jid, "tag": t}


@mcp.tool()
def rename_tag(tag: str, new_name: str) -> dict:
    """Rename a tag (keeps its assets)."""
    guid = _tag_guid(tag)
    client.request("RenameTag", {"TagGuid": guid, "NewName": new_name})
    jid = journal.record("rename_tag", {"tag": tag, "new_name": new_name},
                         [{"type": "RenameTag", "payload": {"TagGuid": guid, "NewName": _tag_path(tag)[-1]}}])
    return {"journal_id": jid, "renamed": True}


@mcp.tool()
def move_tag(tag: str, new_parent: str = "") -> dict:
    """Move a tag under another tag ('' moves it to the top level)."""
    tags = _tags()
    guid = _tag_guid(tag, tags)
    old_parent = next(t["ParentGuid"] for t in tags if t["Guid"] == guid)
    new_guid = _tag_guid(new_parent, tags) if new_parent else None
    client.request("ChangeTagParent", {"TagGuid": guid, "NewParentTagGuid": new_guid})
    jid = journal.record("move_tag", {"tag": tag, "new_parent": new_parent},
                         [{"type": "ChangeTagParent", "payload": {"TagGuid": guid, "NewParentTagGuid": old_parent}}])
    return {"journal_id": jid, "moved": True}


@mcp.tool()
def delete_tag(tag: str, confirm: bool = False) -> dict:
    """Delete a tag. Its assets stay in the library but lose this tag. Needs confirm=True.
    The assets it held are journaled so `undo` can recreate the tag and re-tag them."""
    guid = _tag_guid(tag)
    held = client.request("GetAssetsByTags", {"TagGuids": [guid], "IncludeAssetsFromSubtags": False, "MaxAssetsCount": 100000})["Assets"]
    if not confirm:
        return {"dry_run": True, "tag": tag, "assets_affected": len(held), "message": "Call again with confirm=True."}
    _backup("delete-tag")
    client.request("DeleteTag", {"TagGuid": guid})
    jid = journal.record("delete_tag", {"tag": tag, "assets": [a["FileFullPath"] for a in held]},
                         [{"type": "_recreate_tag", "payload": {"tag": tag, "assets": [a["FileFullPath"] for a in held]}}])
    return {"journal_id": jid, "deleted": tag, "assets_untagged": len(held)}


@mcp.tool()
def set_tag_color(tag: str, color: str | None = None, apply_to_subtags: bool = False) -> dict:
    """Set a tag's colour as '#RRGGBB' (or None to remove it)."""
    guid = _tag_guid(tag)
    if color is None:
        return client.request("RemoveTagColor", {"TagGuid": guid})
    argb = color if len(color) == 9 else "#FF" + color.lstrip("#")
    return client.request("ChangeTagColor", {"TagGuid": guid, "Color": argb, "ApplyOnSubtags": apply_to_subtags})


# ---------------------------------------------------------------- files and folders

@mcp.tool()
def move_asset(asset: str, to_folder: str, new_file_name: str | None = None) -> dict:
    """Move an asset file through Connecter so its tags, previews and description follow it."""
    info = _get_asset(asset)
    payload = {"AssetGuid": info["Guid"], "FolderFullPath": to_folder}
    if new_file_name:
        payload["NewAssetFileName"] = new_file_name
    r = client.request("MoveAssetInFileSystem", payload)
    old = Path(info["FileFullPath"])
    jid = journal.record("move_asset", {"from": str(old), "to": r.get("NewAssetFullPath")},
                         [{"type": "MoveAssetInFileSystem", "payload": {"AssetGuid": info["Guid"], "FolderFullPath": str(old.parent), "NewAssetFileName": old.name}}])
    return {"journal_id": jid, **r}


@mcp.tool()
def rename_asset(asset: str, new_name: str) -> dict:
    """Rename an asset file through Connecter (keeps its catalogue data). new_name without folder."""
    info = _get_asset(asset)
    r = client.request("RenameAssetInFileSystem", {"AssetGuid": info["Guid"], "NewAssetName": new_name})
    jid = journal.record("rename_asset", {"asset": info["FileFullPath"], "new_name": new_name},
                         [{"type": "RenameAssetInFileSystem", "payload": {"AssetGuid": info["Guid"], "NewAssetName": Path(info["FileFullPath"]).stem}}])
    return {"journal_id": jid, **r}


@mcp.tool()
def folder_operation(operation: str, folder: str, target: str = "", confirm: bool = False) -> dict:
    """Folder operations through Connecter so catalogue data follows: operation is 'create', 'rename'
    (target = new name), 'move' (target = new parent folder), 'copy' (target = parent folder) or
    'delete' (to the recycle bin; needs confirm=True)."""
    op = operation.lower()
    if op == "create":
        r = client.request("CreateFolderInFileSystem", {"FolderFullPath": folder})
    elif op == "rename":
        r = client.request("RenameFolderInFileSystem", {"FolderFullPath": folder, "NewName": target})
    elif op == "move":
        r = client.request("MoveFolderInFileSystem", {"FolderFullPath": folder, "NewParentFolderFullPath": target})
    elif op == "copy":
        r = client.request("CopyFolderInFileSystem", {"FolderFullPath": folder, "ParentFolderFullPath": target, "CopyCatalogedData": True})
    elif op == "delete":
        if not confirm:
            return {"dry_run": True, "message": f"Call again with confirm=True to move '{folder}' to the recycle bin."}
        _backup("delete-folder")
        r = client.request("DeleteFolderFromFileSystem", {"FolderFullPath": folder, "MoveToRecycleBinIfPossible": True})
    else:
        raise ConnecterError("operation must be create, rename, move, copy or delete")
    jid = journal.record("folder_operation", {"operation": op, "folder": folder, "target": target})
    return {"journal_id": jid, **r}


# ---------------------------------------------------------------- library ingestion

@mcp.tool()
def library_categories() -> dict:
    """The library's category folders (from models_dir in the config) and the tag tree, so you can
    choose where a new model belongs."""
    s = Settings.load()
    cats = []
    if s.models_dir and s.models_dir.exists():
        for c in sorted(p for p in s.models_dir.iterdir() if p.is_dir()):
            subs = sorted(p.name for p in c.iterdir() if p.is_dir() and not any(f.suffix.lower() in MODEL_EXTS for f in p.iterdir() if f.is_file()))
            cats.append({"category": c.name, "subcategories": subs})
    return {"models_dir": str(s.models_dir) if s.models_dir else None, "categories": cats,
            "tags": [t["tag"] for t in list_tags()],
            "note": None if s.models_dir else f"Set models_dir in {CONFIG_FILE} to enable categories."}


@mcp.tool()
def inspect_folder(folder: str) -> dict:
    """Look inside a downloaded model folder: model files, which images are previews vs texture maps,
    and the textures each .max uses (with missing ones flagged)."""
    root = Path(folder)
    models = [str(p) for p in root.rglob("*") if p.suffix.lower() in MODEL_EXTS]
    images = classify_images(root)
    textures = {}
    for m in root.rglob("*.max"):
        textures[str(m)] = [_texture_status(t, m) for t in max_texture_paths(m)]
    return {"models": models, **images, "max_textures": textures}


@mcp.tool()
def ingest_model(source_folder: str, category: str, model_name: str | None = None, tag: str | None = None,
                 description: str | None = None, move: bool = True, dry_run: bool = True) -> dict:
    """Bring a downloaded model into the library in one step: move (or copy) the folder to
    models_dir/<category>/<model_name>, then catalogue every model file in it with the tag
    (defaults to the category path) and the preview images (texture maps are left alone).
    category is a path under models_dir, e.g. '09_MISC' or '06_LIGHTS/WALL LIGHTS'.
    dry_run=True (default) only returns the plan."""
    s = Settings.load()
    if not s.models_dir:
        raise ConnecterError(f"Set models_dir in {CONFIG_FILE} first")
    src = Path(source_folder)
    if not src.is_dir():
        raise ConnecterError(f"{src} is not a folder")
    name = model_name or re.sub(r"^(pro\s*3dsky|3dsky|3dline\.net|cgtrader)\s*[-_]\s*", "", src.name, flags=re.I).strip()
    dest = s.models_dir.joinpath(*_tag_path(category)) / name
    tag = tag or "/".join(_tag_path(category))
    images = classify_images(src)
    models = sorted(p.relative_to(src) for p in src.rglob("*") if p.suffix.lower() in MODEL_EXTS)
    catalogue = [m for m in models if m.suffix.lower() == ".max"] or models
    tag_ok = True
    try:
        _tag_guid(tag)
    except ConnecterError:
        tag_ok = False
    plan = {"from": str(src), "to": str(dest), "action": "move" if move else "copy", "tag": tag,
            "tag_exists": tag_ok, "catalogue": [str(dest / m) for m in catalogue],
            "previews": [str(dest / Path(p["path"]).relative_to(src)) for p in images["previews"]],
            "textures_left_alone": [Path(p["path"]).name for p in images["textures"]]}
    if dest.exists():
        plan["warning"] = "Destination already exists; files will be merged into it."
    if dry_run:
        return {"dry_run": True, **plan}
    if not tag_ok:
        create_tag(tag)
    if move:
        shutil.copytree(src, dest, dirs_exist_ok=True)
        shutil.rmtree(src)
    else:
        shutil.copytree(src, dest, dirs_exist_ok=True)
    results = []
    for m in plan["catalogue"]:
        results.append(catalogue_asset(m, tags=[tag], previews=plan["previews"], description=description))
    jid = journal.record("ingest_model", plan)
    return {"journal_id": jid, **plan, "catalogued": [r["asset"]["FileFullPath"] for r in results]}


# ---------------------------------------------------------------- health

@mcp.tool()
def audit_library(include_textures: bool = False, limit: int = 30) -> dict:
    """Library health report: untagged assets, assets without previews, missing files, unused preview
    images, and (include_textures=True, slower) .max files with missing texture maps."""
    with _db() as db:
        assets = db.assets()
        stored = db.preview_files()
    models = [a for a in assets if Path(a.path).suffix.lower() in MODEL_EXTS]
    missing = [a.path for a in assets if not Path(a.path).exists()]
    report = {
        "catalogued": len(assets),
        "models": len(models),
        "untagged_models": [a.path for a in models if not a.tags][:limit],
        "untagged_models_count": sum(1 for a in models if not a.tags),
        "models_without_previews_count": sum(1 for a in models if not a.previews),
        "missing_files": missing[:limit],
        "missing_files_count": len(missing),
    }
    pdir = client.paths.previews_dir
    if pdir and pdir.exists():
        used = {local.lower() for _, local, _ in stored}
        orphans = [p.name for p in pdir.iterdir() if p.is_file() and p.name.lower() not in used]
        report["orphan_preview_images_count"] = len(orphans)
    if include_textures:
        broken = {}
        for a in models:
            if a.path.lower().endswith(".max") and Path(a.path).exists():
                bad = [t["path"] for t in (_texture_status(p, Path(a.path)) for p in max_texture_paths(Path(a.path))) if t["status"] == "missing"]
                if bad:
                    broken[a.path] = bad
        report["models_with_missing_textures_count"] = len(broken)
        report["models_with_missing_textures"] = dict(list(broken.items())[:limit])
    return report


@mcp.tool()
def find_missing_assets(folder: str = "", find_candidates: bool = True) -> dict:
    """Catalogued assets whose file is gone (moved, renamed or deleted outside Connecter), with
    likely new locations found by name across the root folders and configured search_dirs."""
    with _db() as db:
        missing = [a for a in db.assets() if not Path(a.path).exists() and a.path.lower().startswith(folder.lower())]
    roots = _search_roots() if find_candidates else []
    out = []
    for a in missing:
        entry = {**a.to_dict()}
        if find_candidates:
            # Rank every same-named file before trimming, or the right copy can be cut off.
            entry["candidates"] = _rank_by_folders(a.path, index.find(roots, Path(a.path).name, limit=200))[:8]
        out.append(entry)
    return {"missing_count": len(out), "assets": out}


def _rank_by_folders(original: str, candidates: list[dict]) -> list[dict]:
    """Break ties between same-named files: prefer the one whose folder names match the old path's."""
    old_dirs = [p.lower() for p in Path(original).parent.parts]
    for c in candidates:
        new_dirs = [p.lower() for p in Path(c["path"]).parent.parts]
        shared = 0
        while shared < min(len(old_dirs), len(new_dirs)) and old_dirs[-1 - shared] == new_dirs[-1 - shared]:
            shared += 1
        c["confidence"] = round(min(0.99, c["confidence"] + 0.01 * shared), 2)
        c["matching_folders"] = shared
    return sorted(candidates, key=lambda c: -c["confidence"])


@mcp.tool()
def relocate_asset(asset: str, new_path: str) -> dict:
    """Point a missing (or moved) catalogue entry at the file's new location, keeping its tags,
    previews and description. Does not move any file."""
    if not Path(new_path).exists():
        raise ConnecterError(f"{new_path} does not exist")
    info = _get_asset(asset)
    # Connecter rejects a GUID-only RelocateAsset for missing files ("extension doesn't match"); it needs the old path.
    client.request("RelocateAsset", {"AssetFullPath": info["FileFullPath"], "NewAssetFullPath": new_path})
    jid = journal.record("relocate_asset", {"from": info["FileFullPath"], "to": new_path},
                         [{"type": "RelocateAsset", "payload": {"AssetFullPath": new_path, "NewAssetFullPath": info["FileFullPath"]}}])
    return {"journal_id": jid, "relocated": info["FileFullPath"], "to": new_path}


@mcp.tool()
def batch_relocate(min_confidence: float = 0.9, folder: str = "", dry_run: bool = True) -> dict:
    """Relocate every missing asset whose best candidate is unambiguous and at least min_confidence.
    dry_run=True (default) only lists what would change and what needs a manual decision."""
    found = find_missing_assets(folder)
    apply, review = [], []
    for a in found["assets"]:
        c = a.get("candidates") or []
        if c and c[0]["confidence"] >= min_confidence and (len(c) == 1 or c[1]["confidence"] < c[0]["confidence"]):
            apply.append({"asset": a["path"], "to": c[0]["path"], "match": c[0]["match"]})
        else:
            review.append({"asset": a["path"], "candidates": c[:3]})
    if dry_run:
        return {"dry_run": True, "would_relocate": apply, "needs_review": review}
    _backup("batch-relocate")
    done = [relocate_asset(x["asset"], x["to"]) for x in apply]
    return {"relocated": len(done), "needs_review": review}


# ---------------------------------------------------------------- textures

def _texture_status(path: str, max_file: Path) -> dict:
    p = Path(path)
    if p.is_absolute() and p.exists():
        return {"path": path, "status": "ok", "resolved": path}
    # 3ds Max also finds bitmaps by name next to the scene file and in its sub-folders.
    for cand in [max_file.parent / p.name, *max_file.parent.rglob(p.name)]:
        if cand.exists():
            return {"path": path, "status": "ok" if not p.is_absolute() else "found_by_name", "resolved": str(cand)}
    return {"path": path, "status": "missing", "resolved": None}


@mcp.tool()
def scan_textures(asset: str = "", folder: str = "", tag: str = "", only_missing: bool = True, limit: int = 100) -> dict:
    """Texture maps used by .max assets (one asset, a folder, or a tag) and whether each resolves.
    'found_by_name' means the stored path is wrong but 3ds Max will still find the file next to the scene."""
    if asset:
        files = [Path(_get_asset(asset)["FileFullPath"]) if GUID_RE.match(asset) else Path(asset)]
    else:
        files = [Path(a["path"]) for a in search_assets(folder=folder, tag=tag, extension=".max", limit=100000)["assets"]]
    out = {}
    for f in files[:limit] if not asset else files:
        if not f.exists():
            continue
        maps = [_texture_status(t, f) for t in max_texture_paths(f)]
        if only_missing:
            maps = [m for m in maps if m["status"] == "missing"]
        if maps or not only_missing:
            out[str(f)] = maps
    return {"files_scanned": min(len(files), limit), "files_with_issues": len(out), "results": out}


@mcp.tool()
def find_texture_candidates(asset: str, extra_folders: list[str] | None = None) -> dict:
    """For each missing map of a .max asset, search the asset folder, the library roots, search_dirs and
    extra_folders by exact name, other extensions and similar names. Returns ranked candidates."""
    max_file = Path(_get_asset(asset)["FileFullPath"]) if GUID_RE.match(asset) else Path(asset)
    roots = [max_file.parent, *_search_roots(), *[Path(p) for p in extra_folders or []]]
    result = {}
    for m in (_texture_status(t, max_file) for t in max_texture_paths(max_file)):
        if m["status"] == "missing":
            result[m["path"]] = index.find(roots, Path(m["path"]).name)
    return {"asset": str(max_file), "missing": result}


@mcp.tool()
def localize_textures(asset: str, choices: dict[str, str] | None = None, min_confidence: float = 0.9,
                      dry_run: bool = True) -> dict:
    """Fix missing maps without opening 3ds Max: copy the found file next to the .max (3ds Max resolves
    bitmaps by name from the scene's folder). `choices` maps a missing path to the file to use; otherwise
    the best candidate at or above min_confidence is used. dry_run=True (default) only shows the plan."""
    found = find_texture_candidates(asset)
    max_file = Path(found["asset"])
    plan, skipped = [], []
    for missing, cands in found["missing"].items():
        src = (choices or {}).get(missing) or (cands[0]["path"] if cands and cands[0]["confidence"] >= min_confidence else None)
        if not src:
            skipped.append({"missing": missing, "candidates": cands[:3]})
            continue
        plan.append({"missing": missing, "copy_from": src, "copy_to": str(max_file.parent / Path(missing).name)})
    if dry_run:
        return {"dry_run": True, "copy": plan, "unresolved": skipped}
    for p in plan:
        if not Path(p["copy_to"]).exists():
            shutil.copy2(p["copy_from"], p["copy_to"])
    jid = journal.record("localize_textures", {"asset": str(max_file), "copied": plan})
    return {"journal_id": jid, "copied": plan, "unresolved": skipped}


@mcp.tool()
def relink_script(asset: str, choices: dict[str, str] | None = None, make_relative: bool = True) -> dict:
    """MaxScript that rewrites a .max file's missing bitmap paths to the found files (or, with
    make_relative, to bare file names - run localize_textures first so they sit next to the scene).
    Run it in 3ds Max or with a 3ds Max MCP; it saves the file."""
    found = find_texture_candidates(asset)
    lines = [f'loadMaxFile @"{found["asset"]}" quiet:true useFileUnits:true',
             "for b in getClassInstances BitmapTexture where b.filename != undefined do ("]
    mapping, unresolved = {}, []
    for missing, cands in found["missing"].items():
        source = (choices or {}).get(missing) or (cands[0]["path"] if cands else None)
        if not source:
            unresolved.append(missing)
            continue
        target = Path(source).name if make_relative else source
        mapping[missing] = target
        lines.append(f'  if (toLower b.filename) == (toLower @"{missing}") do b.filename = @"{target}"')
    lines += [")", f'saveMaxFile @"{found["asset"]}" quiet:true']
    return {"asset": found["asset"], "relinks": mapping, "unresolved": unresolved, "maxscript": "\n".join(lines)}


# ---------------------------------------------------------------- safety

@mcp.tool()
def backup_workspace(label: str = "manual") -> dict:
    """Snapshot Connecter's workspace database to ~/.connecter-mcp/backups."""
    return {"backup": _backup(label), "all_backups": journal.list_backups()[:10]}


@mcp.tool()
def change_journal(limit: int = 20) -> list[dict]:
    """Recent changes made through this MCP, newest first, with their journal IDs for `undo`."""
    return [{k: e[k] for k in ("id", "time", "action", "details", "undone")} | {"undoable": bool(e["undo"])}
            for e in reversed(journal.entries()[-limit:])]


@mcp.tool()
def undo(journal_id: str) -> dict:
    """Reverse a change recorded in the journal (tags, descriptions, previews, moves, relocations, tag edits)."""
    entry = next((e for e in journal.entries() if e["id"] == journal_id), None)
    if not entry:
        raise ConnecterError(f"No journal entry {journal_id}")
    if entry["undone"]:
        return {"message": "Already undone"}
    if not entry["undo"]:
        raise ConnecterError(f"'{entry['action']}' can't be undone automatically")
    for step in entry["undo"]:
        if step["type"] == "_recreate_tag":
            t = create_tag(step["payload"]["tag"])
            if step["payload"]["assets"]:
                client.request("TagAssets", {"TagGuid": t["tag"]["Guid"], "AssetFullPaths": step["payload"]["assets"]})
        else:
            client.request(step["type"], step["payload"])
    journal.mark_undone(journal_id)
    return {"undone": journal_id, "action": entry["action"]}


from . import maintenance  # noqa: E402,F401  registers the maintenance tools on `mcp`


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
