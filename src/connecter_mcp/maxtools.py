"""3ds Max tools: texture relinking, model fixes, measuring, proxies, renderer and version conversion,
materials and scene assembly.

Tools that only read a .max (verify_textures, scene_missing_assets, scene_asset_report, list_materials,
consistency_check in file mode) work without 3ds Max. The rest build MAXScript and either return it
(mode="script", for the open 3ds Max) or run it headless (mode="batch"); see maxbridge.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from . import journal, maxfile
from .client import ConnecterError
from .config import HOME_DIR, Settings
from .files import IMAGE_EXTS, image_size
from .maxbridge import execute, find_max, ms, ms_array
from .server import (GUID_RE, _db, _get_asset, _texture_status, catalogue_asset, find_texture_candidates, search_assets,
                     tool)

MAX_BACKUPS = HOME_DIR / "max_backups"
MEASUREMENTS = HOME_DIR / "measurements.json"
PROXY_EXTS = {".vrmesh", ".cgeo", ".abc"}

# Texture roles by file-name keyword, for create_material.
MAP_ROLES = {
    "diffuse": r"diff(use)?|albedo|base_?colou?r|basecolor|colou?r|col\b",
    "roughness": r"rough(ness)?",
    "glossiness": r"gloss(iness)?",
    "normal": r"norm(al)?(map)?|nrm|nor\b",
    "bump": r"bump",
    "metalness": r"metal(lic|ness)?",
    "opacity": r"opacity|alpha|mask|cutout",
    "displacement": r"disp(lacement)?|height",
    "reflection": r"refl(ect(ion)?)?|spec(ular)?",
}


def _max_path(asset: str) -> Path:
    p = Path(_get_asset(asset)["FileFullPath"]) if GUID_RE.match(asset.strip()) else Path(asset)
    if not p.exists():
        raise ConnecterError(f"{p} does not exist")
    if p.suffix.lower() != ".max":
        raise ConnecterError(f"{p} is not a .max file")
    return p


def _backup_file(path: Path) -> Path:
    dest = MAX_BACKUPS / time.strftime("%Y%m%d-%H%M%S") / path.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return dest


def _sibling(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.stem}_{suffix}{path.suffix}")


def _year() -> str | None:
    d = find_max()
    m = re.search(r"20\d\d", d.name) if d else None
    return m.group(0) if m else None


def _scene_or_open(asset: str, mode: str) -> Path | None:
    """The .max to work on; None means the scene that's open in 3ds Max (script mode only)."""
    if asset:
        return _max_path(asset)
    if mode == "batch":
        raise ConnecterError("Give an asset (.max path or GUID) for mode='batch'")
    return None


def _run(body: str, mode: str, scene: Path | None, force: bool, timeout: int = 1800) -> dict:
    return execute(body, mode, scene, force, timeout)


def _relink_lists(mapping: dict[str, str]) -> tuple[str, str]:
    return ms_array([k.lower() for k in mapping]), ms_array(list(mapping.values()))


def _mapping_for(max_file: Path, choices: dict[str, str] | None, min_confidence: float, make_relative: bool):
    found = find_texture_candidates(str(max_file))
    mapping, unresolved = {}, []
    for missing, cands in found["missing"].items():
        src = (choices or {}).get(missing) or (cands[0]["path"] if cands and cands[0]["confidence"] >= min_confidence else None)
        if src:
            mapping[missing] = Path(src).name if make_relative else src
        else:
            unresolved.append({"missing": missing, "candidates": cands[:3]})
    return mapping, unresolved


# ---------------------------------------------------------------- textures

@tool
def relink_max_textures(asset: str, choices: dict[str, str] | None = None, min_confidence: float = 0.9,
                        make_relative: bool = False, mode: str = "batch", dry_run: bool = True, force: bool = False) -> dict:
    """Rewrite the missing map, proxy and IES paths stored inside a .max file to the files found for them
    (bitmaps, V-Ray/Corona bitmaps, proxies, lights - any file-name property). `choices` maps a missing
    path to the file to use; otherwise the best candidate at or above min_confidence is used.
    mode='batch' runs 3ds Max headless and saves the file (a backup is kept and `undo` restores it);
    mode='script' returns MAXScript for the open 3ds Max. dry_run=True (default) only shows the plan."""
    max_file = _max_path(asset)
    mapping, unresolved = _mapping_for(max_file, choices, min_confidence, make_relative)
    plan = {"asset": str(max_file), "relinks": mapping, "unresolved": unresolved}
    if dry_run or not mapping:
        return {"dry_run": dry_run, **plan, **({"note": "Nothing to relink."} if not mapping else {})}
    frm, to = _relink_lists(mapping)
    body = f"""
n = 0
for o in mcpFileOwners() do n += mcpRelinkProps o {frm} {to}
if n > 0 do saveMaxFile (maxFilePath + maxFileName) quiet:true
mcpResult = mcpO #(#("file", maxFilePath + maxFileName), #("relinked", n))
"""
    backup = _backup_file(max_file) if mode == "batch" else None
    result = _run(body, mode, max_file, force)
    if mode == "batch":
        jid = journal.record("relink_max_textures", plan, [{"type": "_restore_file", "payload": {"backup": str(backup), "target": str(max_file)}}])
        result["journal_id"] = jid
    return {**plan, **result}


