"""Real 3ds Max test: runs the 3ds Max tools in headless 3dsmaxbatch on a copy of one model in a temp folder.

Never touches an open 3ds Max: scene tools, which normally run in the open scene, run here in a fresh
headless scene. Every job goes through the busy check, so nothing starts while 3ds Max is rendering.
Run:  uv run python tests/max_test.py "<path to a .max>" [check name ...]
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

from connecter_mcp import server  # noqa: F401  must load first, it registers the tool modules
from connecter_mcp import library, maxbridge, maxfile, maxtools as m
from connecter_mcp.config import Settings
from PIL import Image

RESULTS: list[tuple[str, bool, str]] = []


class Busy(Exception):
    """3ds Max got busy: stop the whole run rather than start more jobs around the user's work."""

ONLY = set(sys.argv[2:])


def check(label: str, fn) -> None:
    if ONLY and label not in ONLY:
        return
    start = time.monotonic()
    try:
        detail = fn()
        RESULTS.append((label, True, str(detail)[:300]))
    except Exception as e:  # noqa: BLE001 - every failure goes in the report
        RESULTS.append((label, False, f"{type(e).__name__}: {e}"[:1500]))
        traceback.print_exc()
    r = RESULTS[-1]
    print(f"{'PASS' if r[1] else 'FAIL'}  {label} ({time.monotonic() - start:.0f}s)  {r[2]}", flush=True)
    if "3ds Max is busy" in r[2]:
        raise Busy(r[2])


def expect(cond, msg="unexpected result"):
    if not cond:
        raise AssertionError(msg)


def in_scene(capture_tool, setup: str = "", verify: str = "") -> dict:
    """Run a scene tool's script in a fresh headless scene instead of the open 3ds Max."""
    captured = {}
    real = m.execute
    m.execute = lambda body, mode="script", *a, **k: captured.setdefault("body", body) and {}
    try:
        capture_tool()
    finally:
        m.execute = real
    body = setup + "\n" + captured["body"] + "\n" + verify
    return maxbridge.run_batch(body, timeout=600)["result"]


