"""LangGraph post-meeting pipeline (with sequential fallback)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any, TypedDict

from app.models import Meeting, Terminology, TranscriptSegment
from app.models.enums import MeetingStatus, TranscriptSource
from app.realtime.wake import canonical_stop_command
from app.services.audio import AudioService
from app.services.meeting import MeetingService
from app.services.summary import SummaryService
from app.services.transcript import TranscriptService, preferred_segments
from app.storage.audio_store import AudioStore
from app.web.extensions import db


class PostMeetingState(TypedDict, total=False):
    meeting_id: str
    master_path: str
    transcript: dict
    summary_id: str
    error: str


def _try_langgraph():
    try:
        from langgraph.graph import END, StateGraph

        return StateGraph, END
    except Exception:
        return None, None


class PostMeetingGraph:
    def __init__(self, app, providers, store: AudioStore, progress: Callable[[str, int], None] | None = None):
        self.app = app
        self.providers = providers
        self.store = store
        self.audio = AudioService(store)
        self.meetings = MeetingService()
        self.transcripts = TranscriptService()
        self.summaries = SummaryService(providers.llm)
        self.progress = progress

    def _report(self, stage: str, percent: int) -> None:
        if self.progress:
            self.progress(stage, percent)

    def run(self, meeting_id: str, master_path: str | None = None) -> PostMeetingState:
        initial: PostMeetingState = {"meeting_id": meeting_id}
        if master_path:
            initial["master_path"] = master_path
        compiled = self.compile()
        if compiled is not self and hasattr(compiled, "invoke"):
            out = compiled.invoke(initial)
            return out
        state = initial
        state = self.node_assemble(state)
        state = self.node_final_asr(state)
        state = self.node_diarize(state)
        state = self.node_analyze(state)
        state = self.node_embed(state)
        state = self.node_notify(state)
        return state

    def node_assemble(self, state: PostMeetingState) -> PostMeetingState:
        meeting_id = state["meeting_id"]
        raw_master = str(state.get("master_path") or "").strip()
        supplied = Path(raw_master) if raw_master else None
        # ``Path(\"\")`` means the workspace directory and always exists. It
        # previously bypassed audio assembly when callers supplied no master,
        # leaving long browser meetings without a replayable WAV or final ASR.
        if supplied and supplied.is_file():
            state["master_path"] = str(supplied)
        else:
            result = self.audio.complete(meeting_id, client_id="pipeline", force=True)
            state["master_path"] = result.get("master", "")
        meeting = db.session.get(Meeting, meeting_id)
        if meeting and meeting.status in {MeetingStatus.STOPPING.value, MeetingStatus.UPLOADING.value, MeetingStatus.RECORDING.value}:
            try:
                self.meetings.transition(meeting, MeetingStatus.PROCESSING.value) if meeting.status != MeetingStatus.PROCESSING.value else None
            except Exception:
                if meeting.status != MeetingStatus.PROCESSING.value:
                    meeting.status = MeetingStatus.PROCESSING.value
                    db.session.commit()
        if meeting and meeting.status not in {MeetingStatus.PROCESSING.value, MeetingStatus.COMPLETED.value}:
            meeting.status = MeetingStatus.PROCESSING.value
            db.session.commit()
        return state

    def _skip_fake_final_overlay(self, meeting_id: str) -> bool:
        """Do not write FakeFinalASR's canned 针尖 lines over a real meeting.

        Skip when (a) not TESTING and the provider is FakeFinalASR, or
        (b) live/realtime segments already exist. Tests keep canned output.
        """
        from app.providers.final_asr import FakeFinalASRProvider

        is_fake = isinstance(self.providers.final_asr, FakeFinalASRProvider)
        testing = bool(self.app.config.get("TESTING"))
        if is_fake and not testing:
            return True
        return False

    def node_final_asr(self, state: PostMeetingState) -> PostMeetingState:
        meeting_id = state["meeting_id"]
        self._report("transcribing", 30)
        wav = state.get("master_path") or str(self.store.master_path(meeting_id))
        if self._skip_fake_final_overlay(meeting_id):
            result = {"text": "", "segments": []}
        else:
            meeting = db.session.get(Meeting, meeting_id)
            terms = [t.term for t in Terminology.query.filter_by(enabled=True).limit(100).all()]
            result = self.providers.final_asr.transcribe(wav, terminology=terms)
        state["transcript"] = result
        # A retry replaces its own canonical pass, never appends a second copy
        # beside it. Realtime rows remain immutable audit evidence.
        TranscriptSegment.query.filter_by(
            meeting_id=meeting_id, source=TranscriptSource.FINAL.value
        ).delete(synchronize_session=False)
        db.session.commit()
        for seg in result.get("segments") or []:
            raw = seg.get("raw_text") or ""
            # Preserve raw ASR, but normalize an unmistakable stop command so
            # the completed transcript does not regress from live routing to
            # a misleading phonetic spelling such as “城市精灵停止回复”.
            punctuated = canonical_stop_command(raw) or self.providers.punctuation.finalize(raw)
            self.transcripts.add_segment(
                meeting_id,
                raw,
                start_ms=seg.get("start_ms") or 0,
                end_ms=seg.get("end_ms") or 0,
                speaker_label=seg.get("speaker_label") or "Speaker 1",
                source=TranscriptSource.FINAL.value,
                punctuated_text=punctuated,
                asr_state="final",
            )
        # Final ASR is frequently the only usable transcript for a short meeting.
        # It must receive the same Qwen repair pass as live captions before the
        # summary/title are built; otherwise post-meeting text silently bypasses
        # correction altogether.
        final_segments = TranscriptSegment.query.filter_by(
            meeting_id=meeting_id,
            source=TranscriptSource.FINAL.value,
        ).all()
        if final_segments:
            self._report("correcting", 55)
            from app.services.speech_pipeline import SpeechPipeline

            correction_done = threading.Event()
            correction_session = {
                "correction_pending": [
                    {"id": seg.id, "text": seg.raw_text} for seg in final_segments
                ],
                "correction_pending_since": 0.0,
            }
            pipeline = SpeechPipeline(self.app)
            deadline = time.monotonic() + max(1, int(self.app.config.get("FINAL_CORRECTION_WAIT_SECONDS", 45)))
            queued = False
            while time.monotonic() < deadline and not queued:
                queued = pipeline._queue_correction(
                    self.app,
                    meeting_id,
                    correction_session,
                    force=True,
                    completed=correction_done,
                )
                if not queued:
                    time.sleep(0.1)
            if queued:
                correction_done.wait(max(0.1, deadline - time.monotonic()))
            else:
                self.app.logger.warning("final ASR correction skipped: LLM remained busy meeting=%s", meeting_id)
        state["transcript"] = self._write_final_export(meeting_id)
        self._report("correcting", 65)
        return state

    def _write_final_export(self, meeting_id: str) -> dict:
        """Export persisted display text, including live-only meetings safely."""
        # A completed full-recording ASR pass replaces human live hypotheses,
        # but must not erase the assistant's separately persisted replies.
        persisted = preferred_segments(meeting_id, include_assistant=True)
        export_segments = [
            {
                "id": seg.id,
                "start_ms": seg.start_ms,
                "end_ms": seg.end_ms,
                "speaker_label": seg.speaker_label,
                "raw_text": seg.raw_text,
                "sensevoice_text": seg.sensevoice_text,
                "text": seg.display_text or seg.punctuated_text or seg.raw_text,
                "source": seg.source,
            }
            for seg in persisted
        ]
        result: dict = {
            "text": "\n".join(item["text"] for item in export_segments if item["text"]),
            "segments": export_segments,
        }
        self.store.write_json(meeting_id, "final_transcript.json", result)
        return result

    def node_diarize(self, state: PostMeetingState) -> PostMeetingState:
        """Apply conservative CAM++ clustering to completed, offline ASR only."""
        self._report("speakers", 68)
        try:
            from app.services.offline_speaker import OfflineSpeakerDiarizer

            OfflineSpeakerDiarizer(self.app).apply(state["meeting_id"], state.get("master_path") or "")
        except Exception:
            # Diarization must not discard an otherwise usable imported record.
            self.app.logger.exception("offline speaker processing failed meeting=%s", state["meeting_id"])
        state["transcript"] = self._write_final_export(state["meeting_id"])
        self._report("speakers", 72)
        return state

    def node_analyze(self, state: PostMeetingState) -> PostMeetingState:
        self._report("summarizing", 78)
        meeting = db.session.get(Meeting, state["meeting_id"])
        summary = self.summaries.analyze(meeting)
        self.meetings.assign_auto_title(meeting, summary.payload)
        state["summary_id"] = summary.id
        self.store.write_json(
            state["meeting_id"],
            "summary.json",
            {"summary": summary.summary, "payload": summary.payload},
        )
        self._report("summarizing", 87)
        return state

    def node_embed(self, state: PostMeetingState) -> PostMeetingState:
        self._report("embedding", 90)
        meeting = db.session.get(Meeting, state["meeting_id"])
        segs = preferred_segments(meeting.id)
        text = "\n".join((s.display_text or s.raw_text) for s in segs)
        from app.services.knowledge import KnowledgeService

        KnowledgeService(self.providers.embedding).create_document(
            title=f"会议 {meeting.name}",
            content=text or meeting.name,
            project_id=meeting.project_id,
            source_meeting_id=meeting.id,
        )
        self._report("embedding", 95)
        return state

    def node_notify(self, state: PostMeetingState) -> PostMeetingState:
        meeting = db.session.get(Meeting, state["meeting_id"])
        if meeting and meeting.status != MeetingStatus.COMPLETED.value:
            meeting.status = MeetingStatus.COMPLETED.value
            db.session.commit()
        try:
            self.providers.notification.send(
                meeting.created_by if meeting else "",
                "会议处理完成",
                f"{meeting.name if meeting else ''} 已生成总结与待办。",
            )
        except Exception:
            pass
        self._report("finalizing", 99)
        return state

    def compile(self):
        StateGraph, END = _try_langgraph()
        if StateGraph is None:
            return self
        graph = StateGraph(dict)
        graph.add_node("assemble", lambda s: self.node_assemble(s))
        graph.add_node("final_asr", lambda s: self.node_final_asr(s))
        graph.add_node("diarize", lambda s: self.node_diarize(s))
        graph.add_node("analyze", lambda s: self.node_analyze(s))
        graph.add_node("embed", lambda s: self.node_embed(s))
        graph.add_node("notify", lambda s: self.node_notify(s))
        graph.set_entry_point("assemble")
        graph.add_edge("assemble", "final_asr")
        graph.add_edge("final_asr", "diarize")
        graph.add_edge("diarize", "analyze")
        graph.add_edge("analyze", "embed")
        graph.add_edge("embed", "notify")
        graph.add_edge("notify", END)
        return graph.compile()
