"""Library upkeep: hiding files, generating previews, one-shot fixes and sandboxed sessions."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import journal, maxfile
from .client import ConnecterError
from .config import HOME_DIR
from .files import IMAGE_EXTS, classify_images
from .maintenance import attach_folder_previews, orphan_cleanup, rename_cleanly, restore_workspace, sync_tags_to_folders
from .maxbridge import execute, ms
from .server import (_backup, _db, _get_asset, _remove_preview_links, batch_relocate, client, search_assets, set_previews,
                     tool, undo)

GENERATED = HOME_DIR / "generated_previews"
SANDBOX = HOME_DIR / "sandbox.json"


@tool
def hide_files(files: list[str] | None = None, folder: str = "", textures_only: bool = True, dry_run: bool = True) -> dict:
    """Hide image files (texture maps by default) from Connecter's asset grid so folders show only the
    models and their previews. Give files, or a folder to hide every texture map in it (all images
    with textures_only=False). Connecter has no 'hide' command, so this uses the hiding it applies to
    previews: each file is briefly attached as an auto-hidden preview of a model and detached again.
    Unhiding is only possible in Connecter (right-click > Unhide). dry_run=True (default) lists the files."""
    if files:
        targets = [Path(f) for f in files]
    elif folder:
        root = Path(folder)
        found = classify_images(root)
        targets = [Path(i["path"]) for i in found["textures"] + ([] if textures_only else found["previews"])]
    else:
        raise ConnecterError("Give files or a folder")
    with _db() as db:
        hidden = {h.lower() for h in db.hidden_files()}
        assets = [a for a in db.assets() if Path(a.path).exists() and Path(a.path).suffix.lower() not in IMAGE_EXTS]
    targets = [t for t in targets if t.exists() and t.suffix.lower() in IMAGE_EXTS and str(t).lower() not in hidden]
    if not targets:
        return {"hidden": 0, "note": "Nothing to hide (already hidden or not images)."}
    if not assets:
        raise ConnecterError("Need at least one catalogued model to carry the hidden previews")

    def carrier(f: Path) -> str:
        # The nearest catalogued model above the file, so nothing unrelated is touched even briefly.
        best = max(assets, key=lambda a: len(Path(a.path).parent.parts) if Path(a.path).parent in [f.parent, *f.parents] else -1)
        return best.path

    groups: dict[str, list[str]] = {}
    for t in targets:
        groups.setdefault(carrier(t), []).append(str(t))
    if dry_run:
        return {"dry_run": True, "would_hide": len(targets), "files": [str(t) for t in targets][:200]}
    for model, paths in groups.items():
        info = _get_asset(model)
        before = {p["FileFullPath"].lower() for p in info["CustomPreviews"]}
        new = [p for p in paths if p.lower() not in before]
        client.drop_message("AddPreviewsToAsset", {"AssetFullPath": model, "Previews": [{"FullPath": p} for p in new],
                                                   "RemoveOldPreviews": False, "AutoHidePreviews": True})
        for _ in range(20):
            time.sleep(0.5)
            now = {p["FileFullPath"].lower() for p in _get_asset(model)["CustomPreviews"]}
            if {p.lower() for p in new} <= now:
                break
        _remove_preview_links(info["Guid"], new)
    with _db() as db:
        hidden = {h.lower() for h in db.hidden_files()}
    done = [str(t) for t in targets if str(t).lower() in hidden]
    journal.record("hide_files", {"files": done})
    return {"hidden": len(done), "not_hidden": [str(t) for t in targets if str(t).lower() not in hidden]}


def _render_preview(max_file: Path, out: Path, force: bool) -> Path:
    body = f"""