@tool
def batch_relink(folder: str = "", tag: str = "", min_confidence: float = 0.9, make_relative: bool = False,
                 mode: str = "batch", dry_run: bool = True, force: bool = False, limit: int = 200) -> dict:
    """relink_max_textures for every .max (in a folder or tag) that has missing maps, in one 3ds Max session.
    Files are backed up first; `undo` restores them. dry_run=True (default) lists what would change."""
    files = [Path(a["path"]) for a in search_assets(folder=folder, tag=tag, extension=".max", limit=100000)["assets"]]
    jobs, unresolved = [], {}
    for f in files[:limit]:
        if not f.exists():
            continue
        mapping, bad = _mapping_for(f, None, min_confidence, make_relative)
        if mapping:
            jobs.append((f, mapping))
        if bad:
            unresolved[str(f)] = bad
    plan = {"files_to_relink": {str(f): m for f, m in jobs}, "unresolved": unresolved}
    if dry_run or not jobs:
        return {"dry_run": dry_run, "files_checked": min(len(files), limit), **plan}
    entries = ", ".join(f"#({ms(f)}, {_relink_lists(m)[0]}, {_relink_lists(m)[1]})" for f, m in jobs)
    body = f"""
jobs = #({entries})
results = #()
for j in jobs do (
    if loadMaxFile j[1] quiet:true useFileUnits:true then (
        n = 0
        for o in mcpFileOwners() do n += mcpRelinkProps o j[2] j[3]
        if n > 0 do saveMaxFile j[1] quiet:true
        append results (mcpO #(#("file", j[1]), #("relinked", n)))
    ) else append results (mcpO #(#("file", j[1]), #("error", "could not open")))
)
resetMaxFile #noPrompt
mcpResult = mcpO #(#("files", results))
"""
    backups = [{"backup": str(_backup_file(f)), "target": str(f)} for f, _ in jobs] if mode == "batch" else []
    guard = 'if getSaveRequired() do throw "The open scene has unsaved changes. Save it first: this script opens other files."\n'
    result = execute((guard if mode == "script" else "") + body, mode, None, force, timeout=600 + 120 * len(jobs))
    if mode == "batch":
        result["journal_id"] = journal.record("batch_relink", plan, [{"type": "_restore_file", "payload": b} for b in backups])
    return {**plan, **result}


@tool
def verify_textures(asset: str = "", folder: str = "", tag: str = "", limit: int = 200) -> dict:
    """Check every map of .max assets without 3ds Max: missing, found only by name, unreadable or corrupt
    images, very large maps (over 8K) and absolute paths that point outside the model's folder."""
    files = [_max_path(asset)] if asset else [Path(a["path"]) for a in search_assets(folder=folder, tag=tag, extension=".max", limit=100000)["assets"]]
    from PIL import Image

    report, totals = {}, {"ok": 0, "missing": 0, "found_by_name": 0, "unreadable": 0, "oversized": 0, "outside_folder": 0}
    for f in files[:limit]:
        if not f.exists():
            continue
        issues = []
        for t in maxfile.max_texture_paths(f):
            st = _texture_status(t, f)
            entry = {"path": t, "status": st["status"]}
            if st["resolved"] and Path(st["resolved"]).suffix.lower() in IMAGE_EXTS - {".psd", ".exr", ".hdr"}:
                try:
                    with Image.open(st["resolved"]) as im:
                        im.verify()
                    size = image_size(Path(st["resolved"]))
                    if size and max(size) > 8192:
                        entry["status"] = "oversized"
                        entry["size"] = list(size)
                except Exception as e:
                    entry["status"], entry["error"] = "unreadable", str(e)[:120]
            if entry["status"] == "ok" and Path(t).is_absolute() and f.parent not in Path(t).parents:
                entry["status"] = "outside_folder"
            totals[entry["status"]] = totals.get(entry["status"], 0) + 1
            if entry["status"] != "ok":
                issues.append(entry)
        if issues:
            report[str(f)] = issues
    return {"files_checked": min(len(files), limit), "totals": totals, "files_with_issues": len(report), "issues": report}


@tool
def scene_missing_assets(scene: str, extra_folders: list[str] | None = None) -> dict:
    """Missing external files of any .max scene (maps, proxies, IES, VDB, XRefs) with ranked candidates
    found across the library and search folders. Fix them with relink_max_textures or collect_for_project."""
    max_file = _max_path(scene)
    found = find_texture_candidates(str(max_file), extra_folders)
    by_type: dict[str, int] = {}
    for p in found["missing"]:
        ext = Path(p).suffix.lower()
        kind = "map" if ext in IMAGE_EXTS else "proxy" if ext in PROXY_EXTS else "ies" if ext == ".ies" else \
            "vdb" if ext == ".vdb" else "xref" if ext == ".max" else "other"
        by_type[kind] = by_type.get(kind, 0) + 1
    return {"scene": str(max_file), "missing_count": len(found["missing"]), "by_type": by_type, "missing": found["missing"]}


# ---------------------------------------------------------------- geometry

