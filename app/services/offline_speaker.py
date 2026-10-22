"""Conservative offline speaker clustering for imported recordings."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from pathlib import Path

from app.models import Meeting, SpeakerProfile, TranscriptSegment
from app.services.speaker import SpeakerIDService, cosine
from app.web.extensions import db


@dataclass
class _Cluster:
    index: int
    vector: list[float]
    count: int = 1


class OfflineSpeakerDiarizer:
    """Cluster ASR sentence windows without inventing names for uncertain audio.

    This is deliberately not advertised as overlap separation.  CAM++ produces
    useful voice embeddings for clean completed sentences; short or uncertain
    snippets inherit only a nearby established speaker, otherwise remain
    ``未知说话人``.
    """

    def __init__(self, app):
        self.app = app
        self.embedding = app.extensions["providers"].embedding
        self.min_ms = int(app.config.get("SPEAKER_ID_MIN_MS", 1500))
        self.threshold = float(app.config.get("IMPORT_SPEAKER_CLUSTER_THRESHOLD", 0.58))
        self.short_gap_ms = int(app.config.get("IMPORT_SPEAKER_SHORT_GAP_MS", 3000))
        self.max_speakers = int(app.config.get("IMPORT_MAX_SPEAKERS", 8))
        self.identifier = SpeakerIDService(
            high=float(app.config.get("SPEAKER_HIGH_CONFIDENCE", 0.68)),
            mid=float(app.config.get("SPEAKER_MID_CONFIDENCE", 0.55)),
        )

    @staticmethod
    def _window_wav(master: Path, start_ms: int, end_ms: int) -> bytes:
        """Read one ASR sentence window as a small canonical WAV byte string."""
        start_ms, end_ms = max(0, int(start_ms or 0)), max(0, int(end_ms or 0))
        if end_ms <= start_ms:
            return b""
        with wave.open(str(master), "rb") as src:
            rate = src.getframerate()
            if rate <= 0:
                return b""
            src.setpos(min(src.getnframes(), int(start_ms * rate / 1000)))
            # Very long ASR segments are not more informative after 20 seconds
            # and make CPU speaker embedding needlessly expensive.
            count = min(int((end_ms - start_ms) * rate / 1000), rate * 20)
            frames = src.readframes(max(0, count))
            if not frames:
                return b""
            out = io.BytesIO()
            with wave.open(out, "wb") as dest:
                dest.setnchannels(src.getnchannels())
                dest.setsampwidth(src.getsampwidth())
                dest.setframerate(rate)
                dest.writeframes(frames)
            return out.getvalue()

    def _vector(self, master: Path, segment: TranscriptSegment) -> list[float]:
        duration = max(0, int(segment.end_ms or 0) - int(segment.start_ms or 0))
        if duration < self.min_ms:
            return []
        try:
            payload = self._window_wav(master, segment.start_ms, segment.end_ms)
            try:
                value = self.embedding.embed_audio(payload, mime="audio/wav")
            except TypeError:  # lightweight test/fallback providers keep the base signature
                value = self.embedding.embed_audio(payload)
            return list(value or [])
        except Exception:
            self.app.logger.warning("speaker embedding skipped segment=%s", segment.id, exc_info=True)
            return []

    @staticmethod
    def _nearest(vector: list[float], clusters: list[_Cluster]) -> tuple[_Cluster | None, float]:
        best, score = None, -1.0
        for cluster in clusters:
            value = cosine(vector, cluster.vector)
            if value > score:
                best, score = cluster, value
        return best, max(score, 0.0)

    @staticmethod
    def _merge(cluster: _Cluster, vector: list[float]) -> None:
        n = min(len(cluster.vector), len(vector))
        if not n:
            return
        old_count = max(1, cluster.count)
        cluster.vector = [((cluster.vector[i] * old_count) + vector[i]) / (old_count + 1) for i in range(n)]
        cluster.count = old_count + 1

    def apply(self, meeting_id: str, master_path: str | Path) -> int:
        master = Path(master_path)
        if not master.exists():
            return 0
        meeting = db.session.get(Meeting, meeting_id)
        if not meeting:
            return 0
        segments = (
            TranscriptSegment.query.filter_by(meeting_id=meeting_id, source="final")
            .order_by(TranscriptSegment.start_ms.asc(), TranscriptSegment.created_at.asc())
            .all()
        )
        if not segments:
            return 0
        profiles = SpeakerProfile.query.all()
        clusters: list[_Cluster] = []
        vectors: dict[str, list[float]] = {}
        max_clusters = max(1, min(int(meeting.expected_speakers or 1), self.max_speakers))

        # First pass uses only complete acoustic windows to establish clusters.
        for segment in segments:
            vector = self._vector(master, segment)
            if not vector:
                continue
            vectors[segment.id] = vector
            nearest, score = self._nearest(vector, clusters)
            if nearest is not None and score >= self.threshold:
                self._merge(nearest, vector)
                segment.speaker_cluster_id = str(nearest.index)
                segment.speaker_confidence = score
            elif len(clusters) < max_clusters:
                cluster = _Cluster(index=len(clusters) + 1, vector=list(vector))
                clusters.append(cluster)
                segment.speaker_cluster_id = str(cluster.index)
                segment.speaker_confidence = 1.0
            else:
                segment.speaker_cluster_id = None
                segment.speaker_confidence = score

        # Short turns may inherit only an immediately adjacent known cluster.
        for pos, segment in enumerate(segments):
            if segment.speaker_cluster_id:
                continue
            prior = next((row for row in reversed(segments[:pos]) if row.speaker_cluster_id), None)
            later = next((row for row in segments[pos + 1 :] if row.speaker_cluster_id), None)
            prior_gap = int(segment.start_ms or 0) - int(prior.end_ms or 0) if prior else self.short_gap_ms + 1
            later_gap = int(later.start_ms or 0) - int(segment.end_ms or 0) if later else self.short_gap_ms + 1
            chosen = prior if 0 <= prior_gap <= self.short_gap_ms else later if 0 <= later_gap <= self.short_gap_ms else None
            if chosen:
                segment.speaker_cluster_id = chosen.speaker_cluster_id
                segment.speaker_confidence = min(float(chosen.speaker_confidence or 0), 0.55)

        by_index = {str(cluster.index): cluster for cluster in clusters}
        for segment in segments:
            cluster = by_index.get(segment.speaker_cluster_id or "")
            if not cluster:
                segment.speaker_label = "未知说话人"
                segment.speaker_user_id = None
                continue
            identified = self.identifier.identify(cluster.vector, profiles, cluster_index=cluster.index)
            if identified["speaker_user_id"]:
                segment.speaker_label = identified["speaker_label"]
                segment.speaker_user_id = identified["speaker_user_id"]
            else:
                segment.speaker_label = f"说话人 {cluster.index}"
                segment.speaker_user_id = None
            segment.speaker_confidence = max(float(segment.speaker_confidence or 0), float(identified["confidence"] or 0))
        db.session.commit()
        return len(segments)
