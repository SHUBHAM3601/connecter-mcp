"""Change journal and workspace backups, so every write can be reviewed and undone."""

from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

from .config import HOME_DIR

JOURNAL = HOME_DIR / "journal.jsonl"
BACKUPS = HOME_DIR / "backups"


def record(action: str, details: dict, undo: list[dict] | None = None) -> str:
    """Append a change. ``undo`` is a list of API calls ({"type", "payload"}) that reverse it."""
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    entry = {"id": uuid.uuid4().hex[:10], "time": time.strftime("%Y-%m-%d %H:%M:%S"),
             "action": action, "details": details, "undo": undo or [], "undone": False}
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry["id"]


def entries() -> list[dict]:
    if not JOURNAL.exists():
        return []
    return [json.loads(line) for line in JOURNAL.read_text("utf-8").splitlines() if line.strip()]


def mark_undone(entry_id: str) -> None:
    rows = entries()
    for r in rows:
        if r["id"] == entry_id:
            r["undone"] = True
    JOURNAL.write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")


def backup_workspace(db_path: Path, label: str = "manual") -> Path:
    """Copy the workspace database (previews are never deleted by this tool, so the DB is enough)."""
    dest = BACKUPS / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, dest / db_path.name)
    return dest


def list_backups() -> list[dict]:
    if not BACKUPS.exists():
        return []
    return [{"name": d.name, "path": str(d)} for d in sorted(BACKUPS.iterdir(), reverse=True) if d.is_dir()]
