"""Lightweight JSON schemas used by the API layer."""

from __future__ import annotations


def meeting_out(m) -> dict:
    return {"id": m.id, "name": m.name, "status": m.status}


def segment_out(s) -> dict:
    return {
        "id": s.id,
        "raw_text": s.raw_text,
        "corrected_text": s.corrected_text,
        "display_text": s.display_text,
        "speaker_label": s.speaker_label,
        "start_ms": s.start_ms,
        "end_ms": s.end_ms,
        "source": s.source,
    }
