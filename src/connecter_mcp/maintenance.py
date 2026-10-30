"""Library maintenance tools: tag sync, taxonomy changes, previews, duplicates, stats, cleanup, recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from collections import Counter, defaultdict
from pathlib import Path

from . import journal
from .client import ConnecterError
from .config import HOME_DIR, Settings
from .files import MODEL_EXTS, classify_images
from .server import (GUID_RE, _backup, _close_connecter, _db, _get_asset, _launch_connecter, _tag_guid,
                     _tag_path, _tags, client, create_tag, find_missing_assets, mcp, relocate_asset, set_previews)

TESTED_VERSION = "8.0"
JUNK_NAME = [re.compile(p, re.I) for p in (
    r"^\s*(pro\s*3dsky|3dsky(\.org)?|3ddd(\.ru)?|3dline(\.net)?|cgtrader|turbosquid|free\s*3d)\s*[-_:]\s*",
    r"\s*[\(\[](3dline\.net|3dsky\.org|3ddd\.ru|cgtrader|turbosquid)[\)\]]\s*$",
)]
RENDERERS = ("corona", "vray", "arnold", "redshift", "octane", "fstorm")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _ensure_tag(tag: str, tags: list[dict] | None = None) -> str:
    """GUID of a tag path, creating it and any missing parents."""
    tags = tags if tags is not None else _tags()
    try:
        return _tag_guid(tag, tags)
    except ConnecterError:
        parts = _tag_path(tag)
        if len(parts) > 1:
            _ensure_tag("/".join(parts[:-1]), tags)
        guid = create_tag("/".join(parts))["tag"]["Guid"]
        tags[:] = _tags()
        return guid


def _model_files(folder: Path, extensions: list[str]) -> list[Path]:
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in extensions}
    return [Path(d) / n for d, _, names in os.walk(folder) for n in names if Path(n).suffix.lower() in exts]


# ---------------------------------------------------------------- tags

@mcp.tool()
def sync_tags_to_folders(folder: str = "", extensions: list[str] | None = None, only_untagged: bool = True,
                         include_uncatalogued: bool = False, dry_run: bool = True) -> dict:
    """Tag models with the tag that matches their library folder, e.g. a .max under
    '3D MODELS/06_LIGHTS/WALL LIGHTS/Tom Dixon/' gets '06_LIGHTS/WALL LIGHTS'. Uses the deepest existing
    tag whose names match the folder names (ignoring case, spaces and underscores). Needs models_dir in
    the config. extensions defaults to ['.max']. dry_run=True (default) returns the plan per tag."""
    s = Settings.load()
    if not s.models_dir:
        raise ConnecterError("Set models_dir in the config first")
    root = Path(folder) if folder else s.models_dir
    exts = [e.lower() for e in extensions or [".max"]]
    tag_by_norm = {tuple(_norm(p) for p in t["TagPath"]): "/".join(t["TagPath"]) for t in _tags()}
    with _db() as db:
        catalogued = {a.path.lower(): a for a in db.assets()}
    files = [Path(a.path) for a in catalogued.values() if a.path.lower().startswith(str(root).lower())
             and Path(a.path).suffix.lower() in exts]
    if include_uncatalogued:
        files += [f for f in _model_files(root, exts) if str(f).lower() not in catalogued]
    plan: dict[str, list[str]] = defaultdict(list)
    skipped = 0
    for f in files:
        a = catalogued.get(str(f).lower())
        if only_untagged and a and a.tags:
            continue
        try:
            parts = [_norm(p) for p in f.parent.relative_to(s.models_dir).parts]
        except ValueError:
            skipped += 1
            continue
        tag = next((tag_by_norm[tuple(parts[:i])] for i in range(len(parts), 0, -1) if tuple(parts[:i]) in tag_by_norm), None)
        if not tag:
            skipped += 1
        elif not a or tag not in a.tags:
            plan[tag].append(str(f))
    summary = {tag: len(paths) for tag, paths in sorted(plan.items())}
    if dry_run:
        return {"dry_run": True, "assets_to_tag": sum(summary.values()), "by_tag": summary,
                "no_matching_tag": skipped, "sample": {t: p[:3] for t, p in list(plan.items())[:5]}}
    _backup("sync-tags")
    tagged = 0
    for tag, paths in plan.items():
        guid = _tag_guid(tag)
        for i in range(0, len(paths), 200):
            chunk = paths[i:i + 200]
            tagged += client.request("TagAssets", {"TagGuid": guid, "AssetFullPaths": chunk}, timeout=120).get("TaggedAssetsCount") or 0
            journal.record("sync_tags_to_folders", {"tag": tag, "assets": len(chunk)},
                           [{"type": "UntagAssets", "payload": {"TagGuid": guid, "AssetFullPaths": chunk}}])
    return {"tagged": tagged, "by_tag": summary}


@mcp.tool()
def restructure_taxonomy(mapping: dict[str, str], delete_old_tags: bool = False, dry_run: bool = True) -> dict:
    """Move assets between tags in bulk: mapping is {old tag: new tag}, e.g.
    {'09_MISC': '09_MISC/LANDSCAPE'} or {'03_DECOR/BOOKS': '03_DECOR/DECORATIVE SET'}. New tags (and parents)
    are created. Only assets tagged directly with the old tag move. delete_old_tags removes emptied tags that
    have no sub-tags. dry_run=True (default) returns the plan."""
    tags = _tags()
    plan = []
    for old, new in mapping.items():
        guid = _tag_guid(old, tags)
        assets = client.request("GetAssetsByTags", {"TagGuids": [guid], "IncludeAssetsFromSubtags": False, "MaxAssetsCount": 100000})["Assets"]
        direct = [a["FileFullPath"] for a in assets if guid in {t["Guid"] for t in a["Tags"]}]
        has_children = any(t["ParentGuid"] == guid for t in tags)
        plan.append({"from": old, "to": new, "assets": len(direct), "paths": direct,
                     "new_tag_exists": any("/".join(t["TagPath"]).lower() == "/".join(_tag_path(new)).lower() for t in tags),
                     "delete_old": delete_old_tags and not has_children})
    if dry_run:
        return {"dry_run": True, "plan": [{k: v for k, v in p.items() if k != "paths"} for p in plan]}
    _backup("restructure-taxonomy")
    done = []
    for p in plan:
        new_guid = _ensure_tag(p["to"], tags)
        old_guid = _tag_guid(p["from"], tags)
        if p["paths"]:
            client.request("TagAssets", {"TagGuid": new_guid, "AssetFullPaths": p["paths"]}, timeout=120)
            client.request("UntagAssets", {"TagGuid": old_guid, "AssetFullPaths": p["paths"]}, timeout=120)
        undo = [{"type": "TagAssets", "payload": {"TagGuid": old_guid, "AssetFullPaths": p["paths"]}},
                {"type": "UntagAssets", "payload": {"TagGuid": new_guid, "AssetFullPaths": p["paths"]}}]
        if p["delete_old"]:
            client.request("DeleteTag", {"TagGuid": old_guid})
            undo = [{"type": "_recreate_tag", "payload": {"tag": p["from"], "assets": p["paths"]}},
                    {"type": "UntagAssets", "payload": {"TagGuid": new_guid, "AssetFullPaths": p["paths"]}}]
        jid = journal.record("restructure_taxonomy", {"from": p["from"], "to": p["to"], "assets": len(p["paths"])}, undo)
        done.append({"from": p["from"], "to": p["to"], "moved": len(p["paths"]), "deleted_old": p["delete_old"], "journal_id": jid})
    return {"done": done}


# ---------------------------------------------------------------- previews and files

@mcp.tool()
def attach_folder_previews(folder: str = "", tag: str = "", max_previews: int = 3, dry_run: bool = True) -> dict:
    """For catalogued .max models with no previews, attach preview images found in the model's own folder
    (texture maps are excluded). Folders holding more than two .max files (bundles) are skipped, since their
    images can't be matched to a model safely. dry_run=True (default) returns the plan."""
    with _db() as db:
        assets = [a for a in db.assets() if not a.previews and a.path.lower().endswith(".max")
                  and a.path.lower().startswith(folder.lower())
                  and (not tag or any(t.lower() == tag.lower() or t.lower().startswith(tag.lower() + "/") for t in a.tags))]
    plan, skipped = [], Counter()
    seen_dirs: dict[str, dict] = {}
    for a in assets:
        d = Path(a.path).parent
        if not d.exists():
            skipped["file missing"] += 1
            continue
        if str(d) not in seen_dirs:
            maxes = list(d.rglob("*.max"))
            seen_dirs[str(d)] = {"bundle": len(maxes) > 2, "images": [] if len(maxes) > 2 else classify_images(d)["previews"]}
        info = seen_dirs[str(d)]
        if info["bundle"]:
            skipped["bundle folder"] += 1
        elif not info["images"]:
            skipped["no preview images"] += 1
        else:
            stem = _norm(Path(a.path).stem)
            imgs = sorted(info["images"], key=lambda i: (_norm(Path(i["path"]).stem) not in stem and stem not in _norm(Path(i["path"]).stem), i["path"]))
            plan.append({"asset": a.path, "previews": [i["path"] for i in imgs[:max_previews]]})
    if dry_run:
        return {"dry_run": True, "would_attach": len(plan), "skipped": dict(skipped), "sample": plan[:10]}
    for p in plan:
        set_previews(p["asset"], p["previews"])
    return {"attached": len(plan), "skipped": dict(skipped)}


