"""Locate Connecter's install, workspace, message folder and ports, plus user settings."""

from __future__ import annotations

import json
import os
import tomllib
import winreg
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

REG_KEY = r"Software\Design Connected\Connecter"
APPDATA_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "Design Connected" / "Connecter"
HOME_DIR = Path(os.environ.get("CONNECTER_MCP_HOME", Path.home() / ".connecter-mcp"))
CONFIG_FILE = HOME_DIR / "config.toml"


def _reg(subkey: str, name: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def _xml_setting(path: Path, key: str) -> str | None:
    try:
        for s in ET.parse(path).getroot().iter("setting"):
            if s.get("key") == key:
                return (s.text or "").strip()
    except (OSError, ET.ParseError):
        pass
    return None


@dataclass
class Paths:
    install_dir: Path | None
    version: str | None
    buffer_dir: Path | None
    json_port: int | None
    workspace_dir: Path | None
    appdata_dir: Path = APPDATA_DIR

    @property
    def db_path(self) -> Path | None:
        return self.workspace_dir / "default.dcdb" if self.workspace_dir else None

    @property
    def previews_dir(self) -> Path | None:
        return self.workspace_dir / "Media" / "previews" if self.workspace_dir else None

    @property
    def log_path(self) -> Path:
        return self.appdata_dir / "logs" / "connecter_log_00.tx1"

    @property
    def exe(self) -> Path | None:
        return self.install_dir / "Connecter.exe" if self.install_dir else None


def discover() -> Paths:
    """Read Connecter's locations from the registry and its own settings files."""
    integration = {}
    try:
        integration = json.loads((APPDATA_DIR / "integration" / "IntegrationSettings.json").read_text("utf-8"))
    except (OSError, ValueError):
        pass

    port = _reg(REG_KEY + r"\Integration", "JsonMessagingTcpPort") or integration.get("JsonMessagingTcpPort")
    buffer_dir = _reg(REG_KEY, "9") or integration.get("MessagesDirectoryPath")
    install_dir = _reg(REG_KEY, "InstallDir")
    workspace = _xml_setting(APPDATA_DIR / "personal" / "PersonalSettings.xml", "1")

    return Paths(
        install_dir=Path(install_dir) if install_dir else None,
        version=_reg(REG_KEY, "Version"),
        buffer_dir=Path(buffer_dir) if buffer_dir else None,
        json_port=int(port) if port else None,
        workspace_dir=Path(workspace) if workspace else None,
    )


@dataclass
class Settings:
    """User settings from ~/.connecter-mcp/config.toml (all optional)."""

    # Folder whose sub-folders are the library categories, e.g. "Y:\\LIBRARY\\3D MODELS".
    models_dir: Path | None = None
    # Extra folders searched when hunting for missing assets or textures.
    search_dirs: list[Path] = field(default_factory=list)
    # Working directory Connecter is normally started from (its Start-menu shortcut uses one).
    launch_dir: Path | None = None
    # Where new downloads land (ingest_queue). Defaults to ~/Downloads.
    downloads_dir: Path = field(default_factory=lambda: Path.home() / "Downloads")
    # 3ds Max install folder for headless jobs (3dsmaxbatch.exe). Defaults to the newest installed version.
    max_dir: Path | None = None
    # Headless 3ds Max jobs are refused while 3ds Max uses more CPU than this (in % of one core), e.g. while rendering.
    max_busy_cpu: float = 50.0

    @classmethod
    def load(cls) -> "Settings":
        data = {}
        if CONFIG_FILE.exists():
            data = tomllib.loads(CONFIG_FILE.read_text("utf-8"))
        return cls(
            models_dir=Path(data["models_dir"]) if data.get("models_dir") else None,
            search_dirs=[Path(p) for p in data.get("search_dirs", [])],
            launch_dir=Path(data["launch_dir"]) if data.get("launch_dir") else None,
            downloads_dir=Path(data["downloads_dir"]) if data.get("downloads_dir") else Path.home() / "Downloads",
            max_dir=Path(data["max_dir"]) if data.get("max_dir") else None,
            max_busy_cpu=float(data.get("max_busy_cpu", 50.0)),
        )