def _load_measurements() -> dict:
    try:
        return json.loads(MEASUREMENTS.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def measurements() -> dict:
    """Cached results of measure_asset, keyed by lower-case file path."""
    return _load_measurements()


@tool
def measure_asset(asset: str = "", selected_only: bool = False, mode: str = "batch", force: bool = False) -> dict:
    """Real-world size of a model (width x depth x height in cm), object and face counts. Give an asset
    for a .max file, or leave it empty with mode='script' to measure the open scene (selected_only for the
    selection). Batch results are cached and used by ffe_schedule."""
    scene = _scene_or_open(asset, mode)
    body = f"""
nodes = if {str(selected_only).lower()} and selection.count > 0 then (selection as array) else (objects as array)
bb = mcpBBox nodes
faces = 0
for o in nodes where superClassOf o == GeometryClass do faces += (try ((getPolygonCount o)[1]) catch 0)
if bb == undefined then mcpResult = mcpO #(#("error", "No geometry found")) else (
    sz = bb[2] - bb[1]
    mcpResult = mcpO #(#("width_cm", mcpToCm sz.x), #("depth_cm", mcpToCm sz.y), #("height_cm", mcpToCm sz.z),
        #("system_units", units.SystemType as string), #("objects", nodes.count), #("faces", faces))
)
"""
    result = _run(body, mode, scene, force)
    if mode == "batch" and scene and isinstance(result.get("result"), dict):
        cache = _load_measurements()
        cache[str(scene).lower()] = {**result["result"], "measured": time.strftime("%Y-%m-%d")}
        MEASUREMENTS.parent.mkdir(parents=True, exist_ok=True)
        MEASUREMENTS.write_text(json.dumps(cache, indent=1), "utf-8")
    return result


@tool
def fix_model(asset: str = "", reset_xform: bool = True, remove_empty: bool = True, delete_isolated_vertices: bool = False,
              weld_threshold_cm: float = 0.0, pivot_to_base: bool = False, overwrite: bool = False,
              mode: str = "batch", force: bool = False) -> dict:
    """Clean up a downloaded model: reset transforms (fixes odd scaling and flipped normals), remove empty
    objects, delete isolated vertices, weld vertices closer than weld_threshold_cm, and put pivots at the
    base. Vertex fixes convert objects to Editable Poly. Saves '<name>_fixed.max' next to the original
    unless overwrite=True (then a backup is kept and `undo` restores it). Without an asset, mode='script'
    fixes the open scene's selection (or all objects) and does not save."""
    scene = _scene_or_open(asset, mode)
    out = None if not scene else (scene if overwrite else _sibling(scene, "fixed"))
    b = lambda v: str(v).lower()
    body = f"""
geo = for o in (if selection.count > 0 and {b(scene is None)} then selection as array else objects as array)
    where superClassOf o == GeometryClass and classOf o != TargetObject collect o
fixed = #(); removed = #()
weld = {weld_threshold_cm} / (mcpToCm 1.0)
for o in geo where isValidNode o do (
    if {b(remove_empty)} and (try ((getPolygonCount o)[1]) catch 1) == 0 then (append removed o.name; delete o) else (
        if {b(reset_xform)} do (resetXForm o; collapseStack o)
        if ({b(delete_isolated_vertices)} or weld > 0) and canConvertTo o Editable_Poly do (
            convertToPoly o
            if {b(delete_isolated_vertices)} do polyop.deleteIsoVerts o
            if weld > 0 do (o.weldThreshold = weld; polyop.weldVertsByThreshold o #all)
        )
        if {b(pivot_to_base)} do o.pivot = [o.center.x, o.center.y, o.min.z]
        append fixed o.name
    )
)
{f"saveMaxFile {ms(out)} quiet:true" if out else ""}
mcpResult = mcpO #(#("fixed", fixed.count), #("removed_empty", removed), #("saved_as", {ms(out) if out else "undefined"}))
"""
    backup = _backup_file(scene) if scene and overwrite and mode == "batch" else None
    result = _run(body, mode, scene, force)
    if backup:
        result["journal_id"] = journal.record("fix_model", {"asset": str(scene)}, [{"type": "_restore_file", "payload": {"backup": str(backup), "target": str(scene)}}])
    return result


@tool
def make_proxy(asset: str = "", renderer: str = "vray", output_folder: str = "", mode: str = "batch", force: bool = False) -> dict:
    """Turn a model into a render proxy (V-Ray .vrmesh or Corona .cgeo) and save '<name>_proxy.max' with
    the proxy and its materials, next to the model (or in output_folder). Without an asset, mode='script'
    converts the open scene's selection."""
    scene = _scene_or_open(asset, mode)
    renderer = renderer.lower().replace("-", "")
    if renderer not in ("vray", "corona"):
        raise ConnecterError("renderer must be 'vray' or 'corona'")
    base = Path(output_folder) if output_folder else (scene.parent if scene else None)
    stem = scene.stem if scene else "proxy"
    if base is None:
        raise ConnecterError("Give output_folder when converting the open scene")
    mesh = base / f"{stem}_proxy{'.vrmesh' if renderer == 'vray' else '.cgeo'}"
    out = base / f"{stem}_proxy.max"
    if renderer == "vray":
        export = f"""
select nodes
vrayMeshExport meshFile:{ms(mesh)} autoCreateProxies:true exportMultiple:false createMultiMtl:true
"""
    else:
        export = f"""
mcpMesh = snapshot nodes[1]
for i = 2 to nodes.count do meshop.attach mcpMesh (snapshot nodes[i]) attachMat:#IDToMat condenseMat:true
mcpMesh.name = "{stem}_mesh"
p = CProxy name:"{stem}_proxy"
CProxy.ProxyFp.fromScene p mcpMesh {ms(mesh)}
p.material = mcpMesh.material
delete mcpMesh
delete nodes
"""
    body = f"""
nodes = for o in (if selection.count > 0 and {str(scene is None).lower()} then selection as array else objects as array)
    where superClassOf o == GeometryClass and classOf o != TargetObject collect o
if nodes.count == 0 do throw "No geometry to convert"
makeDir {ms(base)} all:true
{export}
saveMaxFile {ms(out)} quiet:true
mcpResult = mcpO #(#("proxy_file", {ms(mesh)}), #("max_file", {ms(out)}), #("objects", nodes.count))
"""
    return _run(body, mode, scene, force)


def _converter_script(target: str) -> str:
    year = _year() or "2024"
    if target == "corona":
        conv = Path(rf"C:\Program Files\Chaos\Corona\Corona Renderer for 3ds Max\{year}\Scripts\coronaConverter.ms")
        return f"""
global CoronaConverterSuppressGui = true
if CoronaConverter == undefined do fileIn {ms(conv)}
CoronaConverter.converterSettings.silent = true
CoronaConverter.converterSettings.showWarnings = false
CoronaConverter.converterSettings.printInfo = false
CoronaConverter.convertScene()
rc = for c in RendererClass.classes where matchPattern (c as string) pattern:"Corona*" collect c
if rc.count > 0 do renderers.current = rc[1]()
"""
    if target == "vray":
        utils = Path(rf"C:\ProgramData\Autodesk\ApplicationPlugins\VRay3dsMax{year}\scripts\vrayutils.ms")
        return f"""
if ConvertToVRay == undefined do fileIn {ms(utils)}
ConvertToVRay false
rc = for c in RendererClass.classes where matchPattern (c as string) pattern:"V_Ray*" and not matchPattern (c as string) pattern:"*GPU*" collect c
if rc.count > 0 do renderers.current = rc[1]()
"""
    raise ConnecterError("target must be 'corona' or 'vray'")


@tool
def convert_renderer(asset: str = "", target: str = "corona", overwrite: bool = False, mode: str = "batch", force: bool = False) -> dict:
    """Convert a model's materials, maps, lights and proxies to Corona or V-Ray with the renderer's own
    converter (Corona Converter / V-Ray Scene Converter) and switch the renderer. Saves
    '<name>_<target>.max' next to the original unless overwrite=True (backup kept, `undo` restores).
    Without an asset, mode='script' converts the open scene without saving."""
    scene = _scene_or_open(asset, mode)
    target = target.lower().replace("-", "")
    out = None if not scene else (scene if overwrite else _sibling(scene, target))
    body = _converter_script(target) + f"""
{f"saveMaxFile {ms(out)} quiet:true" if out else ""}
mcpResult = mcpO #(#("renderer", renderers.current as string), #("materials", sceneMaterials.count), #("saved_as", {ms(out) if out else "undefined"}))
"""
    backup = _backup_file(scene) if scene and overwrite and mode == "batch" else None
    result = _run(body, mode, scene, force)
    if backup:
        result["journal_id"] = journal.record("convert_renderer", {"asset": str(scene), "target": target},
                                              [{"type": "_restore_file", "payload": {"backup": str(backup), "target": str(scene)}}])
    return result


@tool
def convert_version(asset: str, version: int = 2021, output: str = "", mode: str = "batch", force: bool = False) -> dict:
    """Save a .max for an older 3ds Max (3ds Max saves back three versions, e.g. 2024 -> 2021).
    Writes '<name>_max<version>.max' next to the original unless output is given."""
    scene = _max_path(asset)
    out = Path(output) if output else _sibling(scene, f"max{version}")
    body = f"""
ok = saveMaxFile {ms(out)} saveAsVersion:{int(version)} quiet:true
mcpResult = mcpO #(#("saved", ok), #("file", {ms(out)}), #("version", {int(version)}))
"""
    return _run(body, mode, scene, force)


@tool
def texture_optimizer(asset: str = "", folder: str = "", max_size: int = 4096, jpeg_quality: int = 92, dry_run: bool = True) -> dict:
    """Shrink oversized texture maps of a model (or every image in a folder) to max_size pixels on the
    long side, in place and with the same name and format, so no relinking is needed. Originals are
    moved to an '_original_textures' folder next to them. Works without 3ds Max. dry_run=True (default)
    lists what would be resized and the space saved."""
    from PIL import Image

    if asset:
        f = _max_path(asset)
        images = {Path(st["resolved"]) for st in (_texture_status(t, f) for t in maxfile.max_texture_paths(f)) if st["resolved"]}
    elif folder:
        images = {p for p in Path(folder).rglob("*") if p.suffix.lower() in IMAGE_EXTS and "_original_textures" not in p.parts}
    else:
        raise ConnecterError("Give an asset or a folder")
    plan = []
    for img in sorted(images):
        if img.suffix.lower() not in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".tga", ".bmp"}:
            continue
        size = image_size(img)
        if size is None:
            try:
                with Image.open(img) as im:
                    size = im.size
            except Exception:
                continue
        if max(size) > max_size:
            scale = max_size / max(size)
            plan.append({"file": str(img), "from": list(size), "to": [round(size[0] * scale), round(size[1] * scale)],
                         "mb": round(img.stat().st_size / 2**20, 1)})
    if dry_run:
        return {"dry_run": True, "resize": plan, "count": len(plan), "current_mb": round(sum(p["mb"] for p in plan), 1)}
    done, saved = [], 0
    for p in plan:
        src = Path(p["file"])
        keep = src.parent / "_original_textures" / src.name
        keep.parent.mkdir(exist_ok=True)
        if not keep.exists():
            shutil.copy2(src, keep)
        with Image.open(keep) as im:
            im = im.resize(tuple(p["to"]), Image.LANCZOS)
            kw = {"quality": jpeg_quality} if src.suffix.lower() in (".jpg", ".jpeg") else {}
            if src.suffix.lower() in (".jpg", ".jpeg") and im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(src, **kw)
        saved += keep.stat().st_size - src.stat().st_size
        done.append({"file": str(src), "original": str(keep)})
    jid = journal.record("texture_optimizer", {"resized": done}, [{"type": "_restore_file", "payload": {"backup": d["original"], "target": d["file"]}} for d in done])
    return {"journal_id": jid, "resized": len(done), "saved_mb": round(saved / 2**20, 1)}


