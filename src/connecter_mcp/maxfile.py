"""Read a .max file's own metadata without 3ds Max: scene summary, external files and thumbnail."""

from __future__ import annotations

import io
import struct
from pathlib import Path

import olefile

from .files import max_texture_paths

DOC_SUMMARY = "\x05DocumentSummaryInformation"
SUMMARY = "\x05SummaryInformation"


def _text(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)


def summary(max_file: Path) -> dict:
    """The scene info 3ds Max writes into every .max: version, mesh and scene totals, objects,
    materials, plug-ins, render settings and external dependencies."""
    try:
        ole = olefile.OleFileIO(str(max_file))
    except (OSError, ValueError) as e:
        return {"error": f"Not a readable .max file: {e}"}
    try:
        sections: dict[str, list[str]] = {}
        if ole.exists(DOC_SUMMARY):
            props = ole.getproperties(DOC_SUMMARY)
            heads, values = props.get(12) or [], [_text(v) for v in props.get(13) or []]
            # Property 12 pairs each section name with its number of lines in property 13.
            pos = 0
            for name, count in zip(heads[::2], heads[1::2]):
                sections[_text(name)] = values[pos:pos + count]
                pos += count
        info = {}
        if ole.exists(SUMMARY):
            p = ole.getproperties(SUMMARY, convert_time=True)
            info = {"title": _text(p.get(2, "")), "author": _text(p.get(4, "")), "keywords": _text(p.get(5, "")),
                    "comments": _text(p.get(6, "")), "last_saved_by": _text(p.get(8, "")),
                    "saved": str(p.get(13, "")), "created": str(p.get(12, ""))}
    finally:
        ole.close()

    def kv(lines: list[str]) -> dict:
        out = {}
        for line in lines:
            k, sep, v = line.partition(":" if ":" in line else "=")
            out[k.strip()] = v.strip() if sep else True
        return out

    general = kv(sections.get("General", []))
    return {
        **{k: v for k, v in info.items() if v},
        "max_version": general.get("3ds Max Version"),
        "saved_as_version": general.get("Saved As Version"),
        "build": general.get("Build"),
        "mesh_totals": kv(sections.get("Mesh Totals", [])),
        "scene_totals": kv(sections.get("Scene Totals", [])),
        "render": kv(sections.get("Render Data", [])),
        "objects": sections.get("Objects", []),
        "materials": sections.get("Materials", []),
        "plugins": sections.get("Used Plug-Ins", []),
        "external_files": max_texture_paths(max_file) or sections.get("External Dependencies", []),
    }


def renderer(info: dict) -> str | None:
    """Best guess of the scene's renderer from its render data and plug-ins."""
    text = " ".join([*map(str, info.get("render", {}).values()), *info.get("plugins", [])]).lower()
    for name in ("corona", "v-ray", "vray", "arnold", "redshift", "octane", "fstorm", "scanline", "art"):
        if name in text:
            return {"v-ray": "vray"}.get(name, name)
    return None


def thumbnail(max_file: Path, out_png: Path) -> Path | None:
    """Save the thumbnail 3ds Max embeds in the file (a DIB inside a metafile) as a PNG."""
    from PIL import Image

    try:
        ole = olefile.OleFileIO(str(max_file))
        try:
            data = ole.getproperties(SUMMARY).get(17) if ole.exists(SUMMARY) else None
        finally:
            ole.close()
    except (OSError, ValueError):
        return None
    if not data:
        return None
    # Find the BITMAPINFOHEADER (size 40) with plausible dimensions and rebuild a .bmp around it.
    start = 0
    while (i := data.find(b"\x28\x00\x00\x00", start)) != -1:
        start = i + 1
        if len(data) < i + 40:
            break
        w, h, planes, bpp = struct.unpack("<iiHH", data[i + 4:i + 16])
        if not (0 < w <= 4096 and 0 < abs(h) <= 4096 and planes == 1 and bpp in (8, 16, 24, 32)):
            continue
        colors = struct.unpack("<I", data[i + 32:i + 36])[0] or (1 << bpp if bpp <= 8 else 0)
        offset = 14 + 40 + colors * 4
        dib = data[i:]
        bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset) + dib
        try:
            img = Image.open(io.BytesIO(bmp))
            img.load()
        except Exception:
            continue
        out_png.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(out_png)
        return out_png
    return None