renderers.current = Default_Scanline_Renderer()
clay = Standardmaterial diffuse:(color 205 205 200)
for o in geometry do o.material = clay
bb = mcpBBox (objects as array)
if bb == undefined do throw "No geometry"
c = (bb[1] + bb[2]) / 2
d = distance bb[1] bb[2]
cam = Targetcamera fov:35 target:(Targetobject pos:c) pos:(c + [d * 0.9, -d * 1.3, d * 0.75])
-- Scanline's default lights stay on as long as the scene has no lights of its own.
for l in lights do delete l
backgroundColor = color 235 235 235
render camera:cam outputSize:[800, 600] vfb:false outputFile:{ms(out)} quiet:true
mcpResult = mcpO #(#("image", {ms(out)}))
"""
    execute(body, "batch", max_file, force)
    return out


@tool
def generate_preview(asset: str = "", folder: str = "", tag: str = "", source: str = "thumbnail", only_missing: bool = True,
                     dry_run: bool = True, force: bool = False, limit: int = 100) -> dict:
    """Make preview images for models that have none. source='thumbnail' (default) uses the thumbnail 3ds Max
    saved inside the .max (instant, no 3ds Max); source='render' renders a clay preview in headless 3ds Max
    (slow; refused while 3ds Max is busy unless force=True). Previews are attached in Connecter.
    dry_run=True (default) lists the models."""
    if source not in ("thumbnail", "render"):
        raise ConnecterError("source must be 'thumbnail' or 'render'")
    if asset:
        info = _get_asset(asset)
        rows = [{"path": info["FileFullPath"], "previews": len(info["CustomPreviews"])}]
    else:
        rows = search_assets(folder=folder, tag=tag, no_previews=only_missing, extension=".max", limit=1000000)["assets"]
    rows = [r for r in rows if Path(r["path"]).exists() and not (only_missing and r["previews"])][:limit]
    if dry_run:
        return {"dry_run": True, "models": len(rows), "sample": [r["path"] for r in rows[:20]]}
    GENERATED.mkdir(parents=True, exist_ok=True)
    made, failed = [], []
    for r in rows:
        f = Path(r["path"])
        out = GENERATED / f"{f.stem}_{hashlib.md5(str(f).lower().encode()).hexdigest()[:8]}{'.png' if source == 'thumbnail' else '.jpg'}"
        try:
            img = maxfile.thumbnail(f, out) if source == "thumbnail" else _render_preview(f, out, force)
        except ConnecterError as e:
            failed.append({"asset": str(f), "error": str(e)[:200]})
            continue
        if not img:
            failed.append({"asset": str(f), "error": "no thumbnail stored in the file"})
            continue
        set_previews(str(f), [str(img)])
        made.append({"asset": str(f), "preview": str(img)})
    return {"generated": len(made), "failed": failed, "previews": made[:50]}


@tool
def fix_issues(fixes: list[str] | None = None, dry_run: bool = True) -> dict:
    """Fix what audit_library finds, in one go. fixes picks any of: 'missing' (relocate moved assets),
    'tags' (tag untagged models from their folder), 'previews' (attach folder images, then .max
    thumbnails), 'names' (strip download-site junk from folder names), 'orphans' (move unused preview
    images aside). Default: all. dry_run=True (default) returns every plan; a backup is taken before changes."""
    fixes = fixes or ["missing", "tags", "previews", "names", "orphans"]
    unknown = set(fixes) - {"missing", "tags", "previews", "names", "orphans"}
    if unknown:
        raise ConnecterError(f"Unknown fixes: {sorted(unknown)}")
    if not dry_run:
        _backup("fix-issues")
    out = {"dry_run": dry_run}
    if "missing" in fixes:
        out["missing"] = batch_relocate(dry_run=dry_run)
    if "tags" in fixes:
        out["tags"] = sync_tags_to_folders(dry_run=dry_run)
    if "previews" in fixes:
        out["previews"] = {"folder_images": attach_folder_previews(dry_run=dry_run),
                           "thumbnails": generate_preview(dry_run=dry_run, limit=100000)}
    if "names" in fixes:
        r = rename_cleanly(dry_run=dry_run)
        out["names"] = {k: (len(v) if isinstance(v, list) else v) for k, v in r.items()}
    if "orphans" in fixes:
        out["orphans"] = orphan_cleanup(dry_run=dry_run)
    if dry_run:
        # Keep the plan readable: counts and a few examples, not every path.
        for k in ("missing",):
            if k in out:
                out[k] = {"would_relocate": len(out[k]["would_relocate"]), "needs_review": len(out[k]["needs_review"])}
    return out


@tool
def sandbox(action: str = "status", label: str = "", full_restore: bool = False) -> dict:
    """Try changes safely. action='start' takes a workspace backup and marks the journal; everything done
    afterwards can be thrown away with action='rollback' (undoes each journaled change, newest first;
    full_restore=True instead restores the backup and restarts Connecter), or kept with action='commit'.
    action='status' shows what has changed since the start."""
    state = json.loads(SANDBOX.read_text("utf-8")) if SANDBOX.exists() else None
    if action == "start":
        if state:
            raise ConnecterError(f"A sandbox is already open since {state['started']}. Commit or roll it back first.")
        backup = _backup(f"sandbox-{label or 'session'}")
        state = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "label": label, "backup": backup,
                 "journal_count": len(journal.entries())}
        SANDBOX.write_text(json.dumps(state), "utf-8")
        return {"sandbox": "started", **state}
    if not state:
        return {"sandbox": "none open"}
    changes = journal.entries()[state["journal_count"]:]
    if action == "status":
        return {"sandbox": "open", **state, "changes": [{k: c[k] for k in ("id", "time", "action", "undone")} for c in changes]}
    if action == "commit":
        SANDBOX.unlink()
        return {"sandbox": "committed", "kept_changes": len(changes)}
    if action == "rollback":
        if full_restore:
            r = restore_workspace(Path(state["backup"]).name, confirm=True)
            SANDBOX.unlink()
            return {"sandbox": "restored from backup", **r}
        undone, failed = [], []
        for c in reversed(changes):
            if c["undone"] or not c["undo"]:
                if not c["undone"]:
                    failed.append({"id": c["id"], "action": c["action"], "error": "not undoable"})
                continue
            try:
                undo(c["id"])
                undone.append(c["id"])
            except Exception as e:  # noqa: BLE001 - keep rolling back the rest
                failed.append({"id": c["id"], "action": c["action"], "error": str(e)[:200]})
        SANDBOX.unlink()
        return {"sandbox": "rolled back", "undone": len(undone), "could_not_undo": failed,
                "backup": state["backup"], "note": "If anything is left over, sandbox can't help any more, but restore_workspace with the backup can."}
    raise ConnecterError("action must be start, status, commit or rollback")
