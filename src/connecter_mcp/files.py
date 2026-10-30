"""File-system helpers: model and image detection, texture paths inside .max files, file index."""

from __future__ import annotations

import difflib
import os
import re
import struct
import time
from pathlib import Path

import olefile

MODEL_EXTS = {".max", ".fbx", ".obj", ".skp", ".blend", ".c4d", ".3ds", ".gltf", ".glb", ".usd", ".usdz", ".rfa"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".tga", ".bmp", ".exr", ".hdr", ".webp", ".psd"}
TEXTURE_WORDS = re.compile(
    r"(?:^|[\s_.\-])(diff(?:use)?|albedo|basecolou?r|col(?:ou?r)?|norm(?:al)?(?:map)?|nrm|bump|rough(?:ness)?|gloss(?:iness)?"
    r"|refl(?:ect(?:ion)?)?|spec(?:ular)?|metal(?:lic|ness)?|disp(?:lacement)?|height|ao|ambient|occlusion|opacity|alpha"
    r"|mask|cavity|emiss(?:ive)?|ior|translucen\w*|sss|tex(?:ture)?)(?:$|[\s_.\-\d])", re.I)


def image_size(path: Path) -> tuple[int, int] | None:
    """Width and height for JPEG/PNG without third-party libraries."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head.startswith(b"\x89PNG"):
                return struct.unpack(">II", head[16:24])
            if head[:2] != b"\xff\xd8":
                return None
            f.seek(2)
            while True:
                marker = f.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                if marker[1] in (0xC0, 0xC1, 0xC2):
                    f.read(3)
                    h, w = struct.unpack(">HH", f.read(4))
                    return w, h
                f.seek(struct.unpack(">H", f.read(2))[0] - 2, 1)
    except OSError:
        return None


def max_texture_paths(max_file: Path) -> list[str]:
    """Bitmap paths stored in a .max file's asset metadata (no 3ds Max needed)."""
    try:
        ole = olefile.OleFileIO(str(max_file))
    except (OSError, ValueError):
        return []
    try:
        stream = next((s for s in ("FileAssetMetaData3", "FileAssetMetaData2") if ole.exists(s)), None)
        if not stream:
            return []
        text = ole.openstream(stream).read().decode("utf-16-le", "ignore")
    finally:
        ole.close()
    seen: dict[str, None] = {}
    for part in text.split("\x00"):
        if Path(part).suffix.lower() in IMAGE_EXTS | {".ies", ".vrmesh", ".cgeo", ".vdb"} and part.isprintable():
            seen.setdefault(part, None)
    return list(seen)


def classify_images(folder: Path) -> dict:
    """Split a model folder's images into previews and texture maps.

    An image is a texture if a .max in the folder uses it, if its name has a map keyword
    (diffuse, normal, roughness...), or if it is a large power-of-two square.
    """
    used = set()
    for m in folder.rglob("*.max"):
        used |= {Path(p).name.lower() for p in max_texture_paths(m)}
    previews, textures = [], []
    for img in sorted(p for p in folder.rglob("*") if p.suffix.lower() in IMAGE_EXTS):
        size = image_size(img)
        reasons = []
        if img.name.lower() in used:
            reasons.append("used by a .max file")
        if TEXTURE_WORDS.search(img.stem):
            reasons.append("texture keyword in name")
        if size and size[0] == size[1] and size[0] >= 2048 and size[0] & (size[0] - 1) == 0:
            reasons.append(f"{size[0]}px power-of-two square")
        if any(part.lower() in {"map", "maps", "textures", "tex", "texture", "materials", "mat"} for part in img.relative_to(folder).parts[:-1]):
            reasons.append("in a texture folder")
        entry = {"path": str(img), "size": list(size) if size else None, "reasons": reasons}
        (textures if reasons else previews).append(entry)
    return {"previews": previews, "textures": textures}


class FileIndex:
    """Cached name index of files under some roots, for finding moved files."""

    def __init__(self, ttl: float = 600):
        self.ttl = ttl
        self._built: dict[tuple, tuple[float, dict[str, list[str]]]] = {}

    def get(self, roots: list[Path]) -> dict[str, list[str]]:
        key = tuple(sorted(str(r).lower() for r in roots))
        hit = self._built.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        index: dict[str, list[str]] = {}
        for root in roots:
            for dirpath, _dirs, names in os.walk(root):
                for n in names:
                    index.setdefault(n.lower(), []).append(os.path.join(dirpath, n))
        self._built[key] = (time.monotonic(), index)
        return index

    def find(self, roots: list[Path], filename: str, fuzzy: bool = True, limit: int = 8) -> list[dict]:
        index = self.get(roots)
        name = filename.lower()
        results = [{"path": p, "match": "exact name", "confidence": 0.95} for p in index.get(name, [])]
        stem, ext = os.path.splitext(name)
        for other_ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".tga", ".exr"):
            if other_ext != ext:
                results += [{"path": p, "match": f"same name, {other_ext}", "confidence": 0.75}
                            for p in index.get(stem + other_ext, [])]
        if fuzzy and len(results) < limit:
            for cand in difflib.get_close_matches(name, [k for k in index if k.endswith(ext)], n=limit, cutoff=0.75):
                if cand != name:
                    score = difflib.SequenceMatcher(None, name, cand).ratio()
                    results += [{"path": p, "match": "similar name", "confidence": round(score * 0.8, 2)} for p in index[cand]]
        # The index is cached, so drop files that have been moved or deleted since it was built.
        results = [r for r in results if os.path.exists(r["path"])]
        results.sort(key=lambda r: -r["confidence"])
        return results[:limit]