# ---------------------------------------------------------------- materials

@tool
def list_materials(asset: str = "", mode: str = "file", force: bool = False) -> dict:
    """Materials of a model. mode='file' (default) reads the names, renderer and plug-ins straight from the
    .max without 3ds Max; mode='script' or 'batch' also lists each material's class and texture maps."""
    if mode == "file":
        info = maxfile.summary(_max_path(asset))
        return {"materials": info.get("materials", []), "renderer": maxfile.renderer(info),
                "material_plugins": [p for p in info.get("plugins", []) if p.lower().startswith(("mtl", "vray", "corona"))]}
    scene = _scene_or_open(asset, mode)
    body = """
mats = #()
for m in sceneMaterials do (
    subs = if classOf m == MultiMaterial then (for s in m.materialList where s != undefined collect s) else #(m)
    for s in subs do (
        maps = for t in (getClassInstances BitmapTexture target:s) where t.filename != undefined collect t.filename
        append mats (mcpO #(#("name", s.name), #("class", (classOf s) as string), #("parent", m.name), #("maps", maps)))
    )
)
mcpResult = mcpO #(#("materials", mats), #("renderer", renderers.current as string))
"""
    return _run(body, mode, scene, force)


def _classify_maps(files: list[Path]) -> dict[str, str]:
    roles: dict[str, str] = {}
    for f in sorted(files, key=lambda p: len(p.name)):
        stem = f.stem.lower()
        for role, pat in MAP_ROLES.items():
            if role not in roles and re.search(rf"(^|[\s_.\-]){pat}($|[\s_.\-\d])", stem):
                roles[role] = str(f)
                break
    return roles


