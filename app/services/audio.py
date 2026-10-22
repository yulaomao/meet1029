"""Reliable recording chunk upload. ACK then client may delete local copy.

Idempotent on (meeting_id, client_id, seq).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.models import AudioChunk, AudioUploadSession, Meeting
from app.storage.assembler import assemble_master, missing_sequences
from app.storage.audio_store import AudioStore
from app.web.extensions import db


class AudioService:
    def __init__(self, store: AudioStore):
        self.store = store

    def _session(self, meeting_id: str, client_id: str) -> AudioUploadSession:
        row = AudioUploadSession.query.filter_by(meeting_id=meeting_id, client_id=client_id).first()
        if not row:
            row = AudioUploadSession(meeting_id=meeting_id, client_id=client_id, last_seq=-1)
            db.session.add(row)
            db.session.flush()
        return row

    def ingest_chunk(
        self,
        meeting_id: str,
        client_id: str,
        seq: int,
        data: bytes,
        *,
        start_ms: int = 0,
        mime: str = "audio/wav",
        duration_ms: int = 5000,
    ) -> dict:
        existing = AudioChunk.query.filter_by(
            meeting_id=meeting_id, client_id=client_id, seq=seq
        ).first()
        if existing:
            return {"ack": True, "seq": seq, "idempotent": True, "path": existing.path}

        ext = "wav"
        if "webm" in (mime or ""):
            ext = "webm"
        elif "mp4" in (mime or "") or "m4a" in (mime or ""):
            ext = "m4a"
        elif "ogg" in (mime or "") or "opus" in (mime or ""):
            ext = "ogg"
        dest = self.store.chunk_path(meeting_id, seq, ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        row = AudioChunk(
            meeting_id=meeting_id,
            client_id=client_id,
            seq=seq,
            start_ms=start_ms,
            duration_ms=duration_ms,
            mime=mime,
            sha256=digest,
            path=str(dest),
            size_bytes=len(data),
            acked=True,
        )
        db.session.add(row)
        sess = self._session(meeting_id, client_id)
        sess.last_seq = max(sess.last_seq, seq)
        db.session.commit()
        return {"ack": True, "seq": seq, "idempotent": False, "path": str(dest)}

    def status(self, meeting_id: str, client_id: str | None = None) -> dict:
        q = AudioChunk.query.filter_by(meeting_id=meeting_id)
        if client_id:
            q = q.filter_by(client_id=client_id)
        rows = q.order_by(AudioChunk.seq.asc()).all()
        seqs = [r.seq for r in rows]
        return {
            "received": seqs,
            "count": len(seqs),
            "missing": missing_sequences(seqs),
            "acked": [r.seq for r in rows if r.acked],
        }

    def remaining(self, meeting_id: str) -> int:
        info = self.status(meeting_id)
        return len(info["missing"])

    def complete(self, meeting_id: str, client_id: str, force: bool = False) -> dict:
        sess = self._session(meeting_id, client_id)
        info = self.status(meeting_id, client_id)
        if info["missing"] and not force:
            return {"complete": False, "missing": info["missing"], "ack": False}
        sess.completed = True
        sess.force_complete = bool(force)
        db.session.commit()
        paths = [
            Path(r.path)
            for r in AudioChunk.query.filter_by(meeting_id=meeting_id)
            .order_by(AudioChunk.seq.asc())
            .all()
        ]
        master = assemble_master(self.store, meeting_id, paths)
        meeting = db.session.get(Meeting, meeting_id)
        self.store.write_json(
            meeting_id,
            "metadata.json",
            {"meeting_id": meeting_id, "chunks": info["received"], "master": str(master)},
        )
        return {"complete": True, "master": str(master), "ack": True, "status": meeting.status if meeting else None}
