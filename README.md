# connecter-mcp

An [MCP](https://modelcontextprotocol.io) server for **[Connecter](https://www.designconnected.com/connecter)**, Design Connected's asset manager for 3D artists. Its 95 tools let Claude (or any MCP client) search, tag, catalogue, ingest, audit and repair your Connecter library while Connecter keeps running. It can also fix and convert models with 3ds Max, assemble scenes, search the library by what models look like, produce FF&E schedules and client boards, and keep the library tidy automatically.

> Unofficial. Not affiliated with or endorsed by Design Connected. It uses the same local integration channels Connecter's own 3ds Max, Blender and Maya plugins use. No Connecter code is included.

## What it can do

| Area | Tools |
|---|---|
| **System** | `connecter_status`, `self_test`, `restart_connecter`, `read_log`, `list_commands`, `connecter_call` (raw access to any integration command) |
| **Assets** | `search_assets`, `get_asset`, `catalogue_asset`, `set_description`, `tag_assets`, `untag_assets`, `set_previews`, `remove_previews`, `show_in_connecter`, `asset_properties`, `asset_versions` |
| **Tags** | `list_tags`, `create_tag`, `rename_tag`, `move_tag`, `delete_tag`, `set_tag_color`, `sync_tags_to_folders` (tag models from their library folder), `restructure_taxonomy` (bulk-move assets between tags) |
| **Files & folders** | `move_asset`, `rename_asset`, `folder_operation`, `rename_cleanly` (strip download-site prefixes from folder names), `rescan`: changes made through Connecter, so tags and previews follow the files |
| **Ingestion** | `ingest_queue` (downloads waiting to be filed), `library_categories`, `inspect_folder` (tells previews apart from texture maps), `ingest_model` (move a download into the library and catalogue it in one step) |
| **Health** | `audit_library`, `library_stats`, `find_duplicates`, `attach_folder_previews` (give preview-less models the images in their folder), `orphan_cleanup` |
| **Missing assets** | `find_missing_assets`, `relocate_asset`, `batch_relocate`, `relocate_folder`, `resolve_deleted` |
| **Textures** | `scan_textures` (reads map paths straight from `.max` files, no 3ds Max needed), `find_texture_candidates`, `localize_textures`, `relink_script` |
| **Library upkeep** | `hide_files` (hide texture maps from the asset grid), `generate_preview` (from the thumbnail inside the .max, or a clay render), `fix_issues` (everything `audit_library` finds, in one go) |
| **Textures in .max files** | `verify_textures`, `relink_max_textures` (rewrites the paths inside the .max), `batch_relink`, `texture_optimizer` (shrink oversized maps in place) |
| **Models (3ds Max)** | `measure_asset`, `fix_model`, `make_proxy` (V-Ray / Corona), `convert_renderer` (Corona <-> V-Ray with their own converters), `convert_version` |
| **Materials (3ds Max)** | `list_materials`, `create_material` (PBR material from a texture set), `manage_materials` (.mat libraries) |
| **Scenes (3ds Max)** | `merge_into_max`, `swap_placeholder`, `variant_swap`, `furnish_room`, `scene_to_library`, `scene_asset_report`, `scene_missing_assets`, `collect_for_project`, `consistency_check` |
| **AI cataloguing** | `auto_tag`, `auto_describe`, `extract_metadata`, `detect_brand`, `batch_catalogue` |
| **Visual search** | `build_index`, `semantic_search` ("round travertine coffee table"), `find_similar`, `style_match` (mood boards), `identify_render` (which model is this?) |
| **Projects & output** | `ffe_schedule` (Excel + CSV), `project_board` (printable HTML), `client_selection_sheet`, `asset_usage_analytics` |
| **Automation** | `rules` (auto-file downloads, auto-tag), `events` (what changed), `jobs` (background work), `guardian` (scheduled health checks) |
| **Safety** | `backup_workspace`, `restore_workspace`, `sandbox`, `change_journal`, `undo` |

Every change is written to a journal (`~/.connecter-mcp/journal.jsonl`) with the API calls that reverse it, so `undo` can roll it back. Files changed on disk (relinked .max files, resized textures, material libraries) are backed up first and `undo` copies them back. Tools that move files or touch many assets default to `dry_run=True` and return a plan first.

## Working with 3ds Max

Tools that only read a .max (`verify_textures`, `scene_missing_assets`, `scene_asset_report`, `list_materials`, `extract_metadata`, `consistency_check` in file mode) read the file directly: no 3ds Max needed. Thumbnails, scene totals, materials, plug-ins and every referenced file are stored inside each .max.

The others build MAXScript and run it one of two ways:

* `mode="script"`: nothing is started. The tool returns the script, which you run in your open 3ds Max (for example with a 3ds Max MCP's `execute_maxscript`). Scene tools (`merge_into_max`, `furnish_room`, ...) always work this way. Scripts that open another file refuse to run over unsaved changes.
* `mode="batch"`: the script runs headless in `3dsmaxbatch.exe` at below-normal priority. **It refuses to start while 3ds Max is busy** (more than `max_busy_cpu`, e.g. while rendering) or memory is low, unless you pass `force=True`.

Renderer conversion uses the converters that ship with Corona (`coronaConverter.ms`) and V-Ray (`vrayutils.ms`).

## Visual search

`build_index` gives each model a visual fingerprint from its first preview, or from the thumbnail inside its .max. `backend="clip"` understands content and text queries. It runs CLIP in a separate Python process through uv at idle priority, so it never competes with a render. The first run downloads the model (about 350 MB). `backend="basic"` needs nothing extra but only compares colour and layout. Indexing runs as a background job (`jobs`).

## How it talks to Connecter

* **JSON over TCP.** A running Connecter listens on a local port (stored in the registry under `HKCU\Software\Design Connected\Connecter\Integration\JsonMessagingTcpPort`). Each request is one JSON object on its own connection, with a `ResponseEndpoint` that Connecter connects back to with the reply.
* **Message folder.** JSON files dropped into Connecter's `integrationBuffer` folder catalogue assets with tags and previews. This is the same mechanism its DCC plugins use.
* **Read-only database snapshots** for fast library-wide searches and audits. The live `default.dcdb` is copied first and never written to.

## Requirements

* Windows, with Connecter 8.x installed and **running**
* [uv](https://docs.astral.sh/uv/) (installs Python 3.11+ and the dependencies automatically)
* Optional: 3ds Max 2021+ for the 3ds Max tools (tested with 2024, Corona and V-Ray installed)

## Install

```bash
git clone https://github.com/SHUBHAM3601/connecter-mcp.git
```

Register it with Claude Code:

```bash
claude mcp add connecter --scope user -- uv --directory "C:\path\to\connecter-mcp" run connecter-mcp
```

Or add it to any MCP client's config:

```json
{
  "mcpServers": {
    "connecter": {
      "command": "uv",
      "args": ["--directory", "C:\\path\\to\\connecter-mcp", "run", "connecter-mcp"]
    }
  }
}
```

## Configure (optional)

Create `%USERPROFILE%\.connecter-mcp\config.toml`. See [`config.example.toml`](config.example.toml):

```toml
# Folder whose sub-folders are your library categories (enables ingest_model and library_categories)
models_dir = 'D:\Library\3D MODELS'
# Extra places to search for moved assets and missing textures
search_dirs = ['E:\Old Library']
# Folder Connecter is normally started from (used by restart_connecter)
launch_dir = 'C:\Users\you\Documents\Design Connected\Connecter\Sample Products'
# 3ds Max for headless jobs (default: newest installed) and how busy it may be before they wait
max_dir = 'C:\Program Files\Autodesk\3ds Max 2024'
max_busy_cpu = 150
```

## Examples

* *"Move `Downloads\Pro 3DSky - Pebble River Collection` into the library under 09_MISC and tag it."* → `inspect_folder`, then `ingest_model` (dry run, then real)
* *"Which models have no tags?"* → `search_assets(untagged=True, extension=".max")`
* *"Some assets show as missing in Connecter, fix them."* → `batch_relocate` (dry run, then real)
* *"Why does this model render without its normal map?"* → `scan_textures`, `find_texture_candidates`, `localize_textures`
* *"Make a LANDSCAPE tag under 09_MISC and move the pebbles into it."* → `create_tag`, `tag_assets`, `untag_assets`
* *"Find me a round travertine coffee table."* → `semantic_search`
* *"Swap the PH_SOFA boxes for this Minotti sofa."* → `swap_placeholder`, then run the script in 3ds Max
* *"Make an FF&E schedule of everything this scene uses."* → `ffe_schedule(scene=...)`
* *"File new sofa downloads under 02_FURNITURES/SOFAS automatically."* → `rules(action="add", ...)`, `guardian(action="start", auto_fix=True)`

## Testing

`tests/live_test.py` exercises every tool through the MCP layer against a running Connecter. It works on a throwaway copy of one model in `<models_dir>/<category>/_MCP_TEST` and removes it afterwards:

```bash
uv run python tests/live_test.py "D:\Library\3D MODELS\09_MISC\Some Model"
```

`tests/extended_test.py` covers the 3ds Max, AI, project and automation tools the same way. It never starts 3ds Max: the 3ds Max tools are checked in script mode, and batch mode only to confirm it is refused while 3ds Max is busy. Add `--clip` to test CLIP search too.

```bash
uv run python tests/extended_test.py "D:\Library\3D MODELS\09_MISC\Some Model" --clip
```

## Connecter behaviours worth knowing

* **One tag per branch.** An asset can't hold a tag and its own parent or child tag. Tagging with one replaces the other, and `tag_assets` reports how many it replaced (and `undo` puts them back). Sibling tags and tags in other branches can be combined freely.
* **Tag colours** must come from Connecter's palette: `cyan`, `purple`, `lavender`, `yellow`, `pink`, `white`.
* **Renaming assets.** Connecter 8.0's own rename drops the file extension, so `rename_asset` renames on disk and re-points the catalogue entry instead.
* **Custom properties and versions** are only available in some Connecter workspaces; on others these tools say so.
* **Hiding files.** There is no hide command in Connecter's API, so `hide_files` uses the hiding Connecter applies to previews. Files can only be unhidden in Connecter itself (right-click > Unhide).
* **Removing previews by path** is rejected by Connecter ("invalid asset paths"), so the tools look up each preview's GUID first.

## Notes

* Built against Connecter 8.0. Integration commands are discovered from Connecter's public plugin behaviour; a Connecter update can change them. Run `connecter_status` after updating.
* `restart_connecter` starts Connecter from the same working folder as its Start-menu shortcut. Starting it from elsewhere makes its 3ds Max integration check fail.

## License

MIT