@mcp.tool()
def orphan_cleanup(dry_run: bool = True) -> dict:
    """Preview images in the workspace's Media/previews folder that no asset uses any more. With
    dry_run=False they are moved (not deleted) to Media/previews_orphaned_<date> so they can be restored."""
    pdir = client.paths.previews_dir
    if not pdir or not pdir.exists():
        raise ConnecterError("Workspace previews folder not found")
    with _db() as db:
        used = {local.lower() for _, local, _ in db.preview_files()}
    orphans = [p for p in pdir.iterdir() if p.is_file() and p.name.lower() not in used]
    size_mb = round(sum(p.stat().st_size for p in orphans) / 1e6, 1)
    if dry_run:
        return {"dry_run": True, "orphans": len(orphans), "size_mb": size_mb, "sample": [p.name for p in orphans[:10]]}
    dest = pdir.parent / f"previews_orphaned_{time.strftime('%Y%m%d-%H%M%S')}"
    dest.mkdir()
    for p in orphans:
        shutil.move(str(p), dest / p.name)
    journal.record("orphan_cleanup", {"moved_to": str(dest), "files": len(orphans)})
    return {"moved": len(orphans), "size_mb": size_mb, "to": str(dest)}


@mcp.tool()
def rename_cleanly(folder: str = "", dry_run: bool = True) -> dict:
    """Strip download-site junk from model folder names ('Pro 3DSky - X', 'X (3dline.net)', '3ddd - X') by
    renaming through Connecter, so tags and previews stay attached. Skips names that would collide.
    Defaults to models_dir. dry_run=True (default) returns the plan."""
    root = Path(folder) if folder else Settings.load().models_dir
    if not root:
        raise ConnecterError("Give a folder or set models_dir in the config")
    plan, collisions = [], []
    for d, dirs, _ in os.walk(root):
        for name in dirs:
            new = name
            for pat in JUNK_NAME:
                new = pat.sub("", new).strip()
            if new and new != name:
                (collisions if (Path(d) / new).exists() else plan).append({"folder": str(Path(d) / name), "new_name": new})
    if dry_run:
        return {"dry_run": True, "renames": plan, "collisions": collisions}
    done = []
    for p in sorted(plan, key=lambda p: -len(p["folder"])):  # deepest first so parent paths stay valid
        client.request("RenameFolderInFileSystem", {"FolderFullPath": p["folder"], "NewName": p["new_name"]})
        journal.record("rename_cleanly", p, [{"type": "RenameFolderInFileSystem", "payload": {
            "FolderFullPath": str(Path(p["folder"]).parent / p["new_name"]), "NewName": Path(p["folder"]).name}}])
        done.append(p)
    return {"renamed": len(done), "collisions": collisions}