@tool
def create_material(textures: str | list[str], name: str = "", renderer: str = "corona", assign_to_selection: bool = False,
                    save_to_library: str = "", mode: str = "script", force: bool = False) -> dict:
    """Build a PBR material from a set of texture maps (a folder or a list of files), sorting them into
    diffuse, roughness/glossiness, normal, bump, metalness, opacity and displacement by name. renderer is
    'corona' (CoronaPhysicalMtl), 'vray' (VRayMtl) or 'physical'. Assign it to the selection in the open
    3ds Max (mode='script') and/or add it to a .mat library (save_to_library, works in batch too)."""
    files = [p for p in Path(textures).rglob("*") if p.suffix.lower() in IMAGE_EXTS] if isinstance(textures, str) and Path(textures).is_dir() \
        else [Path(t) for t in ([textures] if isinstance(textures, str) else textures)]
    roles = _classify_maps(files)
    if not roles:
        raise ConnecterError("No texture maps recognised by name (diffuse, roughness, normal, ...)")
    name = name or re.sub(r"[_\-\s]*(diff(use)?|albedo|basecolou?r|colou?r)$", "", Path(roles.get("diffuse", files[0])).stem, flags=re.I) or "Material"
    r = renderer.lower().replace("-", "")
    m = lambda role, linear=False: (f"(mcpMap {ms(roles[role])} {str(linear).lower()})" if role in roles else "undefined")
    if r == "corona":
        build = f"""
mat = CoronaPhysicalMtl name:{ms(name)}
mcpSet mat #baseTexmap {m('diffuse')}
if {str('roughness' in roles).lower()} then mcpSet mat #baseRoughnessTexmap {m('roughness', True)} else (
    if {str('glossiness' in roles).lower()} do (mcpSet mat #roughnessMode 1; mcpSet mat #baseRoughnessTexmap {m('glossiness', True)}))
if {str('normal' in roles).lower()} do mcpSet mat #baseBumpTexmap (CoronaNormal normalMap:{m('normal', True)})
if {str('bump' in roles and 'normal' not in roles).lower()} do mcpSet mat #baseBumpTexmap {m('bump', True)}
mcpSet mat #metalnessTexmap {m('metalness', True)}
mcpSet mat #opacityTexmap {m('opacity', True)}
mcpSet mat #displacementTexmap {m('displacement', True)}
"""
    elif r == "vray":
        build = f"""
mat = VRayMtl name:{ms(name)}
mat.reflection = color 255 255 255
mcpSet mat #texmap_diffuse {m('diffuse')}
if {str('roughness' in roles).lower()} then (mcpSet mat #brdf_useRoughness true; mcpSet mat #texmap_reflectionGlossiness {m('roughness', True)}) else
    mcpSet mat #texmap_reflectionGlossiness {m('glossiness', True)}
if {str('normal' in roles).lower()} do mcpSet mat #texmap_bump (VRayNormalMap normal_map:{m('normal', True)})
if {str('bump' in roles and 'normal' not in roles).lower()} do mcpSet mat #texmap_bump {m('bump', True)}
mcpSet mat #texmap_metalness {m('metalness', True)}
mcpSet mat #texmap_opacity {m('opacity', True)}
mcpSet mat #texmap_displacement {m('displacement', True)}
"""
    elif r == "physical":
        build = f"""
mat = PhysicalMaterial name:{ms(name)}
mcpSet mat #base_color_map {m('diffuse')}
if {str('roughness' in roles).lower()} then mcpSet mat #roughness_map {m('roughness', True)} else (
    if {str('glossiness' in roles).lower()} do (mcpSet mat #roughness_inv true; mcpSet mat #roughness_map {m('glossiness', True)}))
if {str('normal' in roles).lower()} do mcpSet mat #bump_map (Normal_Bump normal_map:{m('normal', True)})
mcpSet mat #metalness_map {m('metalness', True)}
mcpSet mat #cutout_map {m('opacity', True)}
mcpSet mat #displacement_map {m('displacement', True)}
"""
    else:
        raise ConnecterError("renderer must be 'corona', 'vray' or 'physical'")
    lib = Path(save_to_library) if save_to_library else None
    body = f"""
fn mcpMap f linear = (
    local b = Bitmaptexture name:(getFilenameFile f)
    -- Setting .bitmap also sets the file name; linear data maps keep gamma 1.0 this way.
    b.bitmap = if linear then (openBitmap f gamma:1.0) else (openBitmap f)
    b
)
unset = #()
fn mcpSet m p v = (if v != undefined do (if isProperty m p then setProperty m p v else append unset (p as string)))
{build}
assigned = 0
if {str(assign_to_selection).lower()} do (for o in selection do (o.material = mat; assigned += 1))
{f'''lib = if doesFileExist {ms(lib)} then loadTempMaterialLibrary {ms(lib)} else materialLibrary()
append lib mat
saveTempMaterialLibrary lib {ms(lib)}''' if lib else ""}
mcpResult = mcpO #(#("material", mat.name), #("class", (classOf mat) as string), #("assigned_to", assigned), #("unset_properties", unset))
"""
    result = execute(body, mode, None, force)
    return {"name": name, "maps": roles, **result}


@tool
def manage_materials(library: str, action: str = "list", names: list[str] | None = None, from_asset: str = "",
                     mode: str = "batch", force: bool = False) -> dict:
    """Work with a 3ds Max material library (.mat): action='list' its materials, 'add' materials (by name,
    or all when names is empty) from the .max `from_asset` (or the open scene in script mode), or 'remove'
    materials by name. A backup of the library is kept for add/remove (`undo` restores it)."""
    lib = Path(library)
    if action not in ("list", "add", "remove"):
        raise ConnecterError("action must be list, add or remove")
    if action != "add" and not lib.exists():
        raise ConnecterError(f"{lib} does not exist")
    want = ms_array([n.lower() for n in names or []])
    scene = _max_path(from_asset) if action == "add" and from_asset else None
    if action == "list":
        body = f"""
lib = loadTempMaterialLibrary {ms(lib)}
mcpResult = mcpO #(#("materials", for m in lib collect (mcpO #(#("name", m.name), #("class", (classOf m) as string)))))
"""
    elif action == "add":
        body = f"""
lib = if doesFileExist {ms(lib)} then loadTempMaterialLibrary {ms(lib)} else materialLibrary()
want = {want}
added = #()
for m in sceneMaterials where want.count == 0 or findItem want (toLower m.name) > 0 do (append lib m; append added m.name)
saveTempMaterialLibrary lib {ms(lib)}
mcpResult = mcpO #(#("added", added), #("library_count", lib.count))
"""
    else:
        body = f"""
lib = loadTempMaterialLibrary {ms(lib)}
want = {want}
removed = #()
for i = lib.count to 1 by -1 where findItem want (toLower lib[i].name) > 0 do (append removed lib[i].name; deleteItem lib i)
saveTempMaterialLibrary lib {ms(lib)}
mcpResult = mcpO #(#("removed", removed), #("library_count", lib.count))
"""
    backup = _backup_file(lib) if action != "list" and lib.exists() and mode == "batch" else None
    result = _run(body, mode, scene, force)
    if backup:
        result["journal_id"] = journal.record("manage_materials", {"library": str(lib), "action": action, "names": names},
                                              [{"type": "_restore_file", "payload": {"backup": str(backup), "target": str(lib)}}])
    return result


# ---------------------------------------------------------------- scene assembly (open 3ds Max)

