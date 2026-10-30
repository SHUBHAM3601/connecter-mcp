"""Test of the AI, 3ds Max, project-output, library and automation tools against a running Connecter.

Never starts or touches 3ds Max: the 3ds Max tools are checked in mode='script' (the generated MAXScript
is checked for balanced brackets), and mode='batch' only to confirm it refuses while 3ds Max is busy.
Works on a throwaway copy of one model in <models_dir>/<category>/_MCP_TEST and cleans up afterwards.
Run:  uv run python tests/extended_test.py "<path to a model folder>" [category] [--clip]
--clip also tests CLIP search (downloads the model on first use and runs it at idle priority).
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from live_test import RESULTS, call, check, cleanup, expect, report

from connecter_mcp.config import Settings
from connecter_mcp.files import classify_images
from connecter_mcp.maxbridge import max_activity


def balanced(script: str) -> bool:
    """Brackets balance once strings and comments are removed (catches most MAXScript typos)."""
    s = re.sub(r'@"[^"]*"', '""', script)
    s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
    s = re.sub(r"--[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    stack, pairs = [], {")": "(", "]": "["}
    for ch in s:
        if ch in "([":
            stack.append(ch)
        elif ch in ")]":
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack


def script_ok(result: dict, *must: str) -> str:
    expect(result["mode"] == "script", "not a script result")
    ms = result["maxscript"]
    expect(balanced(ms), "unbalanced brackets in MAXScript")
    for m in must:
        expect(m in ms, f"script lacks {m!r}")
    return f"{len(ms)} chars"


def main(source: Path, category: str, clip: bool) -> None:
    models_dir = Settings.load().models_dir
    expect(models_dir, "models_dir must be set in the config")
    test_tag = f"{category}/_MCP_TEST"
    test_dir = models_dir / category / "_MCP_TEST"
    model_dir = test_dir / "MCP Test Model"
    tmp = Path(tempfile.mkdtemp(prefix="connecter-mcp-xtest-"))
    try:
        shutil.copytree(source, model_dir, ignore=shutil.ignore_patterns("*.obj", "*.fbx", "*.mtl"))
        A = str(next(model_dir.glob("*.max")))
        imgs = classify_images(model_dir)
        preview = imgs["previews"][0]["path"] if imgs["previews"] else None
        time.sleep(2)
        call("create_tag", tag=test_tag)
        call("catalogue_asset", asset_path=A, tags=[test_tag], previews=[preview] if preview else [])

        # ---- reading .max files (no 3ds Max)
        check("verify_textures", lambda: expect("totals" in call("verify_textures", asset=A)))
        check("scene_missing_assets", lambda: call("scene_missing_assets", scene=A)["by_type"])
        check("scene_asset_report", lambda: expect(any(m["asset"].lower() == A.lower() for m in call("scene_asset_report", scene=A)["library_models_used"])))
        check("list_materials (file)", lambda: expect(call("list_materials", asset=A)["materials"]))
        check("consistency_check (file)", lambda: call("consistency_check", scene=A)["issues"])
        check("extract_metadata", lambda: expect(call("extract_metadata", asset=A)["faces"]))

        # ---- cataloguing
        check("detect_brand", lambda: expect("Minotti" in call("detect_brand", text="Minotti Lawson sofa")["brands"]))
        check("auto_tag", lambda: expect(call("auto_tag", assets=[A], use_visual=False)["assets"][0]["suggestions"]))

        def describe_and_undo():
            d = call("auto_describe", assets=[A], only_empty=False)["descriptions"][0]["description"]
            r = call("auto_describe", assets=[A], only_empty=False, apply=True)
            call("undo", journal_id=r["journal_ids"][0])
            return d
        check("auto_describe (+apply, undo)", describe_and_undo)

        extra = model_dir / "variant" / "variant.max"

        def batch_cat():
            extra.parent.mkdir()
            shutil.copy2(A, extra)
            time.sleep(2)
            plan = call("batch_catalogue", folder=str(test_dir), tag=test_tag)
            expect(plan["to_catalogue"] == 1, f"plan: {plan['to_catalogue']}")
            done = call("batch_catalogue", folder=str(test_dir), tag=test_tag, dry_run=False)
            expect(test_tag in {"/".join(t["TagPath"]) for t in call("get_asset", asset=str(extra))["Tags"]})
            return done
        check("batch_catalogue (dry + real)", batch_cat)

        # ---- library upkeep
        def optimize():
            plan = call("texture_optimizer", folder=str(model_dir), max_size=256)
            expect(plan["count"] > 0, "nothing to resize")
            target = Path(plan["resize"][0]["file"])
            size = target.stat().st_size
            r = call("texture_optimizer", folder=str(model_dir), max_size=256, dry_run=False)
            expect(target.stat().st_size < size, "not smaller")
            call("undo", journal_id=r["journal_id"])
            expect(target.stat().st_size == size, "undo did not restore")
            shutil.rmtree(target.parent / "_original_textures", ignore_errors=True)
            return f"{r['resized']} resized and restored"
        check("texture_optimizer (dry, real, undo)", optimize)

        def collect():
            dest = tmp / "collect"
            plan = call("collect_for_project", scene=A, destination=str(dest))
            expect(plan["dry_run"] and plan["files"] > 0)
            call("collect_for_project", scene=A, destination=str(dest), relink=False, dry_run=False)
            expect((dest / Path(A).name).exists() and any((dest / "maps").iterdir()))
            return f"{plan['files']} files"
        check("collect_for_project (dry + real)", collect)

        check("generate_preview (dry)", lambda: expect(call("generate_preview", folder=str(test_dir), only_missing=False)["models"] >= 1))
        check("generate_preview (thumbnail)", lambda: expect(call("generate_preview", asset=A, only_missing=False, dry_run=False)["generated"] == 1))

        def hide():
            tex = [i["path"] for i in classify_images(model_dir)["textures"]]
            expect(tex, "no texture maps to hide")
            expect(call("hide_files", files=tex[:1])["dry_run"])
            r = call("hide_files", files=tex[:1], dry_run=False)
            expect(r["hidden"] == 1, str(r))
            before = {p["FileFullPath"].lower() for p in call("get_asset", asset=A)["CustomPreviews"]}
            expect(tex[0].lower() not in before, "texture left attached as a preview")
            return r
        check("hide_files", hide)

        def sandboxed():
            call("sandbox", action="start", label="test")
            call("set_description", asset=A, description="sandbox edit")
            st = call("sandbox", action="status")
            expect(len(st["changes"]) == 1, str(st["changes"]))
            r = call("sandbox", action="rollback")
            expect(r["undone"] == 1, str(r))
            return r["sandbox"]
        check("sandbox (start, status, rollback)", sandboxed)
        check("fix_issues (dry)", lambda: expect(call("fix_issues", fixes=["missing", "orphans"])["dry_run"]))

        # ---- project output
        check("ffe_schedule", lambda: expect(Path(call("ffe_schedule", assets=[A, str(extra)], output=str(tmp / "ffe.xlsx"))["xlsx"]).exists()))
        check("project_board", lambda: expect(Path(call("project_board", tag=test_tag, output=str(tmp / "board.html"))["html"]).exists()))
        check("client_selection_sheet", lambda: expect(Path(call("client_selection_sheet", options={"Stones": [A, str(extra)]},
                                                                  output=str(tmp / "pick.html"))["html"]).exists()))
        projects = [r for r in call("connecter_call", command="GetRootFolders")["RootFolders"] if "PROJECT" in r["FullPath"].upper()]
        if projects:
            check("asset_usage_analytics", lambda: expect("most_used" in call("asset_usage_analytics", project_folders=[projects[0]["FullPath"]], max_scenes=15)))

        # ---- automation
        def rule_cycle():
            call("create_tag", tag=f"{test_tag}/SUB2")
            r = call("rules", action="add", rule={"name": "test", "kind": "tag", "when": {"name_matches": "MCP Test Model", "folder": str(test_dir)},
                                                  "then": {"tag": f"{test_tag}/SUB2"}})
            run = call("rules", action="run", rule_id=r["added"]["id"])
            expect(run["rules"][0]["matches"] >= 1, str(run))
            call("rules", action="remove", rule_id=r["added"]["id"])
            return run["rules"][0]["matches"]
        check("rules (add, run dry, remove)", rule_cycle)
        check("events (baseline)", lambda: call("events", reset=True)["baseline_recorded"])
        check("events (diff)", lambda: expect("counts" in call("events")))
        check("guardian (run)", lambda: expect("missing_files" in call("guardian", action="run")))
        check("jobs (list)", lambda: call("jobs")["jobs"])

        # ---- visual search (basic backend)
        check("build_index (basic)", lambda: expect(call("build_index", folder=str(test_dir), backend="basic", background=False).get("index_size", 0) >= 1))
        check("find_similar", lambda: expect(call("find_similar", asset=A, backend="basic")["results"]))
        if preview:
            check("style_match", lambda: expect(call("style_match", images=[preview], backend="basic")["palette"]))
            check("identify_render (crop)", lambda: expect(call("identify_render", image=preview, crop=[0, 0, 0.6, 0.6], backend="basic")["results"]))
        if clip:
            check("build_index (clip, background job)", lambda: _wait_job(call("build_index", folder=str(test_dir), backend="clip", threads=1)))
            check("semantic_search", lambda: expect(call("semantic_search", query="river stones", limit=3)["results"]))
            check("auto_tag (visual)", lambda: call("auto_tag", assets=[A])["assets"][0]["suggestions"])

        # ---- 3ds Max tools, script mode only
        check("relink_max_textures (dry)", lambda: call("relink_max_textures", asset=A)["unresolved"])
        check("relink_max_textures (script)", lambda: script_ok(call("relink_max_textures", asset=A, min_confidence=0.0, mode="script", dry_run=False)
                                                                 if call("relink_max_textures", asset=A, min_confidence=0.0)["relinks"]
                                                                 else {"mode": "script", "maxscript": "()"}))
        check("batch_relink (dry)", lambda: expect(call("batch_relink", folder=str(test_dir))["dry_run"]))
        check("measure_asset (script)", lambda: script_ok(call("measure_asset", asset=A, mode="script"), "getSaveRequired", "loadMaxFile", "mcpToCm"))
        check("fix_model (script)", lambda: script_ok(call("fix_model", asset=A, weld_threshold_cm=0.01, mode="script"), "resetXForm", "_fixed.max"))
        check("make_proxy vray (script)", lambda: script_ok(call("make_proxy", asset=A, mode="script"), "vrayMeshExport"))
        check("make_proxy corona (script)", lambda: script_ok(call("make_proxy", asset=A, renderer="corona", mode="script"), "CProxy.ProxyFp.fromScene"))
        check("convert_renderer corona (script)", lambda: script_ok(call("convert_renderer", asset=A, mode="script"), "CoronaConverter.convertScene"))
        check("convert_renderer vray (script)", lambda: script_ok(call("convert_renderer", asset=A, target="vray", mode="script"), "ConvertToVRay false"))
        check("convert_version (script)", lambda: script_ok(call("convert_version", asset=A, mode="script"), "saveAsVersion:2021"))
        check("list_materials (script)", lambda: script_ok(call("list_materials", asset=A, mode="script"), "sceneMaterials"))
        texset = tmp / "Oak Floor"
        texset.mkdir()
        from PIL import Image
        for n in ("Oak_Floor_diffuse.jpg", "Oak_Floor_roughness.jpg", "Oak_Floor_normal.png", "Oak_Floor_preview.jpg"):
            Image.new("RGB", (8, 8)).save(texset / n)

        def material(renderer, cls):
            r = call("create_material", textures=str(texset), renderer=renderer)
            expect(set(r["maps"]) == {"diffuse", "roughness", "normal"}, str(r["maps"]))
            expect(r["name"] == "Oak_Floor", r["name"])
            return script_ok(r, cls)
        check("create_material corona (script)", lambda: material("corona", "CoronaPhysicalMtl"))
        check("create_material vray (script)", lambda: material("vray", "VRayNormalMap"))
        check("create_material physical (script)", lambda: material("physical", "PhysicalMaterial"))
        check("manage_materials (script)", lambda: script_ok(call("manage_materials", library=str(tmp / "lib.mat"), action="add", mode="script"), "saveTempMaterialLibrary"))
        check("merge_into_max (script)", lambda: script_ok(call("merge_into_max", asset=A, position_cm=[100, 0, 0], rotation_z=90, layer="TEST"), "mergeMaxFile"))
        check("swap_placeholder (script)", lambda: script_ok(call("swap_placeholder", asset=A, placeholders="PH_*", fit="footprint"), "mcpFitTo"))
        check("variant_swap (script)", lambda: script_ok(call("variant_swap", target="Sofa01", asset=A), "mcpFitTo"))
        check("furnish_room (script)", lambda: script_ok(call("furnish_room", placements=[{"asset": A, "position_cm": [0, 0, 0]},
                                                                                          {"asset": A, "position_cm": [200, 0, 0], "rotation_z": 180}]), "mcpClone"))
        check("scene_to_library (script)", lambda: script_ok(call("scene_to_library", name="_MCP_TEST export", category=f"{category}/_MCP_TEST"), "saveNodes"))
        check("consistency_check (script)", lambda: script_ok(call("consistency_check", mode="script"), "ATSOps.GetFiles"))

        act = max_activity()
        if act["max_cpu_percent"] > Settings.load().max_busy_cpu:
            def refused():
                try:
                    call("measure_asset", asset=A, mode="batch")
                except Exception as e:  # noqa: BLE001
                    expect("busy" in str(e), str(e))
                    return f"refused at {act['max_cpu_percent']}% CPU"
                raise AssertionError("batch job was not refused")
            check("batch mode refuses while 3ds Max is busy", refused)
        else:
            RESULTS.append(("batch mode refuses while 3ds Max is busy", True, "skipped: 3ds Max idle"))
    finally:
        cleanup(test_dir, test_tag, tmp)
    report()


def _wait_job(r: dict) -> str:
    if "job_id" not in r:
        return r.get("message", "")
    for _ in range(600):
        st = call("jobs", action="status", job_id=r["job_id"])
        if st["status"] in ("done", "failed", "cancelled"):
            expect(st["status"] == "done", st.get("error") or st["status"])
            return str(st["result"])
        time.sleep(1)
    raise AssertionError("job timed out")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(Path(args[0]), args[1] if len(args) > 1 else "09_MISC", "--clip" in sys.argv)
