"""End-to-end test of every tool against a running Connecter.

Works on a throwaway copy of one library model placed in <models_dir>/<category>/_MCP_TEST and
cleans everything up afterwards. Run:  uv run python tests/live_test.py "<path to a model folder>" [category]
The model folder should hold one .max, a few preview images and (ideally) texture maps.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

from connecter_mcp import server as s
from connecter_mcp.config import Settings

RESULTS: list[tuple[str, bool, str]] = []


def call(tool_name: str, /, **args):
    """Call a tool through the MCP layer (exercises argument validation, errors and serialisation)."""
    result = asyncio.run(s.mcp.call_tool(tool_name, args))
    text = result.content[0].text if result.content else "null"
    try:
        return json.loads(text)
    except ValueError:
        return text


def check(label: str, fn):
    try:
        detail = fn()
        RESULTS.append((label, True, "" if detail is None else str(detail)[:160]))
    except Exception as e:  # noqa: BLE001 - every failure goes in the report
        RESULTS.append((label, False, f"{type(e).__name__}: {e}"[:400]))
        traceback.print_exc()


def expect(cond, msg="unexpected result"):
    if not cond:
        raise AssertionError(msg)


def tags_of(path: str) -> set[str]:
    return {"/".join(t["TagPath"]) for t in call("get_asset", asset=path)["Tags"]}


def main(source: Path, category: str) -> None:
    models_dir = Settings.load().models_dir
    expect(models_dir, "models_dir must be set in the config")
    test_tag = f"{category}/_MCP_TEST"
    test_dir = models_dir / category / "_MCP_TEST"
    tmp = Path(tempfile.mkdtemp(prefix="connecter-mcp-test-"))
    download = tmp / "Pro 3DSky - MCP Test Model"
    shutil.copytree(source, download, ignore=shutil.ignore_patterns("*.obj", "*.fbx", "*.mtl"))
    max_name = next(download.glob("*.max")).name
    model_dir = test_dir / "MCP Test Model"
    A = str(model_dir / max_name)
    try:
        # ---- system
        check("connecter_status", lambda: expect(call("connecter_status")["running"]))
        check("self_test", lambda: expect(call("self_test")["passed"]))
        check("list_commands", lambda: expect("Tags" in call("list_commands", group="tag")))
        check("read_log", lambda: expect(isinstance(call("read_log", lines=5)["lines"], list)))
        check("connecter_call", lambda: expect(call("connecter_call", command="GetRootFolders")["RootFolders"]))
        check("restart_connecter (dry)", lambda: expect(call("restart_connecter")["dry_run"]))
        check("error message surfaces", lambda: expect(str(_error_of("get_asset", asset=r"Z:\nope.max")).split("get_asset:")[-1].strip(),
                                                        "error has no detail"))

        # ---- ingestion
        check("ingest_queue", lambda: expect(any(i.get("folder") == str(download) for i in call("ingest_queue", folder=str(tmp))["pending"])))
        check("inspect_folder", lambda: expect(call("inspect_folder", folder=str(download))["models"]))
        check("library_categories", lambda: expect(call("library_categories")["categories"]))
        check("ingest_model (dry)", lambda: expect(call("ingest_model", source_folder=str(download), category=f"{category}/_MCP_TEST")["dry_run"]))
        check("ingest_model", lambda: (call("ingest_model", source_folder=str(download), category=f"{category}/_MCP_TEST",
                                            description="mcp test", dry_run=False),
                                       expect(Path(A).exists() and not download.exists(), "files not moved"),
                                       expect(test_tag in tags_of(A), "tag missing"))[2])

        # ---- assets
        check("search_assets", lambda: expect(call("search_assets", query="MCP Test Model")["total"] >= 1))
        check("get_asset", lambda: expect(call("get_asset", asset=A)["FileExists"]))

        def description():
            r = call("set_description", asset=A, description="changed")
            expect(call("get_asset", asset=A)["Description"] == "changed")
            call("undo", journal_id=r["journal_id"])
            expect(call("get_asset", asset=A)["Description"] == "mcp test", "undo did not restore description")
        check("set_description + undo", description)

        def replace_tags():
            r = call("catalogue_asset", asset_path=A, tags=[category], replace_tags=True)
            expect(tags_of(A) == {category}, f"tags after replace: {tags_of(A)}")
            call("undo", journal_id=r["journal_id"])
            expect(tags_of(A) == {test_tag}, f"tags after undo: {tags_of(A)}")
        check("catalogue_asset replace_tags + undo", replace_tags)

        def previews():
            before = call("get_asset", asset=A)["CustomPreviews"]
            expect(len(before) >= 1, "no previews after ingest")
            first = str(model_dir / Path(before[0]["FileFullPath"]).name)
            r = call("set_previews", asset=A, images=[first], replace=True)
            expect(len(call("get_asset", asset=A)["CustomPreviews"]) == 1, "replace did not leave one preview")
            call("remove_previews", asset=A, images=[first])
            expect(len(call("get_asset", asset=A)["CustomPreviews"]) == 0, "remove_previews failed")
            call("set_previews", asset=A, images=[str(model_dir / Path(p["FileFullPath"]).name) for p in before])
            expect(len(call("get_asset", asset=A)["CustomPreviews"]) == len(before), "previews not restored")
            return r["journal_id"]
        check("set_previews / remove_previews", previews)

        def replace_previews():
            n = len(call("get_asset", asset=A)["CustomPreviews"])
            img = call("get_asset", asset=A)["CustomPreviews"][0]["FileFullPath"]
            r = call("catalogue_asset", asset_path=A, previews=[str(model_dir / Path(img).name)], replace_previews=True)
            got = len(call("get_asset", asset=A)["CustomPreviews"])
            expect(got == 1, f"replace_previews left {got} previews (had {n})")
            call("undo", journal_id=r["journal_id"])
            got = len(call("get_asset", asset=A)["CustomPreviews"])
            expect(got == n, f"undo left {got} previews (expected {n})")
        check("catalogue_asset replace_previews", replace_previews)

        def tag_untag():
            other = next(t["tag"] for t in call("list_tags")["tags"] if "/" not in t["tag"] and t["tag"] != category)
            call("tag_assets", assets=[A], tag=other)
            expect(tags_of(A) == {test_tag, other}, f"after tag: {tags_of(A)}")
            call("untag_assets", assets=[A], tag=other)
            expect(tags_of(A) == {test_tag}, f"after untag: {tags_of(A)}")
            # Connecter keeps one tag per branch: tagging the parent replaces the child, and undo restores it.
            r = call("tag_assets", assets=[A], tag=category)
            expect(tags_of(A) == {category} and r["replaced_parent_or_child_tags"] == 1, f"parent: {tags_of(A)} {r}")
            call("undo", journal_id=r["journal_id"])
            expect(tags_of(A) == {test_tag}, f"after undo: {tags_of(A)}")
        check("tag_assets / untag_assets", tag_untag)
        check("show_in_connecter", lambda: call("show_in_connecter", assets=[A]))
        def unsupported_or_ok(name):
            err = _error_of(name, asset=A)
            expect(err is None or "doesn't support" in err, f"unclear error: {err}")
            return err or "ok"
        check("asset_properties (supported or clear error)", lambda: unsupported_or_ok("asset_properties"))
        check("asset_versions (supported or clear error)", lambda: unsupported_or_ok("asset_versions"))

        # ---- tags
        def tags():
            sub = f"{test_tag}/SUB"
            call("create_tag", tag=sub)
            call("rename_tag", tag=sub, new_name="SUB2")
            call("move_tag", tag=f"{test_tag}/SUB2", new_parent=category)
            call("set_tag_color", tag=f"{category}/SUB2", color="cyan")
            expect("palette" in str(_error_of("set_tag_color", tag=f"{category}/SUB2", color="#123456")), "bad colour not explained")
            call("set_tag_color", tag=f"{category}/SUB2")
            expect(call("delete_tag", tag=f"{category}/SUB2")["dry_run"])
            call("tag_assets", assets=[A], tag=f"{category}/SUB2")
            r = call("delete_tag", tag=f"{category}/SUB2", confirm=True)
            call("undo", journal_id=r["journal_id"])
            expect(f"{category}/SUB2" in tags_of(A), "undo of delete_tag did not re-tag the asset")
            call("delete_tag", tag=f"{category}/SUB2", confirm=True)
            expect(not call("list_tags", filter="SUB2")["tags"])
        check("tag create/rename/move/color/delete/undo", tags)

        def restructure():
            r = call("restructure_taxonomy", mapping={test_tag: f"{test_tag}/MOVED"}, dry_run=False)
            expect(tags_of(A) == {f"{test_tag}/MOVED"}, f"after: {tags_of(A)}")
            call("undo", journal_id=r["done"][0]["journal_id"])
            expect(tags_of(A) == {test_tag}, f"after undo: {tags_of(A)}")
            call("delete_tag", tag=f"{test_tag}/MOVED", confirm=True)
        check("restructure_taxonomy + undo", restructure)
        check("sync_tags_to_folders (dry)", lambda: call("sync_tags_to_folders", folder=str(test_dir), only_untagged=False))
        check("attach_folder_previews (dry)", lambda: call("attach_folder_previews", folder=str(test_dir)))
        check("find_duplicates", lambda: expect(call("find_duplicates", folder=str(models_dir / category))["duplicate_groups"] >= 1,
                                                 "test copy not detected as duplicate"))
        check("library_stats", lambda: expect(call("library_stats")["total"]["max_files"] > 0))

        # ---- textures
        check("scan_textures", lambda: call("scan_textures", asset=A, only_missing=False))
        check("find_texture_candidates", lambda: call("find_texture_candidates", asset=A))
        check("localize_textures (dry)", lambda: call("localize_textures", asset=A))
        check("relink_script", lambda: expect("loadMaxFile" in call("relink_script", asset=A)["maxscript"]))

        # ---- files and folders
        def rename_move():
            r = call("rename_asset", asset=A, new_name="Renamed Test")
            renamed = model_dir / ("Renamed Test" + Path(A).suffix)
            expect(renamed.exists(), f"rename result: {r}")
            call("undo", journal_id=r["journal_id"])
            expect(Path(A).exists(), "undo rename failed")
            call("folder_operation", operation="create", folder=str(model_dir / "sub"))
            r = call("move_asset", asset=A, to_folder=str(model_dir / "sub"))
            expect((model_dir / "sub" / Path(A).name).exists(), f"move result: {r}")
            call("undo", journal_id=r["journal_id"])
            expect(Path(A).exists(), "undo move failed")
        check("rename_asset / move_asset / undo", rename_move)

        def folders():
            call("folder_operation", operation="rename", folder=str(model_dir), target="Pro 3DSky - MCP Test Model")
            dirty = test_dir / "Pro 3DSky - MCP Test Model"
            expect(dirty.exists())
            plan = call("rename_cleanly", folder=str(test_dir))
            expect(any(p["new_name"] == "MCP Test Model" for p in plan["renames"]), f"plan: {plan}")
            call("rename_cleanly", folder=str(test_dir), dry_run=False)
            expect(Path(A).exists(), "rename_cleanly did not restore the clean name")
            expect(tags_of(A) == {test_tag}, "tags lost after folder renames")
        check("folder_operation rename / rename_cleanly", folders)
        check("rescan", lambda: expect(call("rescan", folder=str(test_dir))["notified"] >= 1))

        # ---- missing assets
        def relocate():
            moved = test_dir / "Moved Outside Connecter"
            new_a = str(moved / Path(A).name)
            # Out of the library and back under a new name: Connecter sees a delete plus a create, not a rename
            # (a plain rename is tracked by its file watcher, so nothing would go missing).
            shutil.move(str(model_dir), str(tmp / "away"))
            time.sleep(3)
            shutil.move(str(tmp / "away"), str(moved))
            try:
                expect(call("find_missing_assets", folder=str(test_dir))["missing_count"] >= 1, "missing asset not found")
                plan = call("relocate_folder", old_folder=str(model_dir), new_folder=str(moved))
                expect(plan["would_relocate"] >= 1, f"plan: {plan}")
                call("relocate_folder", old_folder=str(model_dir), new_folder=str(moved), dry_run=False)
                expect(call("find_missing_assets", folder=str(test_dir), find_candidates=False)["missing_count"] == 0)
                expect(tags_of(new_a) == {test_tag}, "tags lost on relocate")
            finally:
                moved.rename(model_dir)
            r = call("relocate_asset", asset=new_a, new_path=A)
            expect(tags_of(A) == {test_tag})
            return r["journal_id"]
        check("relocate_folder / relocate_asset", relocate)
        check("batch_relocate (dry)", lambda: call("batch_relocate", folder=str(test_dir)))

        def deleted():
            parked = tmp / Path(A).name
            shutil.move(A, parked)
            try:
                rep = call("resolve_deleted", folder=str(test_dir))
                expect(rep["holding_data"] >= 1, f"report: {rep}")
                call("resolve_deleted", folder=str(test_dir), archive=True)
                entry = call("change_journal", limit=1)["changes"][0]
                expect(entry["action"] == "resolve_deleted")
                call("undo", journal_id=entry["id"])
            finally:
                shutil.move(parked, A)
            expect(tags_of(A) == {test_tag}, f"undo did not re-tag: {tags_of(A)}")
        check("resolve_deleted archive + undo", deleted)

        # ---- health and safety
        check("audit_library", lambda: call("audit_library", limit=3))
        check("orphan_cleanup (dry)", lambda: expect(call("orphan_cleanup")["dry_run"]))
        check("backup_workspace", lambda: expect(call("backup_workspace", label="live-test")["backup"]))
        check("restore_workspace (dry)", lambda: expect(call("restore_workspace", backup=call("backup_workspace")["all_backups"][0]["name"])["dry_run"]))
        check("change_journal", lambda: expect(call("change_journal", limit=5)["changes"]))
    finally:
        cleanup(test_dir, test_tag, tmp)
    report()


def _error_of(name: str, **args) -> str | None:
    try:
        asyncio.run(s.mcp.call_tool(name, args))
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


def cleanup(test_dir: Path, test_tag: str, tmp: Path) -> None:
    test_tags = {t["tag"] for f in ("_MCP_TEST", "SUB2") for t in call("list_tags", filter=f)["tags"]}
    for t in sorted(test_tags, key=len, reverse=True):
        try:
            call("delete_tag", tag=t, confirm=True)
        except Exception as e:  # noqa: BLE001
            print("cleanup: tag", t, e)
    if test_dir.exists():
        try:
            call("folder_operation", operation="delete", folder=str(test_dir), confirm=True)
        except Exception as e:  # noqa: BLE001
            print("cleanup: folder via Connecter failed:", e)
        time.sleep(2)
        shutil.rmtree(test_dir, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    left = call("find_missing_assets", folder=str(test_dir), find_candidates=False)["missing_count"]
    RESULTS.append(("cleanup leaves no missing assets", left == 0, f"{left} left"))


def report() -> None:
    width = max(len(r[0]) for r in RESULTS)
    for label, ok, detail in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {label.ljust(width)}  {detail if not ok else ''}")
    failed = sum(1 for r in RESULTS if not r[1])
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "09_MISC")