SCENE_HELPERS = r"""
fn mcpDesc n = (local r = #(n); for c in n.children do join r (mcpDesc c); r)
fn mcpMerge f nm = (
    clearSelection()
    if not (mergeMaxFile f #select #mergeDups #useSceneMtlDups #neverReparent quiet:true) do throw ("Could not merge " + f)
    local sel = selection as array
    if sel.count == 0 do throw ("Nothing merged from " + f)
    local g = if sel.count == 1 then sel[1] else (group sel name:(uniqueName nm))
    local bb = nodeGetBoundingBox g (matrix3 1)
    if sel.count == 1 do (bb = mcpBBox (mcpDesc g); if bb == undefined do bb = #(g.pos, g.pos))
    -- Pivot-independent placement: move so the model's bottom-centre sits at the origin.
    move g ([0, 0, 0] - [(bb[1].x + bb[2].x) / 2, (bb[1].y + bb[2].y) / 2, bb[1].z])
    g
)
fn mcpClone g = (
    local nn
    maxOps.cloneNodes (mcpDesc g) cloneType:#instance newNodes:&nn
    for x in nn where x.parent == undefined do return x
    nn[1]
)
fn mcpLayer nodes nm = (
    if nm != "" do (
        local l = LayerManager.getLayerFromName nm
        if l == undefined do l = LayerManager.newLayerFromName nm
        for n in nodes do for d in (mcpDesc n) do l.addNode d
    )
)
fn mcpPlace g tm0 posCm rotZ = (
    -- tm0 is the transform right after mcpMerge, when the model's bottom-centre is at the origin.
    g.transform = tm0
    about [0, 0, 0] rotate g (eulerAngles 0 0 rotZ)
    move g (posCm * (1.0 / (mcpToCm 1.0)))
)
fn mcpFitTo g p fit = (
    -- Align g's bottom-centre to placeholder p's (in p's own orientation), optionally scaling to fit it.
    local lb = nodeGetBoundingBox p p.transform
    local sp = p.transform.scalepart
    local phSize = (lb[2] - lb[1]) * sp
    local gb = nodeGetBoundingBox g (matrix3 1)
    local gSize = gb[2] - gb[1]
    local c0 = [(gb[1].x + gb[2].x) / 2, (gb[1].y + gb[2].y) / 2, gb[1].z]
    local target = [(lb[1].x + lb[2].x) / 2, (lb[1].y + lb[2].y) / 2, lb[1].z] * p.transform
    local s = 1.0
    if fit == "footprint" and gSize.x > 0 and gSize.y > 0 do s = amin (phSize.x / gSize.x) (phSize.y / gSize.y)
    if fit == "bbox" and gSize.x > 0 and gSize.y > 0 and gSize.z > 0 do s = amin #(phSize.x / gSize.x, phSize.y / gSize.y, phSize.z / gSize.z)
    g.transform = g.transform * (transMatrix ([0, 0, 0] - c0)) * (scaleMatrix [s, s, s]) * (p.transform.rotationpart as matrix3) * (transMatrix target)
    s
)
"""


def _scene_script(body: str) -> dict:
    """Scene-assembly tools always work in the open 3ds Max and are never run headless."""
    return execute(SCENE_HELPERS + body, "script")


def _asset_file(asset: str) -> Path:
    p = Path(_get_asset(asset)["FileFullPath"]) if GUID_RE.match(asset.strip()) else Path(asset)
    if not p.exists():
        raise ConnecterError(f"{p} does not exist")
    return p


@tool
def merge_into_max(asset: str, position_cm: list[float] | None = None, rotation_z: float = 0.0, layer: str = "",
                   group: bool = True) -> dict:
    """MAXScript that merges a library model into the open 3ds Max scene: grouped, bottom-centre placed at
    position_cm ([x, y, z] in centimetres, default origin), rotated rotation_z degrees, on `layer`.
    Run the returned script with the 3ds Max MCP's execute_maxscript."""
    f = _asset_file(asset)
    pos = position_cm or [0, 0, 0]
    body = f"""
g = mcpMerge {ms(f)} {ms(f.stem)}
mcpPlace g g.transform [{pos[0]}, {pos[1]}, {pos[2] if len(pos) > 2 else 0}] {rotation_z}
mcpLayer #(g) {ms(layer)}
nm = g.name
{"" if group else "if isGroupHead g do (explodeGroup g; g = undefined)"}
mcpResult = mcpO #(#("merged", nm), #("file", {ms(f)}))
"""
    return _scene_script(body)


@tool
def swap_placeholder(asset: str, placeholders: str | list[str], fit: str = "none", delete_placeholders: bool = False,
                     layer: str = "") -> dict:
    """MAXScript that replaces placeholder objects (boxes etc., by name list or wildcard like 'PH_SOFA*') with
    instanced copies of a library model, matching each placeholder's position, base and rotation.
    fit: 'none', 'footprint' (scale to the placeholder's width/depth) or 'bbox' (fit all three sides).
    Placeholders are hidden, or deleted with delete_placeholders."""
    f = _asset_file(asset)
    if fit not in ("none", "footprint", "bbox"):
        raise ConnecterError("fit must be none, footprint or bbox")
    pats = [placeholders] if isinstance(placeholders, str) else placeholders
    body = f"""
pats = {ms_array(pats)}
phs = for o in objects where (for p in pats where matchPattern o.name pattern:p collect p).count > 0 collect o
if phs.count == 0 do throw "No placeholder objects match"
src = mcpMerge {ms(f)} {ms(f.stem)}
-- Clone before fitting, so every copy starts from the unplaced model.
gs = #(src)
for i = 2 to phs.count do append gs (mcpClone src)
placed = #()
for i = 1 to phs.count do (
    p = phs[i]
    g = gs[i]
    s = mcpFitTo g p {ms(fit)}
    mcpLayer #(g) (if {ms(layer)} != "" then {ms(layer)} else p.layer.name)
    append placed (mcpO #(#("placeholder", p.name), #("model", g.name), #("scale", s)))
    if {str(delete_placeholders).lower()} then delete p else hide p
)
mcpResult = mcpO #(#("swapped", placed))
"""
    return _scene_script(body)


@tool
def variant_swap(target: str, asset: str, keep_scale: bool = False) -> dict:
    """MAXScript that swaps one object or group in the open scene (by name) for another library model, e.g.
    another colour or size of the same chair, keeping its position and rotation. The old one is deleted."""
    f = _asset_file(asset)
    body = f"""
p = getNodeByName {ms(target)}
if p == undefined do throw ("No object named " + {ms(target)})
g = mcpMerge {ms(f)} {ms(f.stem)}
s = mcpFitTo g p {ms("bbox" if keep_scale else "none")}
mcpLayer #(g) p.layer.name
oldName = p.name
delete (mcpDesc p)
mcpResult = mcpO #(#("replaced", oldName), #("with", g.name), #("scale", s))
"""
    return _scene_script(body)


