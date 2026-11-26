# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded download history, preserving provider metadata independently of search."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DownloadHistory:
    LIMIT = 200

    def __init__(self, data_dir: Path | None):
        self.path = data_dir / "download-history.json" if data_dir else None
        self.items: list[dict[str, str]] = []
        if self.path:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self.items = [
                        {key: str(item.get(key, "")) for key in ("path", "language", "title", "video")}
                        for item in raw if isinstance(item, dict) and isinstance(item.get("path"), str)
                    ][:self.LIMIT]
            except (OSError, ValueError):
                pass

    def remember(self, item: dict[str, Any]) -> None:
        self._store([dict(item), *(entry for entry in self.items if entry["path"] != item["path"])][:self.LIMIT])

    def remove(self, path: str) -> None:
        self._store([entry for entry in self.items if entry["path"] != path])

    def _store(self, items: list[dict[str, str]]) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        self.items = items
