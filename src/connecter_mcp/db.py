"""Read-only access to Connecter's workspace database.

The live ``default.dcdb`` is never opened directly: every read works on a fresh copy,
so the running Connecter can't be disturbed. All writes go through the API instead.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Asset:
    id: int
    guid: str
    path: str
    description: str | None
    tags: list[str]
    previews: int

    def to_dict(self) -> dict:
        return {"guid": self.guid, "path": self.path, "description": self.description,
                "tags": self.tags, "previews": self.previews}


class WorkspaceDb:
    def __init__(self, db_path: Path):
        self._tmp = Path(tempfile.mkdtemp(prefix="connecter-mcp-"))
        snap = self._tmp / "snapshot.dcdb"
        shutil.copy2(db_path, snap)
        self.conn = sqlite3.connect(snap)

    def close(self) -> None:
        self.conn.close()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self) -> "WorkspaceDb":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def tag_paths(self) -> dict[int, str]:
        rows = {i: (p, n) for i, p, n in self.conn.execute("select ID, Parent_ID, Name from Tags")}

        def path(i: int) -> str:
            parent, name = rows[i]
            return f"{path(parent)}/{name}" if parent in rows else name

        return {i: path(i) for i in rows}

    def assets(self) -> list[Asset]:
        paths = self.tag_paths()
        tags: dict[int, list[str]] = {}
        for item, tag in self.conn.execute("select Item_ID, Tag_ID from Items_Tags"):
            tags.setdefault(item, []).append(paths.get(tag, "?"))
        previews = dict(self.conn.execute("select Item_ID, count(*) from Previews group by Item_ID"))
        return [Asset(i, g, p, d, sorted(tags.get(i, [])), previews.get(i, 0))
                for i, g, p, d in self.conn.execute("select ID, Guid, FullPath, Description from Items")]

    def hidden_files(self) -> list[str]:
        return [r[0] for r in self.conn.execute("select FullPathEnc from ItemAttributes where Attribute = 1 and Value = 'True'")]

    def preview_files(self) -> list[tuple[int, str, str]]:
        return list(self.conn.execute("select Item_ID, LocalFileNameEnc, OriginalFullPathEnc from Previews"))

    def root_folders(self) -> list[str]:
        return [r[0] for r in self.conn.execute("select FullPath from Repositories")]