@tool
def furnish_room(placements: list[dict], room_object: str = "", layer: str = "FURNITURE") -> dict:
    """MAXScript that furnishes a room in the open scene from a list of placements:
    [{"asset": <path or GUID>, "position_cm": [x, y, z], "rotation_z": 90}, ...]. Positions are in
    centimetres, measured from the room_object's bottom-left corner when given (e.g. the floor), otherwise
    from the world origin. Each model is merged once and instanced for repeats."""
    if not placements:
        raise ConnecterError("placements is empty")
    files = {}
    rows = []
    for p in placements:
        f = _asset_file(p["asset"])
        files.setdefault(str(f).lower(), f)
        pos = p.get("position_cm") or [0, 0, 0]
        rows.append(f"#({ms(f)}, [{pos[0]}, {pos[1]}, {pos[2] if len(pos) > 2 else 0}], {float(p.get('rotation_z', 0))})")
    body = f"""
origin = [0, 0, 0]
if {ms(room_object)} != "" do (
    r = getNodeByName {ms(room_object)}
    if r == undefined do throw ("No object named " + {ms(room_object)})
    origin = [mcpToCm r.min.x, mcpToCm r.min.y, mcpToCm r.max.z]
)
rows = #({", ".join(rows)})
merged = #(); mergedFiles = #(); tms = #(); used = #(); placed = #()
for row in rows do (
    i = findItem mergedFiles (toLower row[1])
    if i == 0 do (
        x = mcpMerge row[1] (getFilenameFile row[1])
        append mergedFiles (toLower row[1]); append merged x; append tms x.transform; append used false
        i = merged.count
    )
    g = if used[i] then (mcpClone merged[i]) else (used[i] = true; merged[i])
    mcpPlace g tms[i] (origin + row[2]) row[3]
    append placed g
)
mcpLayer placed {ms(layer)}
mcpResult = mcpO #(#("placed", for g in placed collect g.name), #("models", mergedFiles.count))
"""
    return _scene_script(body)


@tool
def scene_to_library(name: str, category: str, objects: list[str] | None = None, tags: list[str] | None = None,
                     description: str = "", stage: str = "export") -> dict:
    """Save objects from the open scene as a new library model in two steps.
    stage='export' returns MAXScript that saves the objects (or the selection) to
    models_dir/<category>/<name>/<name>.max with a framed preview image; run it in 3ds Max.
    stage='catalogue' then adds the saved file to Connecter with the preview, tags (default: the category)
    and description."""
    s = Settings.load()
    if not s.models_dir:
        raise ConnecterError("Set models_dir in the config first")
    folder = s.models_dir.joinpath(*[p for p in re.split(r"[/\\]", category) if p]) / name
    max_out, preview = folder / f"{name}.max", folder / f"{name}_preview.jpg"
    if stage == "catalogue":
        if not max_out.exists():
            raise ConnecterError(f"{max_out} doesn't exist yet. Run the export script first.")
        previews = [str(preview)] if preview.exists() else []
        if not previews and maxfile.thumbnail(max_out, folder / f"{name}_thumb.png"):
            previews = [str(folder / f"{name}_thumb.png")]
        tag = tags or ["/".join(p for p in re.split(r"[/\\]", category) if p)]
        return catalogue_asset(str(max_out), tags=tag, previews=previews, description=description)
    if stage != "export":
        raise ConnecterError("stage must be 'export' or 'catalogue'")
    body = f"""
names = {ms_array(objects or [])}
nodes = if names.count > 0 then (for n in names where getNodeByName n != undefined collect getNodeByName n) else (selection as array)
if nodes.count == 0 do throw "Nothing to export: select objects or pass their names"
allNodes = #(); for n in nodes do join allNodes (mcpDesc n)
makeDir {ms(folder)} all:true
saveNodes allNodes {ms(max_out)} quiet:true
-- Frame the objects for the preview, then put the view back as it was. Camera views are
-- grabbed as they are, so the user's camera is never moved.
oldSel = selection as array
isCam = viewport.getType() == #view_camera
if not isCam do (
    vtm = viewport.getTM(); vfov = try (viewport.getFOV()) catch undefined
    select allNodes
    max zoomext sel
)
img = gw.getViewportDib()
img.filename = {ms(preview)}
save img
close img
if not isCam do (try (viewport.setTM vtm) catch (); if vfov != undefined do try (viewport.setFOV vfov) catch ())
if oldSel.count > 0 then select oldSel else clearSelection()
redrawViews()
mcpResult = mcpO #(#("saved", {ms(max_out)}), #("preview", {ms(preview)}), #("objects", allNodes.count))
"""
    out = _scene_script(body)
    out["next_step"] = "After the script has run, call scene_to_library again with stage='catalogue'."
    return out


@tool
def scene_asset_report(scene: str) -> dict:
    """What a .max scene uses, read without 3ds Max: scene totals, renderer, external files with their
    status, and the library models whose maps or proxies it references (so you can see which library
    assets went into a project)."""
    max_file = _max_path(scene)
    info = maxfile.summary(max_file)
    externals = [_texture_status(t, max_file) for t in info.get("external_files", [])]
    used = library_models_for([e["resolved"] for e in externals if e["resolved"]])
    return {"scene": str(max_file), "renderer": maxfile.renderer(info), "max_version": info.get("max_version"),
            "mesh_totals": info.get("mesh_totals"), "scene_totals": info.get("scene_totals"),
            "materials": len(info.get("materials", [])),
            "external_files": {"total": len(externals), "missing": [e["path"] for e in externals if e["status"] == "missing"],
                               "found_by_name": [e["path"] for e in externals if e["status"] == "found_by_name"]},
            "library_models_used": used}


def library_models_for(paths: list[str]) -> list[dict]:
    """Catalogued .max models whose folder contains any of these files (their maps, proxies...)."""
    with _db() as db:
        models = [a for a in db.assets() if a.path.lower().endswith(".max")]
    by_folder: dict[str, list] = {}
    for a in models:
        by_folder.setdefault(str(Path(a.path).parent).lower(), []).append(a)
    hits: dict[str, dict] = {}
    for p in paths:
        for parent in Path(p).parents:
            group = by_folder.get(str(parent).lower())
            if group:
                for a in group:
                    h = hits.setdefault(a.path, {"asset": a.path, "tags": a.tags, "files_used": 0})
                    h["files_used"] += 1
                break
    return sorted(hits.values(), key=lambda h: -h["files_used"])


