"""Small persistent configuration store for the local LanDrop process."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any


def default_config_path() -> Path:
    configured = os.environ.get("LANDROP_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser()
    root = Path(os.environ.get("APPDATA", Path.home() / ".config"))
    return root / "LanDrop" / "settings.json"


class SettingsStore:
    """Persist user-editable settings without making the server database-backed."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_config_path()
        self._values: dict[str, Any] = {
            "device_name": socket.gethostname(),
            "download_dir": str(Path.home() / "Downloads" / "LanDrop"),
        }
        self._load()

    @property
    def device_name(self) -> str:
        return str(self._values["device_name"])

    @property
    def download_dir(self) -> Path:
        return Path(str(self._values["download_dir"])).expanduser()

    def as_dict(self) -> dict[str, str]:
        return {
            "device_name": self.device_name,
            "download_dir": str(self.download_dir),
        }

    def update(self, *, device_name: str | None = None, download_dir: str | None = None) -> None:
        if device_name is not None:
            name = str(device_name).strip()
            if not name or len(name) > 100:
                raise ValueError("device_name must contain 1 to 100 characters")
            self._values["device_name"] = name
        if download_dir is not None:
            location = Path(str(download_dir)).expanduser()
            if not location.is_absolute():
                raise ValueError("download_dir must be an absolute path")
            self._values["download_dir"] = str(location)
        self.save()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return
        if isinstance(data, dict):
            self._values.update({key: data[key] for key in ("device_name", "download_dir") if key in data})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._values, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