def main(source: Path) -> None:
    work = Path(tempfile.mkdtemp(prefix="connecter-mcp-maxtest-"))
    A = work / "model" / source.name
    A.parent.mkdir()
    shutil.copy2(source, A)
    for p in source.parent.glob("*.jp*g"):
        shutil.copy2(p, A.parent)
    a = str(A)
    try:
        def measure():
            r = m.measure_asset(a, mode="batch")["result"]
            expect(r["width_cm"] > 0 and r["faces"] > 0, str(r))
            expect(str(A).lower() in m.measurements(), "not cached")
            return r
        check("measure_asset", measure)

        def materials():
            r = m.list_materials(a, mode="batch")["result"]
            expect(r["materials"], "no materials")
            return f"{len(r['materials'])} materials, {r['renderer']}"
        check("list_materials", materials)

        def consistency():
            r = m.consistency_check(a, mode="batch")["result"]
            names = [Path(f).name.lower() for f in r["missing_files"]]
            expect(not any(n in {"p1.jpg", "p2.jpg", "p4.jpg", "p5.jpg"} for n in names), f"maps next to the scene counted as missing: {names}")
            return r["issues"], r["missing_files"]
        check("consistency_check", consistency)

        def fix():
            r = m.fix_model(a, mode="batch")["result"]
            out = Path(r["saved_as"])
            expect(out.exists() and out != A, str(r))
            before, after = maxfile.summary(A), maxfile.summary(out)
            expect(after["mesh_totals"]["Faces"] == before["mesh_totals"]["Faces"], "face count changed")
            return r
        check("fix_model", fix)

        def fix_weld():
            r = m.fix_model(a, weld_threshold_cm=0.001, delete_isolated_vertices=True, pivot_to_base=True, mode="batch")["result"]
            expect(Path(r["saved_as"]).exists(), str(r))
            return r["fixed"]
        check("fix_model (weld, pivot)", fix_weld)

        def version():
            r = m.convert_version(a, 2021, mode="batch")["result"]
            out = Path(r["file"])
            expect(out.exists(), str(r))
            info = maxfile.summary(out)
            return info["saved_as_version"]
        check("convert_version", version)

        def renderer(source, target, mat_class):
            def go():
                r = m.convert_renderer(str(source), target=target, mode="batch")["result"]
                out = Path(r["saved_as"])
                expect(out.exists(), str(r))
                expect(target in r["renderer"].lower().replace("_", "").replace("-", ""), str(r))
                mats = m.list_materials(str(out), mode="batch")["result"]["materials"]
                classes = {x["class"] for x in mats}
                expect(mats and all(mat_class in c.lower() for c in classes), f"material classes: {classes}")
                return f"{r['renderer']}: {classes}"
            return go
        check("convert_renderer corona", renderer(A, "corona", "corona"))
        # Back the other way from the Corona file, so the V-Ray converter has real work to do.
        check("convert_renderer vray", renderer(m._sibling(A, "corona"), "vray", "vray"))

        def proxy(renderer_name, ext):
            def go():
                r = m.make_proxy(a, renderer=renderer_name, mode="batch")["result"]
                expect(Path(r["proxy_file"]).suffix == ext and Path(r["proxy_file"]).exists(), str(r))
                expect(Path(r["max_file"]).exists(), str(r))
                return f"{Path(r['proxy_file']).stat().st_size // 1024} KB"
            return go
        check("make_proxy vray", proxy("vray", ".vrmesh"))
        check("make_proxy corona", proxy("corona", ".cgeo"))

        def relink():
            before = maxfile.max_texture_paths(A)
            missing = [p for p in before if m._texture_status(p, A)["status"] == "missing"]
            expect(missing, "model has no missing maps to relink")
            target = next(A.parent.glob("*.jp*g"))
            original = A.read_bytes()
            r = m.relink_max_textures(a, choices={missing[0]: str(target)}, mode="batch", dry_run=False, method="scene")
            expect(r["result"]["relinked"] >= 1, str(r["result"]))
            after = maxfile.max_texture_paths(A)
            expect(str(target).lower() in [p.lower() for p in after], f"new path not stored: {after}")
            expect(missing[0] not in after, "old path still stored")
            server.undo(r["journal_id"])
            expect(A.read_bytes() == original, "undo did not restore the file")
            return f"{r['result']['relinked']} relinked, undo restored"
        check("relink_max_textures (+undo)", relink)

        def collect():
            dest = work / "collected"
            r = m.collect_for_project(a, str(dest), mode="batch", dry_run=False)
            expect(r["result"]["relinked"] >= 1, str(r))
            stored = maxfile.max_texture_paths(dest / A.name)
            inside = [p for p in stored if str(dest).lower() in p.lower()]
            expect(inside, f"nothing points at the copies: {stored}")
            return f"{len(inside)} of {len(stored)} paths point at the copies"
        check("collect_for_project", collect)

        tex = work / "Oak Floor"
        tex.mkdir()
        for n in ("Oak_Floor_diffuse.jpg", "Oak_Floor_roughness.jpg", "Oak_Floor_normal.png", "Oak_Floor_metalness.jpg"):
            Image.new("RGB", (16, 16), (120, 90, 60)).save(tex / n)
        lib = work / "test.mat"

        def material(renderer_name, cls):
            def go():
                r = m.create_material(str(tex), name=f"Oak_{renderer_name}", renderer=renderer_name, save_to_library=str(lib), mode="batch", force=False)["result"]
                expect(cls.lower() in r["class"].lower(), str(r))
                expect(not r["unset_properties"], f"properties that don't exist: {r['unset_properties']}")
                expect(lib.exists(), "library not saved")
                return r["class"]
            return go
        check("create_material corona", material("corona", "CoronaPhysicalMtl"))
        check("create_material vray", material("vray", "VRayMtl"))
        check("create_material physical", material("physical", "Physical"))

        def libs():
            names = [x["name"] for x in m.manage_materials(str(lib), mode="batch")["result"]["materials"]]
            expect("Oak_vray" in names, str(names))
            r = m.manage_materials(str(lib), action="remove", names=["Oak_vray"], mode="batch")["result"]
            expect(r["removed"] == ["Oak_vray"], str(r))
            r = m.manage_materials(str(lib), action="add", from_asset=a, mode="batch")["result"]
            expect(r["added"], str(r))
            return r["library_count"]
        check("manage_materials", libs)

        def render_preview():
            out = library._render_preview(A, work / "clay.jpg", False)
            with Image.open(out) as im:
                expect(im.size == (800, 600), str(im.size))
                lo, hi = im.convert("L").getextrema()
            expect(hi - lo > 20, f"blank render: {lo}-{hi}")
            return f"contrast {lo}-{hi}"
        check("generate_preview (clay render)", render_preview)

        # ---- scene tools, in a fresh headless scene
        def merge():
            r = in_scene(lambda: m.merge_into_max(a, position_cm=[100, 50, 10], rotation_z=90, layer="MCP"), verify="""
            head = getNodeByName (mcpMergedName = (for o in objects where isGroupHead o collect o.name)[1])
            bb = nodeGetBoundingBox head (matrix3 1)
            mcpResult = mcpO #(#("cx", mcpToCm ((bb[1].x + bb[2].x) / 2)), #("cy", mcpToCm ((bb[1].y + bb[2].y) / 2)), #("bz", mcpToCm bb[1].z),
                #("layer", head.layer.name))""")
            expect(abs(r["cx"] - 100) < 1 and abs(r["cy"] - 50) < 1 and abs(r["bz"] - 10) < 1, str(r))
            expect(r["layer"] == "MCP", str(r))
            return r
        check("merge_into_max", merge)

        boxes = """
        k = 1.0 / (mcpToCm 1.0)
        b1 = Box name:"PH_1" length:(200 * k) width:(200 * k) height:(100 * k) pos:([500, 0, 0] * k)
        b2 = Box name:"PH_2" length:(200 * k) width:(200 * k) height:(100 * k) pos:([-500, 0, 0] * k)
        rotate b2 (eulerAngles 0 0 45)
        """

        def swap():
            r = in_scene(lambda: m.swap_placeholder(a, "PH_*", fit="footprint", delete_placeholders=False, layer="SWAP"), setup=boxes, verify="""
            heads = for o in objects where isGroupHead o collect o
            out = for h in heads collect (bb = nodeGetBoundingBox h (matrix3 1); mcpO #(#("cx", mcpToCm ((bb[1].x + bb[2].x) / 2)), #("bz", mcpToCm bb[1].z)))
            mcpResult = mcpO #(#("groups", heads.count), #("positions", out), #("hidden", (getNodeByName "PH_1").isHidden))""")
            expect(r["groups"] == 2 and r["hidden"], str(r))
            xs = sorted(p["cx"] for p in r["positions"])
            expect(abs(xs[0] + 500) < 1 and abs(xs[1] - 500) < 1, str(r))
            expect(all(abs(p["bz"]) < 1 for p in r["positions"]), str(r))
            return r
        check("swap_placeholder", swap)

        def variant():
            r = in_scene(lambda: m.variant_swap("PH_1", a), setup=boxes, verify="""
            heads = for o in objects where isGroupHead o collect o
            bb = nodeGetBoundingBox heads[1] (matrix3 1)
            mcpResult = mcpO #(#("gone", getNodeByName "PH_1" == undefined), #("cx", mcpToCm ((bb[1].x + bb[2].x) / 2)))""")
            expect(r["gone"] and abs(r["cx"] - 500) < 1, str(r))
            return r
        check("variant_swap", variant)

        def furnish():
            floor = 'k = 1.0 / (mcpToCm 1.0)\nf = Box name:"Floor" length:(600 * k) width:(600 * k) height:(10 * k) pos:([300, 300, -10] * k)\n'
            r = in_scene(lambda: m.furnish_room([{"asset": a, "position_cm": [100, 100, 0]}, {"asset": a, "position_cm": [400, 100, 0], "rotation_z": 180}],
                                                room_object="Floor", layer="FURN"), setup=floor, verify="""
            heads = for o in objects where isGroupHead o collect o
            out = for h in heads collect (bb = nodeGetBoundingBox h (matrix3 1); mcpO #(#("cx", mcpToCm ((bb[1].x + bb[2].x) / 2)), #("cy", mcpToCm ((bb[1].y + bb[2].y) / 2)), #("bz", mcpToCm bb[1].z)))
            mcpResult = mcpO #(#("count", heads.count), #("positions", out))""")
            expect(r["count"] == 2, str(r))
            xs = sorted(p["cx"] for p in r["positions"])
            expect(abs(xs[0] - 100) < 1 and abs(xs[1] - 400) < 1, str(r))
            expect(all(abs(p["bz"]) < 1 for p in r["positions"]), str(r))
            return r
        check("furnish_room", furnish)

        # ---- tools that also need the library and Connecter (throwaway folder, removed afterwards)
        models_dir = Settings.load().models_dir
        box = models_dir / "09_MISC" / "_MCP_TEST"

        def batch_relink():
            model = box / "model"
            elsewhere = box / "elsewhere"
            model.mkdir(parents=True)
            elsewhere.mkdir()
            target = model / A.name
            shutil.copy2(A, target)
            missing = [p for p in maxfile.max_texture_paths(target) if m._texture_status(p, target)["status"] == "missing" and "F:" in p]
            expect(missing, "no missing map to relink")
            stand_in = elsewhere / Path(missing[0].replace("\\", "/")).name
            shutil.copy2(next(A.parent.glob("*.jp*g")), stand_in)
            time.sleep(2)
            # Deliberately left without tags or previews: Connecter's database doesn't hold such models.
            server.index._built.clear()
            plan = m.batch_relink(folder=str(model), other_folders=True)
            expect(str(target) in plan["files_to_relink"], str(plan)[:500])
            original = target.read_bytes()
            r = m.batch_relink(folder=str(model), dry_run=False, method="scene", other_folders=True)
            expect(r["result"]["files"][0]["relinked"] >= 1, str(r["result"]))
            # This model's F:\\ map is a leftover in its metadata: no map uses it, so it must be reported, not relinked.
            expect(missing[0] in r["not_in_scene"][str(target)], str(r["not_in_scene"]))
            stored = [p.lower() for p in maxfile.max_texture_paths(target)]
            expect(any(p.endswith("p1.jpg") and "\\" in p for p in stored), f"live maps not relinked: {stored}")
            server.undo(r["journal_id"])
            expect(target.read_bytes() == original, "undo did not restore the file")
            return r["result"]["files"]
        check("batch_relink (+undo)", batch_relink)

        def relink_metadata():
            model = work / "metadata"
            shutil.copytree(A.parent, model, ignore=shutil.ignore_patterns("*.zip", "*.rar"))
            target = model / A.name
            old = next(p for p in maxfile.max_texture_paths(target) if m._texture_status(p, target)["status"] == "ok")
            moved = model / f"relinked_{Path(old).name}"
            shutil.copy2(m._texture_status(old, target)["resolved"], moved)
            original = target.read_bytes()
            versions = {k: maxfile.summary(target).get(k) for k in ("max_version", "saved_as_version", "build")}
            r = m.relink_max_textures(str(target), choices={old: str(moved)}, dry_run=False)
            expect(r["result"]["relinked"] >= 1, str(r["result"]))
            stored = maxfile.max_texture_paths(target)
            expect(str(moved) in stored and old not in stored, f"reader disagrees: {stored}")
            expect({k: maxfile.summary(target).get(k) for k in versions} == versions, "the file's version changed")
            # The scene itself was not re-saved, so loading it is what proves 3ds Max follows the new path.
            loaded = maxbridge.run_batch(f'''
fn hits = (for b in getClassInstances BitmapTexture where b.fileName != undefined and (toLower b.fileName) == (toLower {maxbridge.ms(moved)}) collect b)
mcpResult = mcpO #(#("hits", (hits()).count), #("all", for b in getClassInstances BitmapTexture collect b.fileName))
''', scene=target, timeout=300)["result"]
            expect(loaded["hits"] >= 1, str(loaded))
            server.undo(r["journal_id"])
            expect(target.read_bytes() == original, "undo did not restore the file")
            return f"{r['result']['relinked']} rewritten without a re-save, {loaded['hits']} maps follow it, undo restored"
        check("relink metadata (+undo)", relink_metadata)

        def to_library():
            name = "Scene Export"
            category = "09_MISC/_MCP_TEST"
            out = box / name / f"{name}.max"
            setup = f'''mergeMaxFile {maxbridge.ms(a)} #select #mergeDups quiet:true'''
            r = in_scene(lambda: m.scene_to_library(name, category), setup=setup)
            expect(out.exists(), str(r))
            expect(maxfile.summary(out)["mesh_totals"]["Faces"] == maxfile.summary(A)["mesh_totals"]["Faces"], "geometry differs")
            preview = box / name / f"{name}_preview.jpg"
            server.create_tag(category)
            time.sleep(2)
            c = m.scene_to_library(name, category, description="MCP test", stage="catalogue")
            expect(c["asset"]["CustomPreviews"], "no preview attached")
            return f"saved, preview grabbed: {preview.exists()}"
        check("scene_to_library (export + catalogue)", to_library)

        # ---- the same script, in the form that runs in an open 3ds Max
        def script_form():
            wrapped = m.measure_asset(a, mode="script")["maxscript"]
            expect("getSaveRequired" in wrapped and "loadMaxFile" in wrapped)
            r = maxbridge.run_batch(wrapped + "\nmcpResult = mcpO #(#(\"ran\", true), #(\"file\", maxFileName))", timeout=300)["result"]
            expect(r["file"] == A.name, str(r))
            return r
        check("script mode opens the file", script_form)
    except Busy:
        print("Stopped: 3ds Max is busy. Run this again when it is idle.")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        box = Settings.load().models_dir / "09_MISC" / "_MCP_TEST"
        try:
            for t in sorted((t["tag"] for t in server.list_tags("_MCP_TEST")["tags"]), key=len, reverse=True):
                server.delete_tag(t, confirm=True)
            if box.exists():
                server.client.request("DeleteFolderFromFileSystem", {"FolderFullPath": str(box), "MoveToRecycleBinIfPossible": True})
                time.sleep(2)
        except Exception as e:  # noqa: BLE001 - Connecter may be closed; the folder is removed below anyway
            print("cleanup:", e)
        shutil.rmtree(box, ignore_errors=True)
    failed = sum(1 for r in RESULTS if not r[1])
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
