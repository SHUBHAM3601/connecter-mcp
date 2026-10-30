"""AI cataloguing and visual search.

Cataloguing (auto_tag, auto_describe, extract_metadata, detect_brand, batch_catalogue) works from file
names, folders, .max metadata and, when built, the visual index.

Visual search embeds each asset's first preview (or the thumbnail inside its .max) with CLIP. CLIP runs in
a separate Python through uv (ai_worker.py) at idle priority so it never competes with 3ds Max. Without
it, backend='basic' indexes colour and layout features with Pillow instead (no text search).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from array import array
from pathlib import Path

from . import jobs, journal, maxfile
from .client import ConnecterError
from .config import HOME_DIR, Settings
from .files import IMAGE_EXTS, MODEL_EXTS, classify_images
from .maintenance import _norm
from .maxtools import measurements
from .server import GUID_RE, _db, _get_asset, _tags, catalogue_asset, client, search_assets, tag_assets, tool

INDEX_DIR = HOME_DIR / "visual_index"
THUMBS = HOME_DIR / "thumbs"
WORKER = Path(__file__).with_name("ai_worker.py")
AI_PYTHON = "3.12"

BRANDS = [
    "&Tradition", "Agape", "Andreu World", "Antoniolupi", "Arper", "Artemide", "Audo", "Axor", "B&B Italia", "Baxter",
    "Bocci", "Boffi", "Bonaldo", "Calligaris", "Carl Hansen", "Cassina", "Cattelan Italia", "Ceccotti", "Cosentino",
    "De Padova", "De Sede", "Dedon", "Delightfull", "Dornbracht", "Driade", "Duravit", "Edra", "Ethimo", "Expormim",
    "Fendi Casa", "Flexform", "Flos", "Fontana Arte", "Foscarini", "Frag", "Fritz Hansen", "Gaggenau", "Gallotti&Radice",
    "Gervasoni", "Gessi", "Giorgetti", "Glas Italia", "Grohe", "Gubi", "Hansgrohe", "Hay", "Herman Miller", "Holly Hunt",
    "Kartell", "Knoll", "Kohler", "Kettal", "Lee Broom", "Ligne Roset", "Living Divani", "Louis Poulsen", "Magis",
    "Maxalto", "Menu", "Meridiani", "Miele", "Minotti", "Molteni", "Moooi", "Moroso", "Muuto", "Natuzzi", "Normann Copenhagen",
    "Paola Lenti", "Pedrali", "Poliform", "Poltrona Frau", "Porada", "Porro", "Rimadesio", "Roche Bobois", "Rubelli",
    "Santa & Cole", "Smeg", "Sub-Zero", "Tacchini", "Tom Dixon", "Tribu", "Vibia", "Villeroy & Boch", "Visionnaire",
    "Vitra", "Wittmann", "Zanotta", "Restoration Hardware", "RH", "Baker", "Gessi", "Lasvit", "Preciosa", "Bover",
    "Marset", "Luceplan", "Oluce", "Nemo", "Davide Groppi", "Catellani & Smith", "Bertjan Pot", "Ferm Living", "Ton",
]
MATERIAL_WORDS = ["oak", "walnut", "ash", "teak", "wood", "marble", "travertine", "terrazzo", "concrete", "stone", "fabric",
                  "linen", "velvet", "boucle", "wool", "leather", "brass", "bronze", "copper", "gold", "chrome", "steel",
                  "aluminium", "metal", "glass", "ceramic", "porcelain", "rattan", "wicker", "cane", "plastic", "resin"]


# ---------------------------------------------------------------- embeddings

def _uv() -> str:
    exe = shutil.which("uv") or shutil.which("uv.exe")
    if not exe:
        raise ConnecterError("uv was not found on PATH; it is needed to run the CLIP worker. Use backend='basic'.")
    return exe


def _clip(images: list[str] = (), texts: list[str] = (), threads: int = 2, timeout: int = 1800) -> dict:
    work = Path(tempfile.mkdtemp(prefix="connecter-mcp-ai-"))
    req, resp = work / "req.json", work / "resp.json"
    req.write_text(json.dumps({"images": list(images), "texts": list(texts), "threads": threads}), "utf-8")
    cmd = [_uv(), "run", "--no-project", "--quiet", "--python", AI_PYTHON, "--with", "fastembed>=0.4", "--with", "pillow",
           "python", "-I", str(WORKER), str(req), str(resp)]
    flags = subprocess.IDLE_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags, cwd=work)
    if not resp.exists():
        raise ConnecterError(f"CLIP worker failed (exit {proc.returncode}): {proc.stderr[-800:]}")
    out = json.loads(resp.read_text("utf-8"))
    shutil.rmtree(work, ignore_errors=True)
    return out


def _basic(path: str) -> list[float] | None:
    """Colour histogram (HSV) plus a tiny grey layout thumbnail, L2-normalised."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            hsv = im.resize((64, 64)).convert("HSV")
            hist = [0.0] * 128
            for h, s, v in hsv.getdata():
                hist[(h * 8 // 256) * 16 + (s * 4 // 256) * 4 + v * 4 // 256] += 1
            grey = [p / 255 - 0.5 for p in im.convert("L").resize((8, 8)).getdata()]
    except Exception:
        return None
    vec = [x / 4096 for x in hist] + [g * 0.5 for g in grey]
    n = math.sqrt(sum(x * x for x in vec)) or 1
    return [x / n for x in vec]


def _norm_vec(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1
    return [x / n for x in v]


def _enc(v: list[float]) -> str:
    return base64.b64encode(array("f", v).tobytes()).decode()


def _dec(s: str) -> list[float]:
    a = array("f")
    a.frombytes(base64.b64decode(s))
    return list(a)


def _index_file(backend: str) -> Path:
    return INDEX_DIR / f"{backend}.json"


def _load_index(backend: str) -> dict:
    try:
        return json.loads(_index_file(backend).read_text("utf-8"))
    except (OSError, ValueError):
        return {"backend": backend, "items": {}}


def _save_index(idx: dict) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _index_file(idx["backend"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(idx), "utf-8")
    tmp.replace(_index_file(idx["backend"]))


def _best_backend() -> str:
    return "clip" if _load_index("clip")["items"] else "basic"


def _first_previews() -> dict[str, str]:
    """Asset path (lower case) -> its first stored preview image."""
    pdir = client.paths.previews_dir
    with _db() as db:
        paths = {a.id: a.path for a in db.assets()}
        out = {}
        for item, local, _ in db.preview_files():
            if item in paths and paths[item].lower() not in out and pdir and (pdir / local).exists():
                out[paths[item].lower()] = str(pdir / local)
    return out


def asset_image(path: str, previews: dict[str, str] | None = None) -> str | None:
    """The image that represents an asset: its first Connecter preview, else the .max thumbnail."""
    if previews and path.lower() in previews:
        return previews[path.lower()]
    p = Path(path)
    if p.suffix.lower() in IMAGE_EXTS:
        return str(p)
    if p.suffix.lower() == ".max" and p.exists():
        thumb = THUMBS / (hashlib.md5(path.lower().encode()).hexdigest() + ".png")
        if thumb.exists() or maxfile.thumbnail(p, thumb):
            return str(thumb)
    return None


def _embed_images(paths: list[str], backend: str, threads: int = 2) -> list[list[float] | None]:
    if backend == "basic":
        return [_basic(p) for p in paths]
    return [(_norm_vec(v) if v else None) for v in _clip(images=paths, threads=threads)["images"]]


def _embed_text(text: str) -> list[float]:
    return _norm_vec(_clip(texts=[text], threads=2)["texts"][0])


def _rank(query: list[float], idx: dict, limit: int, exclude: str = "", tag: str = "") -> list[dict]:
    tags_by_path = {}
    if tag:
        tags_by_path = {a["path"].lower(): a["tags"] for a in search_assets(tag=tag, limit=1000000)["assets"]}
    scored = []
    for path, item in idx["items"].items():
        if path.lower() == exclude.lower() or (tag and path.lower() not in tags_by_path):
            continue
        v = _dec(item["vec"])
        if len(v) != len(query):
            continue
        scored.append((sum(a * b for a, b in zip(query, v)), path, item["image"]))
    scored.sort(reverse=True)
    out = []
    for s, p, img in scored:
        if len(out) >= limit:
            break
        if Path(p).exists():  # the index can outlive files deleted since it was built
            out.append({"asset": p, "score": round(s, 3), "image": img})
    return out


def _build(job, items: list[tuple[str, str]], backend: str, threads: int, chunk: int = 48) -> dict:
    idx = _load_index(backend)
    done = failed = 0
    for i in range(0, len(items), chunk):
        if job and job.cancelled:
            break
        part = items[i:i + chunk]
        vecs = _embed_images([img for _, img in part], backend, threads)
        for (asset, img), v in zip(part, vecs):
            if v:
                idx["items"][asset] = {"image": img, "mtime": Path(img).stat().st_mtime, "vec": _enc(v)}
                done += 1
            else:
                failed += 1
        _save_index(idx)
        if job:
            job.update((i + len(part)) / len(items), f"{i + len(part)}/{len(items)} images")
    return {"indexed": done, "failed": failed, "index_size": len(idx["items"]), "backend": backend}


@tool
def build_index(folder: str = "", tag: str = "", backend: str = "clip", rebuild: bool = False, limit: int = 0,
                threads: int = 2, background: bool = True) -> dict:
    """Build (or update) the visual index used by semantic_search, find_similar, style_match,
    identify_render and auto_tag. Each model is indexed from its first preview or the thumbnail inside its
    .max. backend='clip' understands content and text (runs in a separate low-priority process, first use
    downloads the model, about 350 MB); backend='basic' uses colour and layout only. Only new or changed
    images are processed unless rebuild=True. Runs as a background job by default (see `jobs`)."""
    if backend not in ("clip", "basic"):
        raise ConnecterError("backend must be 'clip' or 'basic'")
    assets = [a["path"] for a in search_assets(folder=folder, tag=tag, limit=1000000)["assets"]
              if Path(a["path"]).suffix.lower() in MODEL_EXTS]
    previews = _first_previews()
    idx = _load_index(backend)
    with _db() as db:
        catalogued = {a.path for a in db.assets()}
    stale = [p for p in idx["items"] if p not in catalogued]
    if stale:
        for p in stale:
            del idx["items"][p]
        _save_index(idx)
    todo = []
    for path in assets:
        img = asset_image(path, previews)
        if not img:
            continue
        old = idx["items"].get(path)
        if rebuild or not old or old["image"] != img or old.get("mtime") != Path(img).stat().st_mtime:
            todo.append((path, img))
    if limit:
        todo = todo[:limit]
    if not todo:
        return {"message": "Index is up to date", "index_size": len(idx["items"]), "backend": backend}
    if background:
        job = jobs.submit("build_index", _build, todo, backend, threads, details={"backend": backend, "images": len(todo)})
        return {"job_id": job.id, "images_to_index": len(todo), "note": "Check progress with jobs(action='status')."}
    return _build(None, todo, backend, threads)


@tool
def semantic_search(query: str, limit: int = 20, tag: str = "") -> dict:
    """Find models by what they look like, in plain words: 'round travertine coffee table', 'brass wall
    sconce', 'olive tree in a pot'. Needs build_index(backend='clip'). Optionally limit to a tag."""
    idx = _load_index("clip")
    if not idx["items"]:
        raise ConnecterError("The CLIP index is empty. Run build_index(backend='clip') first.")
    return {"query": query, "results": _rank(_embed_text(f"a 3d render of {query}"), idx, limit, tag=tag)}


def _query_vec(asset: str = "", image: str = "", backend: str = "") -> tuple[list[float], str, str]:
    backend = backend or _best_backend()
    idx = _load_index(backend)
    path = ""
    if asset:
        path = _get_asset(asset)["FileFullPath"] if GUID_RE.match(asset.strip()) else asset
        hit = next((v for k, v in idx["items"].items() if k.lower() == path.lower()), None)
        if hit:
            return _dec(hit["vec"]), backend, path
        image = asset_image(path, _first_previews()) or ""
        if not image:
            raise ConnecterError("This asset has no preview or thumbnail to compare")
    if not image or not Path(image).exists():
        raise ConnecterError("Give an asset or an existing image file")
    v = _embed_images([image], backend)[0]
    if not v:
        raise ConnecterError(f"Could not read {image}")
    return v, backend, path


@tool
def find_similar(asset: str = "", image: str = "", limit: int = 12, tag: str = "", backend: str = "") -> dict:
    """Models that look like a given asset or image (alternatives, duplicates under other names, other
    colours). Uses the CLIP index if built, otherwise the basic colour index."""
    q, backend, path = _query_vec(asset, image, backend)
    return {"backend": backend, "results": _rank(q, _load_index(backend), limit, exclude=path, tag=tag)}


def _palette(path: str, n: int = 5) -> list[str]:
    from PIL import Image

    with Image.open(path) as im:
        small = im.convert("RGB").resize((96, 96)).quantize(n)
        pal = small.getpalette()[:n * 3]
        counts = sorted(small.getcolors(), reverse=True)
    return ["#%02x%02x%02x" % tuple(pal[i * 3:i * 3 + 3]) for _, i in counts]


@tool
def style_match(images: list[str], limit: int = 20, tag: str = "", backend: str = "") -> dict:
    """Models that fit a mood board or reference images (combined), plus the references' main colours.
    Use tag to search one category, e.g. '02_FURNITURES'."""
    backend = backend or _best_backend()
    vecs = [v for v in _embed_images(images, backend) if v]
    if not vecs:
        raise ConnecterError("None of the reference images could be read")
    mean = _norm_vec([sum(col) / len(vecs) for col in zip(*vecs)])
    return {"backend": backend, "palette": {i: _palette(i) for i in images if Path(i).exists()},
            "results": _rank(mean, _load_index(backend), limit, tag=tag)}


@tool
def identify_render(image: str, crop: list[float] | None = None, limit: int = 8, backend: str = "") -> dict:
    """Which library model is this? Give a render, photo or screenshot, optionally with crop
    [left, top, right, bottom] (pixels, or 0-1 fractions) around the object, and get the closest
    models with their tags."""
    from PIL import Image

    src = image
    if crop:
        with Image.open(image) as im:
            w, h = im.size
            box = [c * (w if i % 2 == 0 else h) for i, c in enumerate(crop)] if max(crop) <= 1 else crop
            THUMBS.mkdir(parents=True, exist_ok=True)
            src = str(THUMBS / "identify_crop.png")
            im.crop(tuple(int(b) for b in box)).save(src)
    q, backend, _ = _query_vec(image=src, backend=backend)
    results = _rank(q, _load_index(backend), limit)
    tags = {a["path"].lower(): a["tags"] for a in search_assets(limit=1000000)["assets"]}
    for r in results:
        r["tags"] = tags.get(r["asset"].lower(), [])
    return {"backend": backend, "results": results}


# ---------------------------------------------------------------- cataloguing

def _brands_in(text: str) -> list[str]:
    t = " " + re.sub(r"[_\-.]+", " ", text.lower()) + " "
    found = []
    for b in BRANDS:
        k = re.sub(r"[_\-.]+", " ", b.lower())
        if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", t) and b not in found:
            found.append(b)
    return found


def _asset_text(path: str) -> str:
    p = Path(path)
    s = Settings.load()
    try:
        parts = p.relative_to(s.models_dir).parts if s.models_dir else p.parts[-4:]
    except ValueError:
        parts = p.parts[-4:]
    return " ".join([*parts[:-1], p.stem])


@tool
def detect_brand(asset: str = "", text: str = "") -> dict:
    """Recognise furniture, lighting and bathroom brands (Minotti, Flos, Tom Dixon, Vitra, ...) in an
    asset's folder and file names, description and .max info, or in any text."""
    sources = [text] if text else []
    if asset:
        path = _get_asset(asset)["FileFullPath"] if GUID_RE.match(asset.strip()) else asset
        sources.append(_asset_text(path))
        try:
            info = client.request("GetAssetDescription", {"AssetFullPath": path})
            sources.append(info.get("Description") or "")
        except ConnecterError:
            pass
        if path.lower().endswith(".max") and Path(path).exists():
            m = maxfile.summary(Path(path))
            sources += [m.get("title", ""), m.get("comments", ""), m.get("keywords", "")]
    brands = _brands_in(" ".join(sources))
    tag_leaves = {t["TagPath"][-1].lower(): "/".join(t["TagPath"]) for t in _tags()}
    return {"brands": brands, "matching_tags": [tag_leaves[b.lower()] for b in brands if b.lower() in tag_leaves]}


def _materials_in(text: str) -> list[str]:
    t = text.lower()
    return [w for w in MATERIAL_WORDS if re.search(rf"(?<![a-z]){w}", t)]


@tool
def extract_metadata(asset: str) -> dict:
    """Everything that can be read about a model without opening it: 3ds Max version, renderer, polygon
    and object counts, materials, plug-ins, maps (and how many are missing), brand, likely materials,
    file size and dates, plus its size if measure_asset has run."""
    path = Path(_get_asset(asset)["FileFullPath"] if GUID_RE.match(asset.strip()) else asset)
    if not path.exists():
        raise ConnecterError(f"{path} does not exist")
    out = {"file": str(path), "size_mb": round(path.stat().st_size / 2**20, 1), "folder": str(path.parent)}
    text = _asset_text(str(path))
    if path.suffix.lower() == ".max":
        from .server import _texture_status

        info = maxfile.summary(path)
        maps = [_texture_status(t, path) for t in info.get("external_files", [])]
        out.update({"max_version": info.get("max_version"), "saved": info.get("saved"), "renderer": maxfile.renderer(info),
                    "faces": info.get("mesh_totals", {}).get("Faces"), "vertices": info.get("mesh_totals", {}).get("Vertices"),
                    "objects": info.get("scene_totals", {}).get("Objects"), "materials": info.get("materials", []),
                    "maps": len(maps), "missing_maps": sum(1 for m in maps if m["status"] == "missing")})
        text += " " + " ".join(info.get("materials", []) + [Path(m["path"]).stem for m in maps])
    out["brands"] = _brands_in(text)
    out["material_hints"] = _materials_in(text)
    if (m := measurements().get(str(path).lower())):
        out["dimensions_cm"] = {k: round(m[k], 1) for k in ("width_cm", "depth_cm", "height_cm")}
    return out


def _tag_candidates(path: str, tags: list[dict], s: Settings) -> list[dict]:
    """Tag suggestions from the folder (deepest matching tag) and from tag names found in the file name."""
    out: dict[str, dict] = {}
    by_norm = {tuple(_norm(p) for p in t["TagPath"]): "/".join(t["TagPath"]) for t in tags}
    p = Path(path)
    if s.models_dir:
        try:
            parts = [_norm(x) for x in p.parent.relative_to(s.models_dir).parts]
            for i in range(len(parts), 0, -1):
                if tuple(parts[:i]) in by_norm:
                    out[by_norm[tuple(parts[:i])]] = {"tag": by_norm[tuple(parts[:i])], "score": 0.9, "why": "library folder"}
                    break
        except ValueError:
            pass
    words = " " + re.sub(r"[^a-z0-9]+", " ", f"{p.parent.name} {p.stem}".lower()) + " "
    for t in tags:
        leaf = t["TagPath"][-1].lower()
        if len(leaf) < 4 or re.match(r"^\d", leaf):
            continue
        stem = re.sub(r"[^a-z0-9]+", " ", leaf).strip()
        singular = re.sub(r"(ies)$", "y", stem) if stem.endswith("ies") else re.sub(r"s$", "", stem)
        if f" {stem} " in words or f" {singular} " in words:
            key = "/".join(t["TagPath"])
            out.setdefault(key, {"tag": key, "score": 0.6, "why": f"'{leaf}' in the name"})
    return sorted(out.values(), key=lambda c: -c["score"])


def _clip_tag_scores(path: str, tags: list[dict]) -> list[dict]:
    idx = _load_index("clip")
    hit = next((v for k, v in idx["items"].items() if k.lower() == path.lower()), None)
    if not hit:
        return []
    leaves = [t for t in tags if len(t["TagPath"]) > 1 and not re.match(r"^\d", t["TagPath"][-1])]
    if not leaves:
        return []
    texts = [f"a 3d render of {t['TagPath'][-1].lower()}" for t in leaves]
    tv = [_norm_vec(v) for v in _clip(texts=texts)["texts"]]
    iv = _dec(hit["vec"])
    sims = [100 * sum(a * b for a, b in zip(iv, v)) for v in tv]
    mx = max(sims)
    exps = [math.exp(s - mx) for s in sims]
    total = sum(exps)
    ranked = sorted(zip(exps, leaves), key=lambda x: -x[0])[:3]
    return [{"tag": "/".join(t["TagPath"]), "score": round(e / total, 2), "why": "looks like it (CLIP)"} for e, t in ranked]


@tool
def auto_tag(assets: list[str] | None = None, folder: str = "", only_untagged: bool = True, use_visual: bool = True,
             min_score: float = 0.6, apply: bool = False, limit: int = 50) -> dict:
    """Suggest tags for models from their library folder, their names and (if the CLIP index is built)
    what they look like. apply=True tags each asset with its best suggestion at or above min_score
    (journaled, `undo` reverses it)."""
    s = Settings.load()
    tags = _tags()
    if assets:
        rows = [{"path": _get_asset(a)["FileFullPath"] if GUID_RE.match(a.strip()) else a, "tags": []} for a in assets]
    else:
        rows = [a for a in search_assets(folder=folder, untagged=only_untagged, extension=".max", limit=1000000)["assets"]][:limit]
    out, by_tag = [], {}
    for r in rows:
        cands = _tag_candidates(r["path"], tags, s)
        if use_visual:
            for c in _clip_tag_scores(r["path"], tags):
                prev = next((x for x in cands if x["tag"] == c["tag"]), None)
                if prev:
                    prev["score"] = round(min(0.99, prev["score"] + c["score"] * 0.3), 2)
                    prev["why"] += " + looks like it"
                else:
                    cands.append(c)
        cands.sort(key=lambda c: -c["score"])
        out.append({"asset": r["path"], "current_tags": r.get("tags", []), "suggestions": cands[:3]})
        if cands and cands[0]["score"] >= min_score and cands[0]["tag"] not in r.get("tags", []):
            by_tag.setdefault(cands[0]["tag"], []).append(r["path"])
    if not apply:
        return {"apply": False, "assets": out, "would_tag": {t: len(p) for t, p in by_tag.items()}}
    results = {t: tag_assets(p, t) for t, p in by_tag.items()}
    return {"tagged": {t: len(p) for t, p in by_tag.items()}, "journal_ids": [r.get("journal_id") for r in results.values()]}


def describe(path: str) -> str:
    p = Path(path)
    meta = extract_metadata(path) if p.exists() else {"brands": [], "material_hints": []}
    name = re.sub(r"[_\-]+", " ", p.stem).strip()
    name = re.sub(r"\s+", " ", re.sub(r"(?i)\b(corona|vray|v-ray|max\d{4}|20\d\d)\b", "", name)).strip() or p.stem
    bits = [name + (f" by {meta['brands'][0]}" if meta["brands"] and meta["brands"][0].lower() not in name.lower() else "")]
    try:
        cat = p.parent.relative_to(Settings.load().models_dir).parts[:2]
        bits.append(" / ".join(re.sub(r"^\d+_", "", c).title() for c in cat))
    except (ValueError, TypeError):
        pass
    if meta["material_hints"]:
        bits.append(", ".join(meta["material_hints"][:4]))
    if (d := meta.get("dimensions_cm")):
        bits.append(f"{d['width_cm']:.0f} x {d['depth_cm']:.0f} x {d['height_cm']:.0f} cm")
    if meta.get("renderer"):
        bits.append(meta["renderer"].replace("vray", "V-Ray").title() if meta["renderer"] != "vray" else "V-Ray")
    return ". ".join(bits)


@tool
def auto_describe(assets: list[str] | None = None, folder: str = "", only_empty: bool = True, apply: bool = False,
                  limit: int = 50) -> dict:
    """Write searchable descriptions for models from their name, brand, category, materials, size and
    renderer, e.g. 'Lazy chair by Living Divani. Furnitures / Chairs. fabric, oak. 75 x 80 x 70 cm. Corona'.
    apply=True saves them (journaled); by default only assets without a description are changed."""
    rows = ([{"path": _get_asset(a)["FileFullPath"] if GUID_RE.match(a.strip()) else a, "description": None} for a in assets]
            if assets else search_assets(folder=folder, extension=".max", limit=1000000)["assets"])
    rows = [r for r in rows if not (only_empty and r.get("description"))][:limit]
    plan = [{"asset": r["path"], "description": describe(r["path"])} for r in rows]
    if not apply:
        return {"apply": False, "descriptions": plan}
    from .server import set_description

    done = [set_description(p["asset"], p["description"])["journal_id"] for p in plan]
    return {"described": len(done), "journal_ids": done}


@tool
def batch_catalogue(folder: str, tag: str = "", describe_assets: bool = True, dry_run: bool = True, limit: int = 200) -> dict:
    """Catalogue every model file in a folder that Connecter doesn't know yet, in one go: tag (the given
    tag, else the best auto_tag suggestion), previews (images in the model's folder, else the thumbnail
    inside the .max) and a generated description. dry_run=True (default) returns the plan."""
    root = Path(folder)
    with _db() as db:
        known = {a.path.lower() for a in db.assets()}
    files = [p for p in root.rglob("*") if p.suffix.lower() in MODEL_EXTS and str(p).lower() not in known][:limit]
    max_files = [p for p in files if p.suffix.lower() == ".max"]
    files = max_files or files
    s, tags = Settings.load(), _tags()
    plan = []
    for f in files:
        bundle = len(list(f.parent.glob("*.max"))) > 2
        previews = [] if bundle else [i["path"] for i in classify_images(f.parent)["previews"][:3]]
        if not previews and f.suffix.lower() == ".max":
            thumb = THUMBS / (hashlib.md5(str(f).lower().encode()).hexdigest() + ".png")
            if thumb.exists() or maxfile.thumbnail(f, thumb):
                keep = f.with_name(f.stem + "_thumb.png")
                previews = [str(keep)]
                if not dry_run and not keep.exists():
                    shutil.copy2(thumb, keep)
        cands = _tag_candidates(str(f), tags, s)
        plan.append({"asset": str(f), "tag": tag or (cands[0]["tag"] if cands else None), "previews": previews,
                     "description": describe(str(f)) if describe_assets else None})
    if dry_run:
        return {"dry_run": True, "to_catalogue": len(plan), "plan": plan}
    done = []
    for p in plan:
        r = catalogue_asset(p["asset"], tags=[p["tag"]] if p["tag"] else [], previews=p["previews"], description=p["description"])
        done.append(r["journal_id"])
    journal.record("batch_catalogue", {"folder": folder, "assets": len(done)})
    return {"catalogued": len(done), "journal_ids": done}
