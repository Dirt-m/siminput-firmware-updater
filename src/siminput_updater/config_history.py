"""The box's config history: the backups written before every save and
every firmware update (`firmware._write_backup`), newest first.

Backups live in `applog.LOG_DIR`; the old `~/SIMINPUT Backups` folder is
read too, so history survives a move of the data folder.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .applog import LOG_DIR
from .config_model import Config

PATTERN = "config-backup-*.json"
STAMP = "%Y%m%d-%H%M%S"


@dataclass
class Backup:
    path: Path
    when: datetime
    name: str          # device name in the backup ("" when unreadable)
    rules: int
    readable: bool


def backup_dirs() -> list[Path]:
    return list(dict.fromkeys([LOG_DIR, Path.home() / "SIMINPUT Backups"]))


def _when(path: Path) -> datetime:
    stamp = path.stem.removeprefix("config-backup-")
    try:
        return datetime.fromtimestamp(time.mktime(time.strptime(stamp, STAMP)))
    except ValueError:
        return datetime.fromtimestamp(path.stat().st_mtime)


def read_backup(path: Path) -> Config:
    return Config.from_json(path.read_text(encoding="utf-8"))


def list_backups(dirs: list[Path] | None = None) -> list[Backup]:
    found: dict[Path, Backup] = {}
    for d in dirs if dirs is not None else backup_dirs():
        if not d.is_dir():
            continue
        for path in d.glob(PATTERN):
            key = path.resolve()
            if key in found:
                continue
            try:
                config = read_backup(path)
                info = Backup(path, _when(path), config.device.name,
                              sum(1 for r in config.rules if not r.comment), True)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
                info = Backup(path, _when(path), "", 0, False)
            found[key] = info
    return sorted(found.values(), key=lambda b: b.when, reverse=True)