@mcp.tool()
def rescan(folder: str, extensions: list[str] | None = None) -> dict:
    """Tell Connecter about model files in a folder (and sub-folders) so they show up even when its file
    watcher missed them (its log often shows 'Too many changes at once'). Connecter gives no reply to this
    notification; if files still don't appear, use restart_connecter."""
    files = [str(f) for f in _model_files(Path(folder), extensions or sorted(MODEL_EXTS))]
    for i in range(0, len(files), 200):
        client.send("AssetsExportedToConnecter", {"AssetFullPaths": files[i:i + 200]})
    return {"notified": len(files), "folder": folder}


@mcp.tool()
def ingest_queue(folder: str = "") -> dict:
    """Downloads waiting to go into the library: folders (and archives) in the downloads folder that contain
    model files, with model, preview and texture counts. Use ingest_model to bring one in."""
    root = Path(folder) if folder else Settings.load().downloads_dir
    items = []
    for p in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.is_dir():
            models = _model_files(p, sorted(MODEL_EXTS))
            if models:
                imgs = classify_images(p)
                items.append({"folder": str(p), "models": [m.name for m in models][:10], "model_count": len(models),
                              "previews": len(imgs["previews"]), "textures": len(imgs["textures"]),
                              "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))})
        elif p.suffix.lower() in {".zip", ".rar", ".7z"}:
            items.append({"archive": str(p), "size_mb": round(p.stat().st_size / 1e6, 1),
                          "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
                          "note": "extract it first"})
    return {"downloads_dir": str(root), "pending": items}


# ---------------------------------------------------------------- health

def _quick_hash(path: Path) -> str:
    """Size plus the first and last MB; cheap and reliable enough for finding identical model files."""
    h = hashlib.sha1()
    size = path.stat().st_size
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > 2 << 20:
            f.seek(-(1 << 20), 2)
            h.update(f.read())
    return h.hexdigest()


@mcp.tool()
def find_duplicates(folder: str = "", extensions: list[str] | None = None, limit: int = 50) -> dict:
    """Identical model files saved in more than one place (same size and content), with their tags and
    previews so you can decide which copy to keep. Defaults to models_dir and '.max'."""
    root = Path(folder) if folder else Settings.load().models_dir
    if not root:
        raise ConnecterError("Give a folder or set models_dir in the config")
    by_size: dict[int, list[Path]] = defaultdict(list)
    for f in _model_files(root, extensions or [".max"]):
        try:
            by_size[f.stat().st_size].append(f)
        except OSError:
            pass
    groups: dict[str, list[Path]] = defaultdict(list)
    for files in by_size.values():
        if len(files) > 1:
            for f in files:
                groups[_quick_hash(f)].append(f)
    with _db() as db:
        info = {a.path.lower(): a for a in db.assets()}
    dupes = []
    for files in groups.values():
        if len(files) > 1:
            dupes.append({"size_mb": round(files[0].stat().st_size / 1e6, 1), "copies": [
                {"path": str(f), "tags": info[str(f).lower()].tags if str(f).lower() in info else [],
                 "previews": info[str(f).lower()].previews if str(f).lower() in info else 0} for f in files]})
    dupes.sort(key=lambda d: -d["size_mb"] * len(d["copies"]))
    wasted = sum(d["size_mb"] * (len(d["copies"]) - 1) for d in dupes)
    return {"duplicate_groups": len(dupes), "wasted_mb": round(wasted, 1), "groups": dupes[:limit]}


@mcp.tool()
def library_stats(include_disk_size: bool = False) -> dict:
    """Breakdown per library category: model files on disk, catalogued, tagged, with previews, renderer
    (guessed from names) and optionally disk size. Needs models_dir in the config."""
    s = Settings.load()
    if not s.models_dir:
        raise ConnecterError("Set models_dir in the config first")
    with _db() as db:
        assets = {a.path.lower(): a for a in db.assets()}
    stats = []
    for cat in sorted(p for p in s.models_dir.iterdir() if p.is_dir()):
        files = _model_files(cat, [".max"])
        cat_assets = [assets.get(str(f).lower()) for f in files]
        renderers = Counter(next((r for r in RENDERERS if r in f.name.lower().replace("-", "")), "unknown") for f in files)
        row = {"category": cat.name, "max_files": len(files),
               "catalogued": sum(1 for a in cat_assets if a), "tagged": sum(1 for a in cat_assets if a and a.tags),
               "with_previews": sum(1 for a in cat_assets if a and a.previews), "renderers": dict(renderers)}
        if include_disk_size:
            row["size_gb"] = round(sum(f.stat().st_size for d, _, n in os.walk(cat) for f in (Path(d) / x for x in n)) / 1e9, 2)
        stats.append(row)
    total = {k: sum(r[k] for r in stats) for k in ("max_files", "catalogued", "tagged", "with_previews")}
    return {"models_dir": str(s.models_dir), "total": total, "categories": stats}


# ---------------------------------------------------------------- missing assets

@mcp.tool()
def relocate_folder(old_folder: str, new_folder: str, dry_run: bool = True) -> dict:
    """A whole folder was moved or renamed outside Connecter: point every catalogued asset under
    old_folder at the same relative path under new_folder (only where that file exists).
    dry_run=True (default) returns the plan."""
    old = old_folder.rstrip("\\/") + "\\"
    with _db() as db:
        under = [a for a in db.assets() if a.path.lower().startswith(old.lower())]
    plan, not_found = [], []
    for a in under:
        target = Path(new_folder) / a.path[len(old):]
        if Path(a.path).exists():
            continue
        (plan if target.exists() else not_found).append({"asset": a.path, "to": str(target)})
    if dry_run:
        return {"dry_run": True, "would_relocate": len(plan), "not_found": not_found[:20], "sample": plan[:5]}
    _backup("relocate-folder")
    for p in plan:
        relocate_asset(p["asset"], p["to"])
    return {"relocated": len(plan), "not_found": not_found}


@mcp.tool()
def resolve_deleted(folder: str = "", archive: bool = False) -> dict:
    """Missing assets with no file found anywhere (truly deleted). Lists what each one still holds (tags,
    previews, description). With archive=True, assets that hold tags have their data saved to
    ~/.connecter-mcp/archive.jsonl and are untagged so they stop cluttering tag views (undo re-tags them)."""
    gone = [a for a in find_missing_assets(folder)["assets"] if not any(c["match"] == "exact name" for c in a["candidates"])]
    with_data = [a for a in gone if a["tags"] or a["previews"] or a["description"]]
    if not archive:
        return {"deleted": len(gone), "holding_data": len(with_data),
                "assets": [{k: a[k] for k in ("path", "tags", "previews", "description")} for a in with_data[:50]],
                "note": "Assets holding no data can be left alone or cleared in Connecter's Missing filter."}
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    archived = 0
    with (HOME_DIR / "archive.jsonl").open("a", encoding="utf-8") as f:
        for a in with_data:
            if not a["tags"]:
                continue
            info = _get_asset(a["guid"])
            f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), **info, "description": a["description"]}) + "\n")
            client.request("UntagAssetFromAllTags", {"AssetFullPath": info["FileFullPath"]})
            journal.record("resolve_deleted", {"asset": info["FileFullPath"]},
                           [{"type": "TagAsset", "payload": {"AssetFullPath": info["FileFullPath"], "TagGuids": [t["Guid"] for t in info["Tags"]]}}])
            archived += 1
    return {"archived": archived, "archive_file": str(HOME_DIR / "archive.jsonl")}