@tool
def collect_for_project(scene: str, destination: str, relink: bool = True, mode: str = "batch", dry_run: bool = True,
                        force: bool = False) -> dict:
    """Archive a scene for a project or hand-off: copy the .max and every external file it uses (maps,
    proxies, IES) into destination (files go to destination/maps), then, with relink, re-point the copied
    scene at the copies. The original scene is not changed. dry_run=True (default) lists the files."""
    max_file = _max_path(scene)
    dest = Path(destination)
    maps_dir = dest / "maps"
    copies, missing, taken = {}, [], set()
    for t in maxfile.max_texture_paths(max_file):
        st = _texture_status(t, max_file)
        if not st["resolved"]:
            missing.append(t)
            continue
        name = Path(st["resolved"]).name
        target = maps_dir / name
        n = 1
        while target.name.lower() in taken:
            target = maps_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1
        taken.add(target.name.lower())
        copies[t] = {"from": st["resolved"], "to": str(target)}
    new_scene = dest / max_file.name
    plan = {"scene": str(max_file), "copy_scene_to": str(new_scene), "files": len(copies), "missing": missing,
            "size_mb": round(sum(Path(c["from"]).stat().st_size for c in copies.values()) / 2**20, 1)}
    if dry_run:
        return {"dry_run": True, **plan, "copies": copies}
    maps_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(max_file, new_scene)
    for c in copies.values():
        if not Path(c["to"]).exists():
            shutil.copy2(c["from"], c["to"])
    result = {}
    if relink and copies:
        frm, to = _relink_lists({k: v["to"] for k, v in copies.items()})
        body = f"""
n = 0
for o in mcpFileOwners() do n += mcpRelinkProps o {frm} {to}
saveMaxFile (maxFilePath + maxFileName) quiet:true
mcpResult = mcpO #(#("relinked", n))
"""
        result = _run(body, mode, new_scene, force)
    jid = journal.record("collect_for_project", plan)
    return {"journal_id": jid, **plan, **result}


@tool
def consistency_check(scene: str = "", mode: str = "file", force: bool = False) -> dict:
    """Pre-render checks for a scene. mode='file' (default, no 3ds Max): missing or misplaced files, mixed
    renderers, newer-than-installed version. mode='script'/'batch' adds checks inside 3ds Max: units,
    gamma, materials that don't match the renderer, non-uniform or negative scale, empty objects,
    duplicate material names and empty layers."""
    if mode == "file":
        max_file = _max_path(scene)
        info = maxfile.summary(max_file)
        issues = []
        st = [_texture_status(t, max_file) for t in info.get("external_files", [])]
        if (n := sum(1 for s in st if s["status"] == "missing")):
            issues.append(f"{n} external files are missing")
        if (n := sum(1 for s in st if s["status"] == "found_by_name")):
            issues.append(f"{n} files are found by name only (stored paths are wrong)")
        plugs = " ".join(info.get("plugins", [])).lower()
        found = [r for r in ("vray", "corona") if r in plugs]
        if len(found) > 1:
            issues.append("Uses both V-Ray and Corona plug-ins (mixed materials)")
        ver = (info.get("max_version") or "").split(",")[0]
        year = _year()
        if ver.isdigit() and year and 2000 + int(ver) - 2 > int(year):
            issues.append(f"Saved in a newer 3ds Max ({2000 + int(ver) - 2}) than the installed {year}")
        return {"scene": str(max_file), "renderer": maxfile.renderer(info), "issues": issues, "ok": not issues}
    scene_path = _scene_or_open(scene, mode)
    body = """
issues = #()
r = renderers.current as string
isCorona = matchPattern r pattern:"*Corona*"
isVRay = matchPattern r pattern:"*V_Ray*"
if units.SystemType != #centimeters and units.SystemType != #millimeters do append issues ("System units are " + units.SystemType as string)
if IDisplayGamma.colorCorrectionMode == #none do append issues "Colour management (gamma/OCIO) is off"
bad = 0
for m in sceneMaterials do (
    c = (classOf m) as string
    if (isCorona and matchPattern c pattern:"VRay*") or (isVRay and matchPattern c pattern:"Corona*") do bad += 1
)
if bad > 0 do append issues ((bad as string) + " materials don't match the renderer " + r)
nonUni = for o in objects where (s = o.scale; abs (s.x - s.y) > 0.001 or abs (s.y - s.z) > 0.001) collect o.name
if nonUni.count > 0 do append issues ((nonUni.count as string) + " objects have non-uniform scale")
neg = for o in objects where o.scale.x < 0 or o.scale.y < 0 or o.scale.z < 0 collect o.name
if neg.count > 0 do append issues ((neg.count as string) + " objects have negative (mirrored) scale")
empty = for o in geometry where (try ((getPolygonCount o)[1]) catch 1) == 0 collect o.name
if empty.count > 0 do append issues ((empty.count as string) + " geometry objects have no faces")
names = for m in sceneMaterials collect m.name
dups = 0
for i = 1 to names.count do if (for j = i + 1 to names.count where names[j] == names[i] collect j).count > 0 do dups += 1
if dups > 0 do append issues ((dups as string) + " material names are used more than once")
emptyLayers = #()
for i = 1 to LayerManager.count - 1 do (l = LayerManager.getLayer i; nn = #(); l.nodes &nn; if nn.count == 0 do append emptyLayers l.name)
if emptyLayers.count > 0 do append issues ((emptyLayers.count as string) + " layers are empty")
missing = #()
ATSOps.Refresh()
atsFiles = #()
ATSOps.GetFiles &atsFiles
for f in atsFiles where not doesFileExist f and (getFilenameType f) != "" do append missing f
if missing.count > 0 do append issues ((missing.count as string) + " external files are missing")
mcpResult = mcpO #(#("renderer", r), #("issues", issues), #("non_uniform_scale", nonUni), #("negative_scale", neg),
    #("empty_objects", empty), #("missing_files", missing))
"""
    return _run(body, mode, scene_path, force)
