# connecter-mcp

An [MCP](https://modelcontextprotocol.io) server for **[Connecter](https://www.designconnected.com/connecter)**, Design Connected's asset manager for 3D artists. It lets Claude (or any MCP client) search, tag, catalogue, ingest, audit and repair your Connecter library while Connecter keeps running.

> Unofficial. Not affiliated with or endorsed by Design Connected. It uses the same local integration channels Connecter's own 3ds Max, Blender and Maya plugins use. No Connecter code is included.

## What it can do

| Area | Tools |
|---|---|
| **System** | `connecter_status`, `restart_connecter`, `read_log`, `list_commands`, `connecter_call` (raw access to any integration command) |
| **Assets** | `search_assets`, `get_asset`, `catalogue_asset`, `set_description`, `tag_assets`, `untag_assets`, `set_previews`, `remove_previews`, `show_in_connecter`, `asset_properties`, `asset_versions` |
| **Tags** | `list_tags`, `create_tag`, `rename_tag`, `move_tag`, `delete_tag`, `set_tag_color` |
| **Files & folders** | `move_asset`, `rename_asset`, `folder_operation`: moves made through Connecter, so tags and previews follow the files |
| **Ingestion** | `library_categories`, `inspect_folder` (tells previews apart from texture maps), `ingest_model` (move a download into the library and catalogue it in one step) |
| **Health** | `audit_library`, `find_missing_assets`, `relocate_asset`, `batch_relocate` |
| **Textures** | `scan_textures` (reads map paths straight from `.max` files, no 3ds Max needed), `find_texture_candidates`, `localize_textures`, `relink_script` |
| **Safety** | `backup_workspace`, `change_journal`, `undo` |

Every change is written to a journal (`~/.connecter-mcp/journal.jsonl`) with the API calls that reverse it, so `undo` can roll it back. Tools that move files or touch many assets default to `dry_run=True` and return a plan first.

## How it talks to Connecter

* **JSON over TCP.** A running Connecter listens on a local port (stored in the registry under `HKCU\Software\Design Connected\Connecter\Integration\JsonMessagingTcpPort`). Each request is one JSON object on its own connection, with a `ResponseEndpoint` that Connecter connects back to with the reply.
* **Message folder.** JSON files dropped into Connecter's `integrationBuffer` folder catalogue assets with tags and previews. This is the same mechanism its DCC plugins use.
* **Read-only database snapshots** for fast library-wide searches and audits. The live `default.dcdb` is copied first and never written to.

## Requirements

* Windows, with Connecter 8.x installed and **running**
* [uv](https://docs.astral.sh/uv/) (installs Python 3.11+ and the dependencies automatically)

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
```

## Examples

* *"Move `Downloads\Pro 3DSky - Pebble River Collection` into the library under 09_MISC and tag it."* → `inspect_folder`, then `ingest_model` (dry run, then real)
* *"Which models have no tags?"* → `search_assets(untagged=True, extension=".max")`
* *"Some assets show as missing in Connecter, fix them."* → `batch_relocate` (dry run, then real)
* *"Why does this model render without its normal map?"* → `scan_textures`, `find_texture_candidates`, `localize_textures`
* *"Make a LANDSCAPE tag under 09_MISC and move the pebbles into it."* → `create_tag`, `tag_assets`, `untag_assets`

## Notes

* Built against Connecter 8.0. Integration commands are discovered from Connecter's public plugin behaviour; a Connecter update can change them. Run `connecter_status` after updating.
* `restart_connecter` starts Connecter from the same working folder as its Start-menu shortcut. Starting it from elsewhere makes its 3ds Max integration check fail.

## License

MIT
