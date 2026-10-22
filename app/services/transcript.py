"""Transcript segments. raw_text is NEVER overwritten by AI."""

from __future__ import annotations

from app.exceptions import RawTextImmutableError
from app.realtime.echo import AI_SPEAKER
from app.models import TranscriptSegment
from app.models.enums import TranscriptSource
from app.web.extensions import db


def preferred_segments(meeting_id: str, *, include_assistant: bool = False) -> list[TranscriptSegment]:
    """Return canonical human segments, optionally interleaved assistant replies.

    A completed full-recording ASR pass is more accurate than the low-latency
    live hypotheses, so once final segments exist they become the single source
    for summaries, exports and the completed-meeting UI. Live segments remain
    stored unchanged for audit and troubleshooting.
    """
    final = (
        TranscriptSegment.query.filter_by(meeting_id=meeting_id, source=TranscriptSource.FINAL.value)
        .order_by(TranscriptSegment.start_ms.asc(), TranscriptSegment.created_at.asc())
        .all()
    )
    if not final:
        return (
        TranscriptSegment.query.filter_by(meeting_id=meeting_id)
        .order_by(TranscriptSegment.start_ms.asc(), TranscriptSegment.created_at.asc())
        .all()
        )
    if not include_assistant:
        return final
    # Full-recording ASR deliberately replaces only human realtime hypotheses.
    # The assistant's replies are persisted as realtime rows, so retain and
    # time-order them for the meeting record instead of dropping them merely
    # because a FINAL human pass exists.
    assistant = (
        TranscriptSegment.query.filter_by(meeting_id=meeting_id, speaker_label=AI_SPEAKER)
        .order_by(TranscriptSegment.start_ms.asc(), TranscriptSegment.created_at.asc())
        .all()
    )
    return sorted(
        {segment.id: segment for segment in [*final, *assistant]}.values(),
        key=lambda segment: (int(segment.start_ms or 0), segment.created_at, segment.id),
    )


class TranscriptService:
    def add_segment(
        self,
        meeting_id: str,
        raw_text: str,
        *,
        start_ms: int = 0,
        end_ms: int = 0,
        speaker_label: str = "Speaker 1",
        speaker_user_id: str | None = None,
        speaker_confidence: float = 0.0,
        asr_confidence: float = 1.0,
        source: str = TranscriptSource.REALTIME.value,
        is_overlap: bool = False,
        speaker_cluster_id: str | None = None,
        punctuated_text: str | None = None,
        asr_state: str = "final",
        asr_revision: int = 0,
        terminology_snapshot_id: str | None = None,
    ) -> TranscriptSegment:
        seg = TranscriptSegment(
            meeting_id=meeting_id,
            raw_text=raw_text,
            punctuated_text=punctuated_text,
            final_text=punctuated_text or raw_text,
            display_text=punctuated_text or raw_text,
            asr_state=asr_state,
            asr_revision=asr_revision,
            terminology_snapshot_id=terminology_snapshot_id,
            start_ms=start_ms,
            end_ms=end_ms,
            speaker_label=speaker_label,
            speaker_user_id=speaker_user_id,
            speaker_confidence=speaker_confidence,
            asr_confidence=asr_confidence,
            source=source,
            is_overlap=is_overlap,
            speaker_cluster_id=speaker_cluster_id,
        )
        db.session.add(seg)
        db.session.commit()
        return seg

    def revise_realtime_hypothesis(self, segment: TranscriptSegment, new_text: str, *, end_ms: int | None = None) -> TranscriptSegment:
        """Deprecated compatibility entry point: raw ASR is immutable after insert."""
        if (new_text or "").strip() != (segment.raw_text or "").strip():
            raise RawTextImmutableError(segment.id)
        if end_ms is not None and int(end_ms) > int(segment.end_ms or 0):
            segment.end_ms = int(end_ms)
            segment.asr_revision = int(segment.asr_revision or 0) + 1
            db.session.commit()
        return segment

    def apply_punctuation(self, segment: TranscriptSegment, punctuated_text: str) -> TranscriptSegment:
        text = (punctuated_text or "").strip() or segment.raw_text
        segment.punctuated_text = text
        segment.final_text = segment.corrected_text or segment.sensevoice_text or text
        segment.display_text = segment.final_text
        segment.asr_state = "final"
        db.session.commit()
        return segment

    def apply_correction(self, segment: TranscriptSegment, corrected_text: str) -> TranscriptSegment:
        segment.corrected_text = corrected_text
        segment.final_text = corrected_text
        segment.display_text = corrected_text
        db.session.commit()
        return segment

    def apply_sensevoice(self, segment: TranscriptSegment, text: str) -> TranscriptSegment:
        value = (text or "").strip()
        if not value:
            return segment
        segment.sensevoice_text = value
        segment.final_text = segment.corrected_text or value
        segment.display_text = segment.final_text
        db.session.commit()
        return segment

    def apply_speaker(
        self,
        segment: TranscriptSegment,
        *,
        label: str,
        user_id: str | None,
        cluster: str | None,
        confidence: float,
    ) -> TranscriptSegment:
        """Persist a late speaker result without changing transcript text."""
        segment.speaker_label = (label or "Speaker 1").strip() or "Speaker 1"
        segment.speaker_user_id = user_id
        segment.speaker_cluster_id = cluster
        segment.speaker_confidence = float(confidence or 0.0)
        db.session.commit()
        return segment

    def patch(
        self,
        segment: TranscriptSegment,
        *,
        corrected_text: str | None = None,
        speaker_label: str | None = None,
        speaker_user_id: str | None = None,
        raw_text: str | None = None,
        display_text: str | None = None,
    ) -> TranscriptSegment:
        if raw_text is not None and raw_text != segment.raw_text:
            raise RawTextImmutableError(segment.id)
        if corrected_text is not None:
            segment.corrected_text = corrected_text
            segment.final_text = corrected_text
            segment.display_text = corrected_text
        if display_text is not None and corrected_text is None:
            segment.display_text = display_text
        if speaker_label is not None:
            segment.speaker_label = speaker_label
        if speaker_user_id is not None:
            segment.speaker_user_id = speaker_user_id
        db.session.commit()
        return segment

    def restore_raw(self, segment: TranscriptSegment) -> TranscriptSegment:
        segment.corrected_text = None
        segment.final_text = segment.sensevoice_text or segment.punctuated_text or segment.raw_text
        segment.display_text = segment.final_text
        db.session.commit()
        return segment

    def list_for_meeting(self, meeting_id: str) -> list[TranscriptSegment]:
        return preferred_segments(meeting_id)
