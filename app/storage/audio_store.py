"""On-disk audio layout: data/meetings/<uuid>/source/chunk_*.* plus master/json files."""

from __future__ import annotations

import json
from pathlib import Path


class AudioStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)

    def meeting_dir(self, meeting_id: str) -> Path:
        path = self.data_dir / "meetings" / meeting_id
        path.mkdir(parents=True, exist_ok=True)
        (path / "source").mkdir(exist_ok=True)
        return path

    def source_dir(self, meeting_id: str) -> Path:
        return self.meeting_dir(meeting_id) / "source"

    def chunk_path(self, meeting_id: str, seq: int, ext: str = "wav") -> Path:
        ext = ext.lstrip(".")
        return self.source_dir(meeting_id) / f"chunk_{seq:06d}.{ext}"

    def master_path(self, meeting_id: str) -> Path:
        return self.meeting_dir(meeting_id) / "meeting_master.wav"

    def write_json(self, meeting_id: str, name: str, payload) -> Path:
        path = self.meeting_dir(meeting_id) / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_json(self, meeting_id: str, name: str):
        path = self.meeting_dir(meeting_id) / name
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