# ---------------------------------------------------------------- system

@mcp.tool()
def self_test() -> dict:
    """Check that everything this MCP relies on still works (useful after a Connecter update): the API,
    the message folder, the database layout and the journal. Write tools may misbehave if any check fails."""
    checks = []

    def check(name, fn):
        try:
            detail = fn()
            checks.append({"check": name, "ok": True, "detail": detail})
        except Exception as e:  # noqa: BLE001 - report every failure
            checks.append({"check": name, "ok": False, "detail": str(e)})

    client.refresh()
    p = client.paths
    check("Connecter version", lambda: (p.version or "unknown") + ("" if (p.version or "").startswith(TESTED_VERSION)
                                                                     else f" (built against {TESTED_VERSION}.x; re-test write tools)"))
    check("API reachable", lambda: f"{len(_tags())} tags")
    check("API asset lookup", lambda: f"{len(client.request('GetCatalogedAssets', {'MaxAssetsCount': 1})['Assets'])} asset returned")
    def message_folder():
        if not (p.buffer_dir and p.buffer_dir.exists() and os.access(p.buffer_dir, os.W_OK)):
            raise ConnecterError("missing or not writable")
        return str(p.buffer_dir)

    check("Message folder", message_folder)

    def db_layout():
        with _db() as db:
            tables = {r[0] for r in db.conn.execute("select name from sqlite_master where type='table'")}
        missing = {"Items", "Tags", "Items_Tags", "Previews", "ItemAttributes", "Repositories"} - tables
        if missing:
            raise ConnecterError(f"tables missing: {sorted(missing)}")
        return "expected tables present"

    check("Database layout", db_layout)
    check("Journal writable", lambda: (HOME_DIR.mkdir(parents=True, exist_ok=True), str(HOME_DIR))[1])
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


@mcp.tool()
def restore_workspace(backup: str, confirm: bool = False) -> dict:
    """Put a workspace database backup (a name from backup_workspace's list) back in place. Closes
    Connecter, saves the current database as a 'pre-restore' backup, copies the backup in and restarts
    Connecter. Needs confirm=True."""
    src = journal.BACKUPS / backup
    db_path = client.paths.db_path
    if not (src / db_path.name).exists():
        raise ConnecterError(f"Backup '{backup}' not found. Available: {[b['name'] for b in journal.list_backups()[:10]]}")
    if not confirm:
        return {"dry_run": True, "message": f"Call again with confirm=True to restore '{backup}' (Connecter will restart)."}
    if not _close_connecter():
        return {"restored": False, "message": "Connecter did not close (a dialog may be open). Close it and retry."}
    current = journal.backup_workspace(db_path, "pre-restore")
    shutil.copy2(src / db_path.name, db_path)
    journal.record("restore_workspace", {"restored": backup, "previous_saved_as": str(current)})
    return {"restored": backup, "previous_saved_as": current.name, **_launch_connecter()}
