"""Speech worker pipeline: PCM spool first, AudioChunk fallback, no schema migration.

Cursor is stored at data/meetings/<id>/speech_cursor.json via AudioStore.
ASR / diarization / LLM run here — not in the Flask web process.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from app.graphs.ai_turn import AITurnGraph
from app.logging import get_logger
from app.media import resolve_ffmpeg
from app.models import AudioChunk, Meeting, SpeakerProfile, Terminology, TranscriptSegment
from app.models.enums import MeetingStatus, TranscriptSource
from app.providers.asr import FakeASRProvider
from app.providers.diarization import FakeDiarizationProvider
from app.providers.embedding import CampPlusSpeakerEmbeddingProvider
from app.realtime.dialogue import DialogueTurnPolicy
from app.realtime.echo import AI_SPEAKER, EchoFilter
from app.realtime.smart_turn import get_smart_turn
from app.realtime.wake import (
    WakeDetector,
    canonical_stop_command,
    correct_asr_text,
)
from app.realtime.framing import (
    BYTES_PER_FRAME,
    FRAME_MS,
    PCM_SAMPLE_RATE,
)
from app.services.meeting_state import MeetingStateService
from app.services.speaker import SpeakerIDService, StickySpeakerAssigner
from app.services.summary import attributed_transcript, parse_json_payload, validate_speaker_views
from app.services.transcript import TranscriptService
from app.web.socket_events import NS, transcript_final_payload

log = get_logger("speech-worker")

# Live wake must own the only serial LLM slot; rolling summary yields.
_wake_inflight = threading.Event()
_llm_lock = threading.Lock()
# A meeting can have only one audible assistant response at a time.  This is
# deliberately separate from the Qwen text lock: the reference pipeline lets
# text generation continue while its TTS stage consumes already-produced text.
_tts_lock = threading.Lock()

CURSOR_NAME = "speech_cursor.json"
PCM_NAME = "realtime.pcm"
AI_CONTROL_NAME = "ai_control.json"
ACTIVE_STATUSES = (
    MeetingStatus.RECORDING.value,
    MeetingStatus.INTERRUPTED.value,
    MeetingStatus.STOPPING.value,
)
SUMMARY_INTERVAL_S = 60.0
_TTS_SENTENCE_END = re.compile(r"[。！？!?；;]\s*")
_CORRECTION_PUNCT = re.compile(r"[\s，。！？；：、,.!?;:]+")
_NUMERIC_FACT = re.compile(r"(?<![A-Za-z])\d+(?:\.\d+)?")
_TERMINAL_PUNCT_RUN = re.compile(r"[。！？!?]+")
_STUTTER_SINGLE_CHARS = frozenset("我你他她它这那的是了和与在就都也")
def _default_cursor() -> dict[str, Any]:
    return {
        "pcm_offset": 0,
        "last_seq": -1,
        "last_summary_at": 0,
        "asr_queue_lag_ms": 0,
        "asr_processing_ms": 0,
        "asr_metrics_at": 0,
    }


def load_cursor(store, meeting_id: str) -> dict[str, Any]:
    data = store.read_json(meeting_id, CURSOR_NAME) or {}
    cur = _default_cursor()
    if isinstance(data, dict):
        for key in cur:
            if key in data:
                cur[key] = data[key]
    return cur


def save_cursor(store, meeting_id: str, cursor: dict[str, Any]) -> None:
    store.write_json(meeting_id, CURSOR_NAME, cursor)


def is_loopback_addr(addr: str | None) -> bool:
    raw = (addr or "").strip()
    if not raw:
        return False
    if raw.startswith("::ffff:"):
        raw = raw[7:]
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return raw in {"localhost", "127.0.0.1", "::1"}


def broadcast_live(app, event: str, meeting_id: str, payload: dict) -> None:
    """Push an event to browsers in the web process.

    Flask-SocketIO rooms live in the web process. Tests share that process so
    we emit in-process. The worker POSTs to localhost /api/internal/live-broadcast.
    """
    if app.config.get("TESTING"):
        try:
            from app.web.extensions import socketio

            socketio.emit(event, payload, namespace=NS, to=meeting_id)
        except Exception:
            log.debug("in-process emit skipped event=%s", event)
        return
    host = app.config.get("INTERNAL_LIVE_HOST") or "127.0.0.1"
    port = int(app.config.get("INTERNAL_LIVE_PORT") or 5000)
    scheme = str(app.config.get("INTERNAL_LIVE_SCHEME") or "http").lower()
    if scheme not in {"http", "https"}:
        log.warning("invalid INTERNAL_LIVE_SCHEME=%s; using http", scheme)
        scheme = "http"
    url = f"{scheme}://{host}:{port}/api/internal/live-broadcast"
    try:
        import httpx

        ca_file = str(app.config.get("INTERNAL_LIVE_CA_FILE") or "").strip()
        verify: bool | str = ca_file if scheme == "https" and ca_file else True
        resp = httpx.post(
            url,
            json={"event": event, "meeting_id": meeting_id, "payload": payload},
            timeout=2.0,
            trust_env=False,
            verify=verify,
        )
        if resp.status_code >= 400:
            log.warning(
                "live-broadcast failed event=%s meeting=%s status=%s",
                event,
                meeting_id,
                resp.status_code,
            )
    except Exception:
        log.warning("live-broadcast failed event=%s meeting=%s", event, meeting_id)


class SpeechPipeline:
    def __init__(self, app=None):
        self.app = app

    def _skip_live_fake_asr(self, app, asr) -> bool:
        """Do not dump FakeASR's canned script into a real live meeting."""
        if app.config.get("TESTING"):
            return False
        return isinstance(asr, FakeASRProvider)

    def _uses_server_vad(self, asr) -> bool:
        """FunASR endpointing is controlled by local VAD, never a remote server."""
        return False

    def _drain_asr(
        self,
        app,
        meeting_id: str,
        session: dict,
        pcm: bytes,
        *,
        start_ms: int,
        end_ms: int,
    ) -> None:
        asr = session["asr"]
        punctuator = session.setdefault("punctuation", app.extensions["providers"].punctuation)
        stable_getter = getattr(asr, "get_stable_prefix", asr.get_partial)
        stable = (stable_getter() or "").strip()
        if stable:
            display = punctuator.online(stable)
            previous_display = str(session.get("last_stable_display") or "")
            previous_key = self._compact_spoken_text(previous_display)
            display_key = self._compact_spoken_text(display)
            stable_key = self._compact_spoken_text(stable)
            if previous_key and display_key and not display_key.startswith(previous_key):
                # Some online punctuation checkpoints return only the newest
                # delta even though the UI replaces the whole live row. The
                # ASR stable prefix is cumulative, so prefer it when the
                # punctuated rendering would visibly erase already stable text.
                if stable_key.startswith(previous_key):
                    display = stable
                else:
                    session["discarded_regressive_partial_count"] = int(
                        session.get("discarded_regressive_partial_count") or 0
                    ) + 1
                    log.warning(
                        "discarding regressive live partial meeting=%s utterance=%s",
                        meeting_id,
                        session.get("utterance_id"),
                    )
                    display = ""
            if display and display != session.get("last_stable_display"):
                session["last_stable_display"] = display
                session["event_sequence"] = int(session.get("event_sequence") or 0) + 1
                broadcast_live(
                    app,
                    "transcript_partial",
                    meeting_id,
                    {
                        "meeting_id": meeting_id,
                        "text": display,
                        "state": "stable",
                        "utterance_id": int(session.get("utterance_id") or 0),
                        "turn_revision": int(session.get("turn_revision") or 0),
                        "sequence": int(session["event_sequence"]),
                    },
                )
        while True:
            final = asr.get_final()
            if not final:
                break
            self._on_final(app, meeting_id, final, pcm, session, start_ms=start_ms, end_ms=end_ms)

    @staticmethod
    def _publish_asr_metrics(
        app,
        meeting_id: str,
        session: dict,
        cursor: dict,
        *,
        queue_lag_ms: int,
        processing_ms: int,
    ) -> None:
        """Expose real worker backlog rather than leaving the UI's ASR lag static.

        The spool's unread PCM is the measurable end-to-end capture-to-worker
        delay.  It remains meaningful even while the recognizer has not yet
        emitted a partial sentence, unlike a timestamp attached only to text.
        """
        queue_lag_ms = max(0, int(queue_lag_ms))
        processing_ms = max(0, int(processing_ms))
        now = time.monotonic()
        prior_at = float(session.get("last_asr_metrics_emit_at") or 0)
        prior_lag = int(session.get("last_asr_queue_lag_ms") or -1)
        # One update per half-second is responsive in the header without
        # flooding Socket.IO alongside 32 ms audio/TTS packets.
        if now - prior_at < 0.5 and abs(queue_lag_ms - prior_lag) < 100:
            return
        session["last_asr_metrics_emit_at"] = now
        session["last_asr_queue_lag_ms"] = queue_lag_ms
        cursor["asr_queue_lag_ms"] = queue_lag_ms
        cursor["asr_processing_ms"] = processing_ms
        cursor["asr_metrics_at"] = time.time()
        broadcast_live(
            app,
            "asr_metrics",
            meeting_id,
            {"queue_lag_ms": queue_lag_ms, "processing_ms": processing_ms},
        )

    def _session(self, app, meeting_id: str) -> dict:
        bag = app.extensions.setdefault("speech_sessions", {})
        if meeting_id in bag:
            return bag[meeting_id]
        providers = app.extensions["providers"]
        configured_asr_hotwords: list[str] = []
        meeting_terminology_hotwords: list[str] = []
        if providers.speech_backend == "fake" or isinstance(providers.asr, FakeASRProvider):
            script = list(getattr(providers.asr, "script", None) or FakeASRProvider.DEFAULT_SCRIPT)
            asr = FakeASRProvider(script=script)
            try:
                dia = type(providers.diarization)()
            except Exception:
                dia = FakeDiarizationProvider()
        else:
            new_asr_session = getattr(providers.asr, "new_session", None)
            asr = new_asr_session() if callable(new_asr_session) else providers.asr
            if hasattr(asr, "hotwords"):
                configured_asr_hotwords = str(getattr(asr, "hotwords", "") or "").split()
                meeting_terminology_hotwords = self._meeting_terminology_hotwords(meeting_id)
                asr.hotwords = " ".join(
                    dict.fromkeys([*configured_asr_hotwords, *meeting_terminology_hotwords])
                )
                if meeting_terminology_hotwords:
                    log.info(
                        "loaded meeting ASR terminology meeting=%s count=%s",
                        meeting_id,
                        len(meeting_terminology_hotwords),
                    )
            new_diarization_session = getattr(providers.diarization, "new_session", None)
            if callable(new_diarization_session):
                dia = new_diarization_session()
            else:
                try:
                    dia = type(providers.diarization)()
                except Exception:
                    dia = providers.diarization
        new_punctuation_session = getattr(providers.punctuation, "new_session", None)
        punctuation = (
            new_punctuation_session()
            if callable(new_punctuation_session)
            else type(providers.punctuation)()
            if providers.speech_backend == "fake"
            else providers.punctuation
        )
        bag[meeting_id] = {
            "asr": asr,
            "configured_asr_hotwords": configured_asr_hotwords,
            "meeting_terminology_hotwords": meeting_terminology_hotwords,
            "diarization": dia,
            "punctuation": punctuation,
            "echo": EchoFilter(),
            "speech_active": False,
            "silence_ms": 0,
            "utterance_start_ms": 0,
            "utterance_ms": 0,
            "utterance_id": 0,
            "turn_revision": 0,
            "speaker_executor": None,
            "speaker_label_snapshot": {
                "label": "Speaker 1",
                "user_id": None,
                "cluster": "0",
                "confidence": 0.0,
            },
            "event_sequence": 0,
            "pending_endpoint": None,
            # One turn-local cancellation token. It is set after sustained VAD
            # activity while AI is talking, and is also used by the Stop button.
            "ai_cancel": threading.Event(),
            "ai_turn_active": False,
            "ai_turn_id": 0,
            "ai_generation": 0,
            "barge_speech_ms": 0,
            "barge_pending": False,
            # A wake enters an explicit hands-free dialogue.  Barge-in cancels
            # only the current audio, not this state; a clear spoken stop or
            # the Stop button ends it.
            "dialogue_active": False,
            "dialogue_last_activity": 0.0,
            "dialogue_last_assistant_at": 0.0,
            "dialogue_phase": "idle",
        }
        return bag[meeting_id]

    @staticmethod
    def _meeting_terminology_hotwords(meeting_id: str) -> list[str]:
        """Return enabled global and project terms for a meeting's ASR session.

        Resolve the vocabulary once when the stream session is created. Terms
        from other projects are excluded so unrelated vocabulary cannot bias
        recognition. The current utterance keeps a consistent hotword set.
        """
        from sqlalchemy import or_

        from app.web.extensions import db

        try:
            meeting = db.session.get(Meeting, meeting_id)
            if meeting is None:
                return []
            query = Terminology.query.filter(Terminology.enabled.is_(True))
            if meeting.project_id:
                query = query.filter(
                    or_(
                        Terminology.project_id.is_(None),
                        Terminology.project_id == meeting.project_id,
                    )
                )
            else:
                query = query.filter(Terminology.project_id.is_(None))
            rows = query.order_by(Terminology.priority.desc(), Terminology.term.asc()).all()
            words: list[str] = []
            seen: set[str] = set()
            for row in rows:
                for value in [row.term, *(row.aliases or [])]:
                    word = str(value or "").strip()
                    if word and word not in seen:
                        seen.add(word)
                        words.append(word)
            return words
        except Exception:
            db.session.rollback()
            log.warning(
                "meeting ASR terminology could not be loaded meeting=%s; continuing without it",
                meeting_id,
                exc_info=True,
            )
            return []

    def _refresh_meeting_asr_hotwords(self, meeting_id: str, session: dict) -> None:
        """Apply vocabulary changes before a new utterance, never mid-utterance."""
        asr = session.get("asr")
        if not hasattr(asr, "hotwords"):
            return
        words = self._meeting_terminology_hotwords(meeting_id)
        if words == session.get("meeting_terminology_hotwords", []):
            return
        configured = session.get("configured_asr_hotwords", [])
        asr.hotwords = " ".join(dict.fromkeys([*configured, *words]))
        session["meeting_terminology_hotwords"] = words
        log.info("refreshed meeting ASR terminology meeting=%s count=%s", meeting_id, len(words))

    def _pcm_for_chunk(self, app, chunk: AudioChunk) -> bytes | None:
        providers = app.extensions["providers"]
        fake = providers.speech_backend == "fake" or isinstance(providers.asr, FakeASRProvider)
        if fake:
            # Fake ASR must work without ffmpeg / webm decode.
            return b"\x00" * (BYTES_PER_FRAME * 3)
        # Reliability fallback when a browser cannot keep the AudioWorklet socket
        # alive: decode its ACKed MediaRecorder chunk entirely on this machine.
        path = app.extensions["audio_store"].meeting_dir(chunk.meeting_id) / "source"
        source = Path(chunk.path).resolve()
        if not source.exists() or path.resolve() not in source.parents:
            log.warning("refusing chunk outside meeting storage meeting=%s seq=%s", chunk.meeting_id, chunk.seq)
            return None
        ffmpeg = resolve_ffmpeg()
        if not ffmpeg:
            log.warning("ffmpeg unavailable; cannot decode fallback chunk meeting=%s seq=%s", chunk.meeting_id, chunk.seq)
            return None
        try:
            proc = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source), "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                capture_output=True,
                check=False,
                timeout=45,
            )
        except (OSError, subprocess.TimeoutExpired):
            log.warning("chunk decode failed meeting=%s seq=%s", chunk.meeting_id, chunk.seq, exc_info=True)
            return None
        if proc.returncode or not proc.stdout:
            log.warning("chunk decode returned no PCM meeting=%s seq=%s", chunk.meeting_id, chunk.seq)
            return None
        return bytes(proc.stdout)

    def _label_speaker(
        self,
        app,
        pcm: bytes,
        speakers: list[dict],
        session: dict,
        *,
        duration_ms: int = 0,
        context_critical: bool = False,
    ) -> tuple[str, str | None, str | None, float]:
        human_slots = [item for item in speakers if item.get("speaker") != AI_SPEAKER]
        if len(human_slots) > 1:
            # A mono mixed frame cannot be truthfully attributed to a profile.
            # Preserve the transcript and surface the uncertainty instead of
            # letting sticky identity turn simultaneous speakers into one name.
            return "重叠发言", None, "overlap", 0.0
        suggested = speakers[0]["speaker"] if speakers else "Speaker 1"
        if suggested == AI_SPEAKER:
            suggested = "Speaker 1"
        suggested_cluster = str(speakers[0].get("cluster_id")) if speakers else "0"
        assigner = session.get("speaker_assigner")
        if assigner is None:
            assigner = StickySpeakerAssigner(
                match_threshold=float(app.config.get("SPEAKER_CLUSTER_MATCH_THRESHOLD", 0.45))
            )
            session["speaker_assigner"] = assigner
        providers = app.extensions["providers"]
        min_ms = int(app.config.get("SPEAKER_ID_MIN_MS", 1500))
        if context_critical:
            # The wake/follow-up sentence is passed straight into Qwen's
            # attributed context.  Spend the small CAM++ cost now instead of
            # allowing an unverified default Speaker N into that prompt.
            min_ms = min(min_ms, int(app.config.get("AI_CONTEXT_SPEAKER_ID_MIN_MS", 700)))
        emb: list[float] = []
        # The completed VAD utterance is the only safe real-time boundary for
        # CPU CAM++ inference.  Its vector drives anonymous clustering first;
        # enrolled-profile matching then merely replaces a confident label.
        if int(duration_ms or 0) >= min_ms:
            try:
                emb = providers.embedding.embed_audio(pcm)
            except Exception:
                log.debug("speaker embedding skipped", exc_info=True)
        assigned = assigner.assign(
            duration_ms=duration_ms,
            suggested_label=suggested,
            suggested_cluster=suggested_cluster,
            embedding=emb,
        )
        label = assigned["speaker_label"]
        cluster = assigned["cluster_id"]
        user_id = None
        conf = float(assigned.get("confidence") or 0.0)
        cluster_key = str(cluster or "0")
        identities = session.setdefault("speaker_identities", {})
        cached_identity = identities.get(cluster_key)
        try:
            profiles = SpeakerProfile.query.limit(64).all()
            if not profiles or not emb:
                # Very short utterances are deliberately not embedded.  Once a
                # cluster has been confirmed by longer speech, retain that
                # identity for these fragments instead of flipping to Speaker 1.
                if cached_identity:
                    return (
                        cached_identity["label"],
                        cached_identity["user_id"],
                        cluster,
                        float(cached_identity["confidence"]),
                    )
                if context_critical and user_id is None and label.startswith("Speaker "):
                    return "未知说话人", None, cluster, 0.0
                return label, user_id, cluster, conf
            # Ignore old placeholder vectors after the local CAM++ provider is
            # enabled. Comparing different dimensions would fabricate a score.
            profiles = [p for p in profiles if p.embedding and len(p.embedding) == len(emb)]
            if not emb or not profiles:
                if cached_identity:
                    return (
                        cached_identity["label"],
                        cached_identity["user_id"],
                        cluster,
                        float(cached_identity["confidence"]),
                    )
                if context_critical and user_id is None and label.startswith("Speaker "):
                    return "未知说话人", None, cluster, 0.0
                return label, user_id, cluster, conf
            idx = 1
            if cluster is not None:
                try:
                    idx = int(cluster) + 1
                except (TypeError, ValueError):
                    idx = 1
            ident = SpeakerIDService(
                high=float(app.config.get("SPEAKER_HIGH_CONFIDENCE", 0.75)),
                mid=float(app.config.get("SPEAKER_MID_CONFIDENCE", 0.55)),
            ).identify(emb, profiles, cluster_index=idx)
            if ident.get("speaker_user_id"):
                label = ident["speaker_label"]
                user_id = ident["speaker_user_id"]
                conf = float(ident.get("confidence") or 0.0)
                identities[cluster_key] = {
                    "label": label,
                    "user_id": user_id,
                    "confidence": conf,
                }
                assigner.last_label = label
            elif (
                cached_identity
                and ident.get("candidate") == cached_identity.get("label")
                and float(ident.get("confidence") or 0.0)
                >= float(app.config.get("SPEAKER_MID_CONFIDENCE", 0.55))
            ):
                # A second, moderate score for the same profile supports the
                # already-confirmed cluster identity without lowering the
                # global threshold for unrelated speakers.
                label = cached_identity["label"]
                user_id = cached_identity["user_id"]
                conf = float(ident.get("confidence") or cached_identity["confidence"])
        except Exception:
            log.debug("speaker id skipped", exc_info=True)
        if context_critical and user_id is None and label.startswith("Speaker "):
            # Never turn a lack of evidence into “Speaker 1” merely because
            # this is the first sentence in a meeting.  Unknown is honest and
            # prevents the assistant from attributing the request to a person.
            return "未知说话人", None, cluster, 0.0
        return label, user_id, cluster, conf

    def _queue_speaker_label_refinement(
        self,
        app,
        meeting_id: str,
        segment_id: str,
        pcm: bytes,
        speakers: list[dict],
        session: dict,
        *,
        duration_ms: int,
    ) -> None:
        """Run ordered CAM++ labeling after the final caption is already visible."""
        executor = session.get("speaker_executor")
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"speaker-{meeting_id[:8]}")
            session["speaker_executor"] = executor

        def _run() -> None:
            try:
                with app.app_context():
                    segment = TranscriptSegment.query.filter_by(id=segment_id, meeting_id=meeting_id).first()
                    if segment is None:
                        return
                    label, user_id, cluster, confidence = self._label_speaker(
                        app,
                        pcm,
                        speakers,
                        session,
                        duration_ms=duration_ms,
                        context_critical=False,
                    )
                    session["speaker_label_snapshot"] = {
                        "label": label,
                        "user_id": user_id,
                        "cluster": cluster,
                        "confidence": confidence,
                    }
                    TranscriptService().apply_speaker(
                        segment,
                        label=label,
                        user_id=user_id,
                        cluster=cluster,
                        confidence=confidence,
                    )
                    self._backfill_cluster_identity(
                        app,
                        meeting_id,
                        cluster,
                        label=label,
                        user_id=user_id,
                        confidence=confidence,
                    )
                    from app.web.extensions import db

                    db.session.refresh(segment)
                    payload = transcript_final_payload(
                        segment,
                        text=segment.display_text or segment.raw_text,
                        speaker=segment.speaker_label,
                    )
                    payload["speaker_update"] = True
                    broadcast_live(app, "transcript_final", meeting_id, payload)
            except Exception:
                log.warning(
                    "asynchronous speaker labeling failed meeting=%s segment=%s",
                    meeting_id,
                    segment_id,
                    exc_info=True,
                )

        executor.submit(_run)

    @staticmethod
    def _backfill_cluster_identity(
        app,
        meeting_id: str,
        cluster: str | None,
        *,
        label: str,
        user_id: str | None,
        confidence: float,
    ) -> None:
        """Rename earlier anonymous turns after this cluster is confirmed.

        The first short/noisy turn can precede the first high-confidence vector.
        Keep immutable ASR untouched, but update only anonymous rows in the same
        local cluster so a one-speaker meeting does not begin with Speaker 1 and
        then switch names midway through.
        """
        if not user_id or cluster is None:
            return
        pending = (
            TranscriptSegment.query.filter_by(
                meeting_id=meeting_id,
                speaker_cluster_id=str(cluster),
                speaker_user_id=None,
            )
            .filter(TranscriptSegment.speaker_label.like("Speaker %"))
            .all()
        )
        for prior in pending:
            TranscriptService().patch(
                prior,
                speaker_label=label,
                speaker_user_id=user_id,
            )
            prior.speaker_confidence = max(float(prior.speaker_confidence or 0.0), float(confidence))
            from app.web.extensions import db

            db.session.commit()
            broadcast_live(
                app,
                "transcript_final",
                meeting_id,
                transcript_final_payload(prior, text=prior.display_text or prior.raw_text, speaker=label),
            )

    @staticmethod
    def _collapse_stutter(s: str) -> str:
        out: list[str] = []
        for ch in s or "":
            if out and ch == out[-1] and "\u4e00" <= ch <= "\u9fff":
                continue
            out.append(ch)
        t = "".join(out)
        for n in (3, 2):
            changed = True
            while changed:
                changed = False
                i = 0
                buf: list[str] = []
                while i < len(t):
                    nxt = t[i : i + n]
                    if (
                        i + 2 * n <= len(t)
                        and nxt
                        and nxt == t[i + n : i + 2 * n]
                        and all("\u4e00" <= c <= "\u9fff" for c in nxt)
                    ):
                        buf.append(nxt)
                        i += 2 * n
                        changed = True
                    else:
                        buf.append(t[i])
                        i += 1
                t = "".join(buf)
        return t

    @staticmethod

    def _prefix_overlap(a: str, b: str) -> bool:
        a = (a or "").strip()
        b = (b or "").strip()
        if not a or not b:
            return False
        if a.startswith(b) or b.startswith(a):
            return True
        ca, cb = SpeechPipeline._collapse_stutter(a), SpeechPipeline._collapse_stutter(b)
        if not ca or not cb:
            return False
        if ca.startswith(cb) or cb.startswith(ca):
            return True
        n = 0
        for x, y in zip(ca, cb):
            if x != y:
                break
            n += 1
        if n >= 4:
            return True
        if len(ca) >= 3 and len(cb) >= 3:
            short, long_ = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
            stem = short[:-2] if len(short) > 4 else short
            if len(stem) >= 3 and long_.startswith(stem):
                return True
        return False

    def _session_wake(self, session: dict) -> WakeDetector:
        wake = session.get("wake")
        if wake is None:
            wake = WakeDetector(session["echo"])
            session["wake"] = wake
        return wake

    def _interrupt_ai_turn(self, app, meeting_id: str, session: dict, *, reason: str) -> bool:
        """Cancel generation and browser playback for the active turn once."""
        if not session.get("ai_turn_active"):
            return False
        cancel = session.setdefault("ai_cancel", threading.Event())
        if cancel.is_set():
            return False
        cancel.set()
        session["ai_generation"] = int(session.get("ai_generation") or 0) + 1
        session["ai_turn_active"] = False
        session["barge_speech_ms"] = 0
        session["barge_pending"] = False
        echo: EchoFilter = session["echo"]
        echo.on_tts_end()
        turn_id = int(session.get("ai_turn_id") or 0)
        broadcast_live(app, "tts_stop", meeting_id, {"turn_id": turn_id, "generation": int(session["ai_generation"]), "reason": reason})
        state = "dialogue_listening" if session.get("dialogue_active") else "interrupted"
        session["dialogue_phase"] = "awaiting_follow_up" if session.get("dialogue_active") else "idle"
        if session.get("dialogue_active"):
            session["dialogue_last_activity"] = time.monotonic()
        broadcast_live(app, "ai_status", meeting_id, {"state": state, "turn_id": turn_id, "generation": int(session["ai_generation"]), "reason": reason})
        log.info("ai turn interrupted meeting=%s turn=%s reason=%s", meeting_id, turn_id, reason)
        return True

    def _hold_ai_playback(self, app, meeting_id: str, session: dict) -> bool:
        """Mute an answer promptly while ASR decides whether speech is meaningful."""
        if not session.get("ai_turn_active") or session.get("barge_pending"):
            return False
        turn_id = int(session.get("ai_turn_id") or 0)
        session["barge_pending"] = True
        session["dialogue_phase"] = "barge_pending"
        generation = int(session.get("ai_generation") or 0)
        broadcast_live(app, "tts_hold", meeting_id, {"turn_id": turn_id, "generation": generation, "reason": "voice_onset"})
        broadcast_live(app, "ai_status", meeting_id, {"state": "barge_pending", "turn_id": turn_id, "generation": generation})
        log.info("ai playback held meeting=%s turn=%s", meeting_id, turn_id)
        return True

    def _resume_ai_playback(self, app, meeting_id: str, session: dict, *, reason: str) -> bool:
        """Resume cached browser PCM after an acknowledgement/noise final."""
        if not session.get("ai_turn_active") or not session.get("barge_pending"):
            return False
        turn_id = int(session.get("ai_turn_id") or 0)
        session["barge_pending"] = False
        session["barge_speech_ms"] = 0
        session["dialogue_phase"] = "speaking"
        generation = int(session.get("ai_generation") or 0)
        broadcast_live(app, "tts_resume", meeting_id, {"turn_id": turn_id, "generation": generation, "reason": reason})
        broadcast_live(app, "ai_status", meeting_id, {"state": "speaking", "turn_id": turn_id, "generation": generation, "reason": reason})
        log.info("ai playback resumed meeting=%s turn=%s reason=%s", meeting_id, turn_id, reason)
        return True

    @staticmethod
    def _compact_spoken_text(text: str) -> str:
        """Keep only lexical characters when deciding whether speech is a barge-in."""
        return DialogueTurnPolicy.compact(text)

    @staticmethod
    def _dialogue_policy(app) -> DialogueTurnPolicy:
        return DialogueTurnPolicy(
            min_barge_chars=int(app.config.get("AI_BARGE_IN_MIN_CHARS", 6)),
            min_barge_ms=int(app.config.get("AI_BARGE_IN_MIN_SPEECH_MS", 700)),
            short_command_chars=int(app.config.get("AI_BARGE_IN_SHORT_COMMAND_CHARS", 3)),
            short_command_ms=int(app.config.get("AI_BARGE_IN_SHORT_COMMAND_MS", 450)),
            min_follow_up_chars=int(app.config.get("AI_DIALOGUE_MIN_FOLLOW_UP_CHARS", 4)),
            min_follow_up_ms=int(app.config.get("AI_DIALOGUE_MIN_FOLLOW_UP_MS", 550)),
        )

    @classmethod
    def _is_meaningful_barge_in(cls, app, text: str, *, duration_ms: int = 0) -> bool:
        """Distinguish an actual interruption from a listener's backchannel.

        VAD only tells us that somebody made a sound.  Acknowledgements such as
        ``嗯`` and ``啊呀`` are common while the assistant speaks, so they must
        never cancel playback. Final ASR supplies the semantic guard: normally
        require six lexical characters; concise, recognized control intent is
        allowed from three characters when the utterance lasted long enough.
        """
        return cls._dialogue_policy(app).should_interrupt(text, duration_ms=duration_ms)

    def _consume_ai_control(self, app, meeting_id: str, session: dict) -> None:
        """Consume a browser Stop request written by the separate web process."""
        control = app.extensions["audio_store"].read_json(meeting_id, AI_CONTROL_NAME) or {}
        requested = int(control.get("stop_turn_id") or 0) if isinstance(control, dict) else 0
        requested_at = float(control.get("requested_at") or 0) if isinstance(control, dict) else 0.0
        if requested_at and requested_at <= float(session.get("last_ai_control_at") or 0):
            return
        if requested_at:
            session["last_ai_control_at"] = requested_at
        active = int(session.get("ai_turn_id") or 0)
        end_dialogue = bool(control.get("end_dialogue")) if isinstance(control, dict) else False
        if end_dialogue:
            was_dialogue = bool(session.get("dialogue_active"))
            session["dialogue_active"] = False
            session["dialogue_last_activity"] = 0.0
            session["dialogue_last_assistant_at"] = 0.0
            session["dialogue_phase"] = "stopped"
            if requested and requested == active:
                self._interrupt_ai_turn(app, meeting_id, session, reason="manual_stop")
            session["dialogue_phase"] = "stopped"
            if was_dialogue:
                broadcast_live(app, "ai_status", meeting_id, {"state": "dialogue_stopped"})
        elif requested and requested == active:
            self._interrupt_ai_turn(app, meeting_id, session, reason="manual_stop")

    @staticmethod
    def _take_tts_sentence(buffer: str, *, final: bool = False, max_chars: int = 40) -> tuple[str, str]:
        """Emit a short natural unit without waiting for the full LLM response."""
        text = buffer or ""
        if not text:
            return "", ""
        # Take the *next* stable sentence, not the last one already in the
        # buffer.  The latter subtly recreates "wait for the whole response"
        # whenever Qwen emits several sentences before the TTS worker wakes.
        for match in _TTS_SENTENCE_END.finditer(text):
            end = match.end()
            if end >= 6 or final:
                return text[:end].strip(), text[end:]
        if len(text) >= max_chars:
            split = max(text.rfind("，", 8), text.rfind("、", 8), text.rfind(" ", 8))
            end = split + 1 if split >= 8 else max_chars
            return text[:end].strip(), text[end:]
        if final:
            return text.strip(), ""
        return "", text

    @staticmethod
    def _short_reply_playback_rate(
        text: str,
        audio_seconds: float,
        *,
        base_rate: float,
        max_rate: float,
        short_reply_chars: int,
        target_chars_per_second: float,
    ) -> float:
        """Keep an unusually slow *complete* short reply conversational.

        Qwen3-TTS intentionally varies acoustic duration. This is natural for
        normal sentences, but a 10--25 character confirmation can occasionally
        receive a much slower contour than a paragraph. The correction is only
        made after the complete short reply is synthesized, never per clause
        in a longer response.
        """
        maximum = max(0.9, float(max_rate))
        base = min(maximum, max(0.9, float(base_rate)))
        compact = re.sub(r"[\s，。！？；：、,.!?;:]+", "", text or "")
        chars = len(compact)
        if not audio_seconds or chars < 4 or chars > max(4, int(short_reply_chars)):
            return base
        needed_rate = float(audio_seconds) * float(target_chars_per_second) / chars
        return min(maximum, max(base, needed_rate))

    @staticmethod
    def _split_tts_units(text: str, *, max_chars: int = 110) -> list[str]:
        """Split a completed answer into coherent, non-tiny TTS requests.

        Qwen3-TTS CustomVoice deliberately adapts prosody from each input.  A
        new request for every streamed LLM sentence therefore sounds like a
        succession of different delivery speeds.  Keep each request large
        enough to establish one prosodic contour, while retaining a cap for
        prompt answers and barge-in responsiveness.
        """
        remaining = (text or "").strip()
        max_chars = max(32, int(max_chars))
        units: list[str] = []
        while remaining:
            if len(remaining) <= max_chars:
                # Avoid ending with a tiny independent request; it resets the
                # voice style for only a few words.  A modestly oversized last
                # unit is much more natural than a 5–15 character tail.
                if units and len(remaining) < max(16, max_chars // 2):
                    units[-1] += remaining
                else:
                    units.append(remaining)
                break
            prefix = remaining[: max_chars + 1]
            # Prefer a complete sentence, then a clause.  Do not use the first
            # punctuation mark: it recreates the short-request problem.
            min_natural = max(16, max_chars // 2)
            sentence_ends = [m.end() for m in _TTS_SENTENCE_END.finditer(prefix) if m.end() >= min_natural]
            clause_ends = [m.end() for m in re.finditer(r"[，、,:：\n]", prefix) if m.end() >= min_natural]
            cut = (sentence_ends or clause_ends or [max_chars])[-1]
            units.append(remaining[:cut].strip())
            remaining = remaining[cut:].lstrip()
        return [unit for unit in units if unit]

    @staticmethod
    def _spoken_answer(app, question: str, answer: str) -> str:
        """Keep ordinary meeting replies concise without reducing text capacity.

        The complete answer remains in the transcript.  Only the spoken path is
        shortened for ordinary prompts so one CustomVoice request can retain a
        stable delivery.  Explicit requests for plans/details keep a larger cap.
        """
        detailed = bool(re.search(r"深入|详细|完整|方案|步骤|对比|分析|展开", question or ""))
        key = "AI_TTS_DETAILED_MAX_CHARS" if detailed else "AI_TTS_DEFAULT_MAX_CHARS"
        fallback = 2000 if detailed else 1000
        # This helper mirrors the streaming path's ceiling.  A response may
        # use several stable, queued CustomVoice units; silently stopping at a
        # historic 180/360-character cap is worse than allowing the user to
        # barge in or use the explicit stop control.
        hard_limit = max(48, int(app.config.get("AI_TTS_HARD_MAX_CHARS", 2000)))
        limit = min(hard_limit, max(48, int(app.config.get(key, fallback))))
        text = (answer or "").strip()
        if len(text) <= limit:
            return text
        prefix = text[:limit]
        cut = max(prefix.rfind("。"), prefix.rfind("！"), prefix.rfind("？"), prefix.rfind("；"))
        if cut >= max(36, limit // 2):
            return prefix[: cut + 1]
        return prefix.rstrip("，、；： ") + "。"

    @staticmethod
    def _follow_up_text(text: str) -> str:
        return _CORRECTION_PUNCT.sub("", text or "").strip()

    def _dialogue_accepts_follow_up(self, app, session: dict, text: str, *, duration_ms: int = 0) -> bool:
        """Return whether an unaddressed final is a meaningful dialogue reply."""
        if not session.get("dialogue_active"):
            return False
        now = time.monotonic()
        # Dialogue mode is an explicit user choice. A positive timeout remains
        # available to deployments that need it, but the default is disabled:
        # after “成就精灵” the assistant stays in conversation until an explicit
        # spoken stop or the Stop button, rather than silently falling back to
        # wake-word mode after two minutes.
        idle = max(0, int(app.config.get("AI_DIALOGUE_IDLE_SECONDS", 0)))
        if idle and now - float(session.get("dialogue_last_activity") or 0.0) > idle:
            session["dialogue_active"] = False
            return False
        policy = self._dialogue_policy(app)
        # Callers in older integrations only supplied a final transcript.  The
        # realtime path always passes measured duration; for that compatibility
        # path, regard the final as having met the minimum duration rather than
        # silently disabling a valid continuous dialogue.
        effective_duration = duration_ms or policy.min_follow_up_ms
        return policy.should_accept_follow_up(
            text,
            duration_ms=effective_duration,
            # Once the assistant is explicitly in dialogue mode, a meaningful
            # unaddressed sentence remains a follow-up even after a long pause.
            # Backchannels are still rejected inside DialogueTurnPolicy.
            plain_speech_allowed=True,
        )

    def _prepare_ai_context_text(
        self,
        app,
        meeting_id: str,
        segment_id: str | None,
        text: str,
        session: dict,
        llm,
    ) -> str:
        """Apply a bounded contextual repair before Qwen answers a live turn.

        Normal captions remain low latency and are corrected in batches.  A
        turn addressed to the assistant is different: letting it reason over a
        known ASR homophone produces a confident but irrelevant answer.  This
        one-line repair runs only for the segment that actually starts an AI
        turn, retains immutable raw ASR, and falls back instantly on failure.
        """
        original = (text or "").strip()
        if (
            not segment_id
            or not original
            or not app.config.get("AI_CONTEXT_CORRECTION_ENABLED", True)
            or len(_CORRECTION_PUNCT.sub("", original)) < 6
        ):
            return original
        try:
            # The current caption was just appended; exclude it from the
            # evidence window so Qwen sees preceding conversation, not a
            # duplicated self-reference.
            recent = self._recent_captions(session)
            if recent and recent[-1] == original:
                recent = recent[:-1]
            repaired = correct_asr_text(original, recent[-8:], llm=llm, force_llm=True)
            lexical = _CORRECTION_PUNCT.sub("", repaired)
            rendered = self._normalize_correction_punctuation(
                app.extensions["providers"].punctuation.finalize(lexical)
            )
            if not rendered or rendered == original:
                return original
            if not self._valid_contextual_correction(app, original, rendered):
                return original
            segment = TranscriptSegment.query.filter_by(id=segment_id, meeting_id=meeting_id).first()
            if segment is None:
                return original
            TranscriptService().apply_correction(segment, rendered)
            broadcast_live(
                app,
                "transcript_final",
                meeting_id,
                transcript_final_payload(segment, text=rendered),
            )
            self._remember_caption(session, rendered)
            log.info("prepared corrected AI context meeting=%s segment=%s", meeting_id, segment_id)
            return rendered
        except Exception:
            log.warning("AI context correction failed meeting=%s segment=%s", meeting_id, segment_id, exc_info=True)
            return original

    def _spawn_ai_turn(
        self,
        app,
        meeting_id: str,
        text: str,
        session: dict,
        *,
        follow_up: bool = False,
        transcript_start_ms: int | None = None,
        transcript_segment_id: str | None = None,
    ) -> bool:
        """Run wake→LLM→TTS off the PCM drain path so a 27B stall cannot freeze captions."""
        echo: EchoFilter = session["echo"]
        wake = self._session_wake(session)
        # Claim wake here so a later final cannot double-fire while LLM runs.
        if follow_up:
            question = (text or "").strip()
        else:
            if not wake.should_respond(text):
                broadcast_live(app, "ai_status", meeting_id, {"state": "listening"})
                return False
            question = wake.extract_question(text)
        if not (question or "").strip():
            broadcast_live(app, "ai_status", meeting_id, {"state": "listening"})
            return False

        _wake_inflight.set()
        # Each turn owns a distinct event.  Clearing a shared event when the
        # follow-up starts would otherwise revive the interrupted LLM stream.
        cancel = threading.Event()
        session["ai_cancel"] = cancel
        session["ai_turn_id"] = int(session.get("ai_turn_id") or 0) + 1
        turn_id = int(session["ai_turn_id"])
        session["ai_generation"] = int(session.get("ai_generation") or 0) + 1
        generation = int(session["ai_generation"])
        session["ai_turn_active"] = True
        session["barge_speech_ms"] = 0
        session["barge_pending"] = False
        session["dialogue_active"] = True
        session["dialogue_last_activity"] = time.monotonic()
        session["ai_turn_started_at"] = time.monotonic()
        # Capture the meeting-audio position before waiting on Qwen/TTS.  It
        # is the stable chronological slot for the persisted assistant reply.
        session["ai_turn_transcript_start_ms"] = max(0, int(transcript_start_ms or 0))
        session["dialogue_phase"] = "thinking"
        # Start echo matching with this answer only.  Streaming TTS can emit
        # several units, which EchoFilter retains for the entire turn.
        echo.begin_turn()
        broadcast_live(app, "ai_status", meeting_id, {"state": "thinking", "turn_id": turn_id, "generation": generation})

        def _run() -> None:
            with app.app_context():
                providers = app.extensions["providers"]
                try:
                    graph = AITurnGraph(providers.llm, providers.tts, echo)
                    graph.wake = wake
                    answer_parts: list[str] = []
                    # Keep LLM deltas and TTS inputs on two ordered stages,
                    # matching speech-to-speech's LMOutputProcessor ->
                    # Qwen3TTSHandler path.  The LLM producer puts stable
                    # sentence chunks here as it receives them; the consumer
                    # coalesces consecutive chunks before one synthesis call.
                    # It must not wait for EndOfResponse to start talking.
                    tts_inputs: Queue[str | object] = Queue()
                    tts_end = object()
                    tts_done = threading.Event()
                    tts_result: dict[str, Any] = {
                        "audio_seconds": 0.0,
                        "playback_seconds": 0.0,
                        "spoken_chars": 0,
                        "started_at": None,
                        "first_audio_at": None,
                        "error": None,
                    }
                    resolved_question = question
                    detailed = bool(re.search(r"深入|详细|完整|方案|步骤|对比|分析|展开", question or ""))
                    spoken_key = "AI_TTS_DETAILED_MAX_CHARS" if detailed else "AI_TTS_DEFAULT_MAX_CHARS"
                    hard_spoken_limit = max(48, int(app.config.get("AI_TTS_HARD_MAX_CHARS", 2000)))
                    spoken_limit = min(
                        hard_spoken_limit,
                        max(48, int(app.config.get(spoken_key, 2000 if detailed else 1000))),
                    )
                    sentence_batch = max(1, int(app.config.get("AI_TTS_STREAM_BATCH_SENTENCES", 3)))
                    batch_wait_seconds = max(0.08, float(app.config.get("AI_TTS_STREAM_BATCH_WAIT_MS", 220)) / 1000.0)

                    def _emit_tts_audio(utterance: str, *, completed_short_reply: bool = False) -> None:
                        if not utterance or cancel.is_set() or int(session.get("ai_turn_id") or 0) != turn_id:
                            return
                        # Reference Qwen3TTS coalesces immediately adjacent
                        # TTSInput objects into one CustomVoice request.  The
                        # fixed voice/profile and this batching prevent a new
                        # prosodic reset for every incoming LLM token.
                        with _tts_lock:
                            if cancel.is_set() or int(session.get("ai_turn_id") or 0) != turn_id:
                                return
                            echo.on_tts(utterance)
                            session["dialogue_phase"] = "speaking"
                            broadcast_live(
                                app,
                                "ai_status",
                                meeting_id,
                                {"state": "speaking", "turn_id": turn_id, "generation": generation},
                            )
                            if tts_result["started_at"] is None:
                                tts_result["started_at"] = time.monotonic()
                            rate = int(getattr(providers.tts, "SAMPLE_RATE", 16000) or 16000)
                            streams_pcm = bool(getattr(providers.tts, "STREAMS_PCM16", False))
                            transport_samples = max(
                                512,
                                int(app.config.get("AI_TTS_TRANSPORT_BLOCK_SAMPLES", 2048)),
                            )
                            transport_bytes = transport_samples * 2
                            pending_pcm = bytearray()
                            base_playback_rate = min(
                                float(app.config.get("AI_TTS_MAX_PLAYBACK_RATE", 1.60)),
                                max(0.9, float(app.config.get("AI_TTS_PLAYBACK_RATE", 1.0))),
                            )

                            def broadcast_audio(payload: bytes, playback_rate: float) -> None:
                                if not payload:
                                    return
                                broadcast_live(
                                    app,
                                    "tts_audio",
                                    meeting_id,
                                    {
                                        "audio_b64": base64.b64encode(payload).decode("ascii"),
                                        "mime": "audio/pcm;codec=s16le" if streams_pcm else "audio/wav",
                                        "byte_length": len(payload),
                                        "sample_rate": rate,
                                        "playback_rate": playback_rate,
                                        "turn_id": turn_id,
                                        "generation": generation,
                                    },
                                )

                            def deliver_pcm(pcm: bytes, playback_rate: float) -> None:
                                if not pcm:
                                    return
                                if not streams_pcm:
                                    broadcast_audio(pcm, playback_rate)
                                    return
                                pending_pcm.extend(pcm)
                                while len(pending_pcm) >= transport_bytes:
                                    broadcast_audio(bytes(pending_pcm[:transport_bytes]), playback_rate)
                                    del pending_pcm[:transport_bytes]

                            def flush_pending(playback_rate: float) -> None:
                                if streams_pcm and pending_pcm:
                                    broadcast_audio(bytes(pending_pcm), playback_rate)
                                    pending_pcm.clear()

                            # A complete short reply is cheap to buffer after
                            # warmed MLX synthesis. Measure its actual audio
                            # duration once, so an occasional overly slow
                            # short contour can be corrected consistently.
                            buffered_pcm: list[bytes] | None = [] if completed_short_reply else None
                            for pcm in providers.tts.stream(utterance):
                                if cancel.is_set() or int(session.get("ai_turn_id") or 0) != turn_id:
                                    return
                                if not pcm:
                                    continue
                                if tts_result["first_audio_at"] is None:
                                    tts_result["first_audio_at"] = time.monotonic()
                                payload_bytes = len(pcm) - (0 if streams_pcm else 44)
                                duration = max(0, payload_bytes) / float(max(rate * 2, 1))
                                tts_result["audio_seconds"] += duration
                                if buffered_pcm is not None:
                                    buffered_pcm.append(bytes(pcm))
                                else:
                                    tts_result["playback_seconds"] += duration / base_playback_rate
                                    deliver_pcm(pcm, base_playback_rate)
                            if buffered_pcm is not None:
                                playback_rate = self._short_reply_playback_rate(
                                    utterance,
                                    float(tts_result["audio_seconds"]),
                                    base_rate=base_playback_rate,
                                    max_rate=float(app.config.get("AI_TTS_MAX_PLAYBACK_RATE", 1.60)),
                                    short_reply_chars=int(app.config.get("AI_TTS_SHORT_REPLY_CHARS", 28)),
                                    target_chars_per_second=float(app.config.get("AI_TTS_SHORT_TARGET_CHARS_PER_SECOND", 5.0)),
                                )
                                for pcm in buffered_pcm:
                                    payload_bytes = len(pcm) - (0 if streams_pcm else 44)
                                    duration = max(0, payload_bytes) / float(max(rate * 2, 1))
                                    tts_result["playback_seconds"] += duration / playback_rate
                                    deliver_pcm(pcm, playback_rate)
                                flush_pending(playback_rate)
                            else:
                                flush_pending(base_playback_rate)

                    def _consume_tts_inputs() -> None:
                        buffer = ""
                        sentences: list[str] = []
                        batch_started_at: float | None = None
                        input_closed = False
                        try:
                            while not cancel.is_set():
                                if not input_closed:
                                    try:
                                        wait = 0.08
                                        if sentences and batch_started_at is not None:
                                            wait = min(wait, max(0.005, batch_wait_seconds - (time.monotonic() - batch_started_at)))
                                        item = tts_inputs.get(timeout=wait)
                                    except Empty:
                                        item = None
                                    if item is not None:
                                        if item is tts_end:
                                            input_closed = True
                                        elif isinstance(item, str):
                                            buffer += item

                                # As in the reference TTS handler, consume all
                                # deltas already queued before deciding what to
                                # synthesize.  This joins adjacent LLM chunks
                                # without waiting for the final response.
                                while not input_closed:
                                    try:
                                        item = tts_inputs.get_nowait()
                                    except Empty:
                                        break
                                    if item is tts_end:
                                        input_closed = True
                                    elif isinstance(item, str):
                                        buffer += item

                                while buffer:
                                    early = not bool(tts_result["started_at"])
                                    max_chars = int(
                                        app.config.get(
                                            "AI_TTS_EARLY_CHARS" if early else "AI_TTS_FOLLOWING_CHARS",
                                            36 if early else 72,
                                        )
                                    )
                                    sentence, remaining = self._take_tts_sentence(
                                        buffer,
                                        final=input_closed,
                                        max_chars=max_chars,
                                    )
                                    if not sentence:
                                        break
                                    buffer = remaining
                                    sentences.append(sentence)
                                    if batch_started_at is None:
                                        batch_started_at = time.monotonic()
                                    # speech-to-speech batches sentence chunks
                                    # (three by default), then its TTS handler
                                    # coalesces the adjacent queue entries.
                                    batch_elapsed = time.monotonic() - (batch_started_at or time.monotonic())
                                    if len(sentences) < sentence_batch and not input_closed and batch_elapsed < batch_wait_seconds:
                                        continue
                                    utterance = "".join(sentences).strip()
                                    completed_short_reply = input_closed and tts_result["started_at"] is None
                                    sentences.clear()
                                    batch_started_at = None
                                    _emit_tts_audio(utterance, completed_short_reply=completed_short_reply)
                                    if cancel.is_set():
                                        return

                                # If no more LLM deltas arrive, the deadline
                                # flushes a small complete sentence instead of
                                # leaving it queued until EndOfResponse.
                                if (
                                    sentences
                                    and not input_closed
                                    and batch_started_at is not None
                                    and time.monotonic() - batch_started_at >= batch_wait_seconds
                                ):
                                    utterance = "".join(sentences).strip()
                                    sentences.clear()
                                    batch_started_at = None
                                    _emit_tts_audio(utterance)
                                    if cancel.is_set():
                                        return

                                if input_closed:
                                    if sentences:
                                        _emit_tts_audio(
                                            "".join(sentences).strip(),
                                            completed_short_reply=tts_result["started_at"] is None,
                                        )
                                    return
                        except Exception as exc:
                            tts_result["error"] = exc
                            log.warning("ai tts stream failed meeting=%s turn=%s", meeting_id, turn_id, exc_info=True)
                        finally:
                            tts_done.set()

                    tts_thread = threading.Thread(
                        target=_consume_tts_inputs,
                        name=f"ai-tts-{meeting_id[:8]}-{turn_id}",
                        daemon=True,
                    )
                    tts_thread.start()

                    # The serialized local LLM remains protected from
                    # correction/summary work, but its output is no longer
                    # held hostage by TTS completion.
                    with _llm_lock:
                        prepared_turn_text = self._prepare_ai_context_text(
                            app, meeting_id, transcript_segment_id, text, session, providers.llm
                        )
                        if follow_up:
                            resolved_question = prepared_turn_text
                        else:
                            resolved_question = wake.extract_question(prepared_turn_text)
                        prompt = graph.build_prompt(meeting_id, resolved_question)
                        first_token_at: float | None = None
                        try:
                            for token in providers.llm.stream(prompt, cancel_event=cancel):
                                if cancel.is_set():
                                    break
                                if not token:
                                    continue
                                if first_token_at is None:
                                    first_token_at = time.monotonic()
                                    first_token_ms = round(
                                        (first_token_at - float(session.get("ai_turn_started_at") or first_token_at)) * 1000
                                    )
                                    log.info(
                                        "ai turn first token meeting=%s turn=%s latency_s=%.2f",
                                        meeting_id,
                                        turn_id,
                                        first_token_ms / 1000,
                                    )
                                    broadcast_live(
                                        app,
                                        "ai_status",
                                        meeting_id,
                                        {
                                            "state": "thinking",
                                            "turn_id": turn_id,
                                            "generation": generation,
                                            "first_token_latency_ms": first_token_ms,
                                        },
                                    )
                                answer_parts.append(token)
                                # Preserve the complete text answer in the UI
                                # and meeting history, while limiting only the
                                # audible portion of an ordinary reply.
                                if tts_result["spoken_chars"] < spoken_limit:
                                    audible = token[: spoken_limit - tts_result["spoken_chars"]]
                                    if audible:
                                        tts_result["spoken_chars"] += len(audible)
                                        tts_inputs.put(audible)
                                broadcast_live(app, "ai_token", meeting_id, {"text": token, "turn_id": turn_id, "generation": generation})
                        except Exception:
                            log.warning(
                                "ai turn llm stream failed meeting=%s turn=%s",
                                meeting_id,
                                turn_id,
                                exc_info=True,
                            )
                    answer = "".join(answer_parts).strip()
                    if cancel.is_set():
                        tts_inputs.put(tts_end)
                        tts_done.wait(timeout=1.0)
                        log.info("ai turn cancelled meeting=%s turn=%s partial_len=%s", meeting_id, turn_id, len(answer))
                        return
                    if not answer:
                        answer = "刚才没接上，请再说一次"
                        tts_result["spoken_chars"] = len(answer)
                        tts_inputs.put(answer)
                    tts_inputs.put(tts_end)
                    tts_done.wait()
                    ai_start_ms = int(session.get("ai_turn_transcript_start_ms") or 0)
                    ai_end_ms = ai_start_ms + round(
                        float(tts_result["playback_seconds"] or 0.0) * 1000
                    )
                    ai_segment = graph.persist_answer(
                        meeting_id,
                        resolved_question,
                        answer,
                        start_ms=ai_start_ms,
                        end_ms=max(ai_start_ms, ai_end_ms),
                    )
                    broadcast_live(
                        app,
                        "transcript_final",
                        meeting_id,
                        transcript_final_payload(ai_segment, text=answer, speaker=AI_SPEAKER),
                    )
                    audio_seconds = float(tts_result["audio_seconds"] or 0.0)
                    tts_started_at = tts_result["started_at"] or time.monotonic()
                    log.info(
                        "ai turn ok meeting=%s turn=%s question=%s answer_len=%s tts_s=%.2f tts=%s",
                        meeting_id,
                        turn_id,
                        resolved_question[:80],
                        len(answer),
                        audio_seconds,
                        type(providers.tts).__name__,
                    )
                    log.info(
                        "ai tts metrics meeting=%s turn=%s answer_chars=%s spoken_chars=%s generation_s=%.2f audio_s=%.2f playback_s=%.2f",
                        meeting_id,
                        turn_id,
                        len(answer),
                        int(tts_result["spoken_chars"]),
                        time.monotonic() - tts_started_at,
                        audio_seconds,
                        float(tts_result["playback_seconds"] or 0.0),
                    )

                    # Browser playback starts while MLX is still generating.
                    # Do not add the *whole* synthesized duration after the
                    # last PCM chunk, otherwise the dialogue looks speaking
                    # long after its final audio has finished.
                    # The browser consumes PCM at playback_rate.  Keep the
                    # server's speech state in the same timebase, with a small
                    # tail guard for socket/Worklet scheduling.  The browser
                    # deliberately does not clear its remaining queue when
                    # this state flips, so an estimate can never truncate it.
                    remaining_audio = max(
                        0.2,
                        float(tts_result["playback_seconds"] or 0.0) - (time.monotonic() - tts_started_at) + 0.5,
                    )

                    def finish_audio() -> None:
                        if int(session.get("ai_turn_id") or 0) != turn_id or cancel.is_set():
                            return
                        session["ai_turn_active"] = False
                        session["dialogue_last_assistant_at"] = time.monotonic()
                        session["dialogue_phase"] = "awaiting_follow_up" if session.get("dialogue_active") else "idle"
                        echo.on_tts_end()
                        state = "dialogue_listening" if session.get("dialogue_active") else "listening"
                        broadcast_live(app, "ai_status", meeting_id, {"state": state, "turn_id": turn_id})

                    threading.Timer(remaining_audio, finish_audio).start()
                except Exception:
                    log.warning(
                        "ai turn failed meeting=%s text=%s",
                        meeting_id,
                        text[:80],
                        exc_info=True,
                    )
                    try:
                        echo.on_tts_end()
                    except Exception:
                        pass
                    session["ai_turn_active"] = False
                    session["dialogue_last_assistant_at"] = time.monotonic()
                    session["dialogue_phase"] = "awaiting_follow_up" if session.get("dialogue_active") else "idle"
                    state = "dialogue_listening" if session.get("dialogue_active") else "listening"
                    broadcast_live(app, "ai_status", meeting_id, {"state": state, "turn_id": turn_id})
                finally:
                    # A newly queued follow-up owns the gate; do not make a
                    # correction batch jump ahead while it waits for this turn.
                    if int(session.get("ai_turn_id") or 0) == turn_id:
                        _wake_inflight.clear()

        threading.Thread(target=_run, name=f"ai-turn-{meeting_id[:8]}", daemon=True).start()
        return True

    def _recent_captions(self, session: dict) -> list[str]:
        caps = session.get("recent_captions")
        if isinstance(caps, list):
            return [c for c in caps if c]
        return []

    def _remember_caption(self, session: dict, display: str) -> None:
        caps = session.setdefault("recent_captions", [])
        if not isinstance(caps, list):
            caps = []
            session["recent_captions"] = caps
        text = (display or "").strip()
        if text:
            if caps and (text.startswith(caps[-1]) or caps[-1].startswith(text)):
                if len(text) >= len(caps[-1]):
                    caps[-1] = text
            else:
                caps.append(text)
        del caps[:-8]

    @classmethod
    def _is_nearby_duplicate_final(
        cls,
        session: dict,
        text: str,
        *,
        start_ms: int,
        end_ms: int,
        cluster: str | None,
    ) -> bool:
        """Drop only backend replays of the same VAD final, never a repeat request."""
        compact = cls._compact_spoken_text(text)
        if not compact:
            return False
        for prior in reversed(session.get("recent_finals") or []):
            if str(prior.get("cluster") or "") != str(cluster or ""):
                continue
            old_start, old_end = int(prior.get("start_ms") or 0), int(prior.get("end_ms") or 0)
            nearby = max(start_ms, old_start) <= min(end_ms, old_end) or abs(start_ms - old_end) <= 1200
            if not nearby:
                continue
            old = str(prior.get("text") or "")
            if compact == old or cls._prefix_overlap(compact, old):
                return True
        return False

    @staticmethod
    def _remember_final(session: dict, text: str, *, start_ms: int, end_ms: int, cluster: str | None) -> None:
        finals = session.setdefault("recent_finals", [])
        finals.append({"text": text, "start_ms": start_ms, "end_ms": end_ms, "cluster": cluster})
        del finals[:-12]

    @staticmethod
    def _valid_correction(source: str, candidate: str) -> bool:
        """Allow a tiny local ASR edit, never a fluent rewrite of a fragment."""
        source = _CORRECTION_PUNCT.sub("", source or "")
        candidate = _CORRECTION_PUNCT.sub("", candidate or "")
        if len(source) < 8 or not candidate:
            return False
        if re.findall(r"\d+(?:\.\d+)?", source) != re.findall(r"\d+(?:\.\d+)?", candidate):
            return False
        edits = 0
        runs = 0
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, source, candidate).get_opcodes():
            if tag == "equal":
                continue
            # Removing or reorganising fillers is interpretation, not ASR repair.
            if tag == "delete":
                return False
            runs += 1
            edits += max(i2 - i1, j2 - j1)
        return runs == 1 and edits <= 2

    @staticmethod
    def _clean_local_disfluencies(source: str) -> str:
        """Remove only mechanically provable ASR stutter, never infer content.

        This runs before the LLM.  It covers adjacent pronoun/function-character
        stutter (``我我觉得``) and exact repeated multi-character phrases
        (``这个这个方案``).  Backchannels such as ``嗯嗯`` and lexical reduplication
        are intentionally kept because they can carry meaning or affect tone.
        """
        text = (source or "").strip()
        if not text:
            return text
        out: list[str] = []
        i = 0
        while i < len(text):
            ch = text[i]
            # Exact adjacent phrase repeats are safe evidence of a decoder
            # restart.  Require two CJK characters to avoid changing "看看".
            matched = 0
            for n in range(min(8, (len(text) - i) // 2), 1, -1):
                unit = text[i : i + n]
                if (
                    unit == text[i + n : i + 2 * n]
                    and all("\u4e00" <= c <= "\u9fff" for c in unit)
                    and not DialogueTurnPolicy().is_backchannel(unit)
                ):
                    matched = n
                    break
            if matched:
                out.append(text[i : i + matched])
                i += matched * 2
            elif ch in _STUTTER_SINGLE_CHARS:
                j = i + 1
                while j < len(text) and text[j] == ch:
                    j += 1
                out.append(ch)
                i = j
            else:
                out.append(ch)
                i += 1
        return "".join(out)

    @classmethod
    def _apply_safe_replacement(cls, source: str, replacement: dict) -> str | None:
        """Apply one exact source-span replacement from the correction model."""
        if not isinstance(replacement, dict):
            return None
        old = str(replacement.get("from") or "").strip()
        new = str(replacement.get("to") or "").strip()
        raw = (source or "").strip()
        if len(raw) < 8 or len(old) < 2 or not new:
            return None
        if _CORRECTION_PUNCT.search(old) or _CORRECTION_PUNCT.search(new):
            return None
        if raw.count(old) != 1:
            return None
        candidate = raw.replace(old, new, 1)
        return candidate if cls._valid_correction(raw, candidate) else None

    @staticmethod
    def _valid_contextual_correction(app, source: str, candidate: str) -> bool:
        """Accept a faithful multi-edit ASR repair, never a summary or rewrite.

        The editor may now fix several terms, repeats and sentence boundaries in
        one long utterance.  Raw ASR remains immutable, and conservative shape,
        number and character-overlap checks reject a response that replaces the
        speaker's content rather than repairing its recognition.
        """
        raw = _CORRECTION_PUNCT.sub("", source or "")
        fixed = _CORRECTION_PUNCT.sub("", candidate or "")
        if len(raw) < 8 or len(fixed) < 4:
            return False
        # Digits embedded in an English model name (for example Qwen3) are
        # terminology, not a spoken business number.  Standalone values remain
        # immutable through the correction layer.
        if _NUMERIC_FACT.findall(raw) != _NUMERIC_FACT.findall(fixed):
            return False
        if not (len(raw) * 0.45 <= len(fixed) <= len(raw) * 1.35):
            return False
        minimum = float(app.config.get("CORRECTION_MIN_SIMILARITY", 0.55))
        return SequenceMatcher(None, raw, fixed).ratio() >= minimum

    @staticmethod
    def _normalize_correction_punctuation(text: str) -> str:
        """Make the punctuation model's second pass idempotent for LLM output."""
        def terminal(match: re.Match) -> str:
            run = match.group(0)
            if "？" in run or "?" in run:
                return "？"
            if "！" in run or "!" in run:
                return "！"
            return "。"

        cleaned = (text or "").strip()
        cleaned = re.sub(r"[，、]{2,}", "，", cleaned)
        cleaned = re.sub(r"[；;]{2,}", "；", cleaned)
        cleaned = re.sub(r"[：:]{2,}", "：", cleaned)
        cleaned = re.sub(r"[，、；：:]+(?=[。！？!?])", "", cleaned)
        cleaned = re.sub(r"([。！？!?])[，、；：:]+", r"\1", cleaned)
        cleaned = _TERMINAL_PUNCT_RUN.sub(terminal, cleaned)
        return cleaned.strip()

    def _queue_correction(
        self,
        app,
        meeting_id: str,
        session: dict,
        *,
        force: bool = False,
        completed: threading.Event | None = None,
    ) -> bool:
        """Run Qwen correction in batches, never per real-time segment.

        The model is allowed to correct homophones/terms only.  It returns a JSON
        object keyed by immutable segment id, so a late response cannot overwrite
        a different ASR revision.
        """
        now = time.time()
        last = float(session.get("last_correction_at") or 0)
        pending = session.setdefault("correction_pending", [])
        # ``add_segment`` commits before returning, so ORM instances can be
        # expired/detached by the time the asynchronous correction batch runs.
        # Keep immutable snapshots in the queue instead of holding model rows.
        chars = sum(len(str(item.get("text") or "")) for item in pending)
        pending_since = float(session.get("correction_pending_since") or now)
        if not pending or (
            not force
            and chars < int(app.config["CORRECTION_BATCH_CHARS"])
            and now - pending_since < int(app.config["CORRECTION_INTERVAL_SECONDS"])
        ):
            return False
        # Qwen3.8 is a deliberately serial local service.  A correction job
        # can otherwise occupy it just as someone says the wake phrase and
        # turn a conversational reply into a minute-long apparent stall.
        # FunASR punctuation and local disfluency cleanup remain immediate;
        # contextual Qwen correction is flushed at meeting finalization.
        if (
            not app.config.get("TESTING")
            and not force
            and not app.config.get("REALTIME_BACKGROUND_LLM_ENABLED", False)
        ):
            return False
        if _wake_inflight.is_set() or not _llm_lock.acquire(blocking=False):
            return False
        rows = list(pending)
        pending.clear()
        session.pop("correction_pending_since", None)
        session["last_correction_at"] = now

        def _run() -> None:
            try:
                with app.app_context():
                    terms = [t.term for t in Terminology.query.filter_by(enabled=True).limit(100).all()]
                    all_segments = TranscriptSegment.query.filter_by(meeting_id=meeting_id).order_by(
                        TranscriptSegment.start_ms.asc(), TranscriptSegment.created_at.asc()
                    ).all()
                    positions = {seg.id: idx for idx, seg in enumerate(all_segments)}
                    context_lines = max(1, int(app.config.get("CORRECTION_CONTEXT_LINES", 3)))
                    payload = []
                    for item in rows:
                        pos = positions.get(item["id"])
                        if pos is None:
                            continue
                        current_segment = all_segments[pos]
                        before = [
                            (seg.punctuated_text or seg.raw_text or "").strip()
                            for seg in all_segments[max(0, pos - context_lines) : pos]
                            if seg.speaker_label != AI_SPEAKER
                        ]
                        after = [
                            (seg.punctuated_text or seg.raw_text or "").strip()
                            for seg in all_segments[pos + 1 : pos + 2]
                            if seg.speaker_label != AI_SPEAKER
                        ]
                        # SenseVoice is the preferred acoustic text whenever
                        # its sentence-end pass has completed. Qwen then edits
                        # that result rather than trying to repair an older
                        # low-latency Paraformer hypothesis.
                        raw = str(current_segment.sensevoice_text or item["text"] or "").strip()
                        payload.append(
                            {
                                "id": item["id"],
                                "context_before": before,
                                "raw_text": raw,
                                "local_draft": self._clean_local_disfluencies(raw),
                                "context_after": after,
                            }
                        )
                    local_by_id = {item["id"]: item["local_draft"] for item in payload}

                    def apply_candidate(seg, candidate_raw: str) -> None:
                        # The editor may return punctuation, but the local
                        # punctuation model must see lexical text exactly once.
                        lexical = _CORRECTION_PUNCT.sub("", candidate_raw or "")
                        candidate = self._normalize_correction_punctuation(
                            app.extensions["providers"].punctuation.finalize(lexical)
                        )
                        if candidate == (seg.display_text or seg.punctuated_text or seg.raw_text):
                            return
                        TranscriptService().apply_correction(seg, candidate)
                        broadcast_live(
                            app,
                            "transcript_final",
                            meeting_id,
                            transcript_final_payload(seg, text=candidate),
                        )

                    # Do local, reversible-by-restore cleanup even when the
                    # Qwen endpoint is unavailable or chooses no correction.
                    for seg_id, local in local_by_id.items():
                        seg = TranscriptSegment.query.filter_by(id=seg_id, meeting_id=meeting_id).first()
                        if seg is not None and local != seg.raw_text:
                            apply_candidate(seg, local)
                    known_terms = sorted({*terms, "AI", "FunASR", "Qwen3", "Qwen3-TTS", "TTS"})
                    prompt = (
                        "你是中文会议 ASR 校订器。你的任务是忠实还原说话者原意，不是摘要、润色或改写。"
                        "可以大胆修复多处明确的同音错字、专有名词、英文型号、口吃、ASR 重启造成的重复词，"
                        "并补全合理标点；可删除没有语义的语气词或重复测试问候。context_before/context_after"
                        "仅用于判断当前句已有词的含义，绝不能把上下文的新信息补写到当前句。"
                        "删除无法融入句子的孤立虚词和明显重复的设备测试问候，但不要删除有实际信息的追问。"
                        "不得新增观点、事实、数字、日期、人员或未被原话支持的细节；术语不确定时保留原词。"
                        "每段返回完整 corrected_text；若没有可靠改动则返回空字符串。confidence 只能是 high、medium、low。\n"
                        "只返回 JSON：{\"segments\":[{\"id\":\"...\",\"corrected_text\":\"...\",\"confidence\":\"high\"}]}。\n"
                        f"术语表（可用于核对，不可凭空补词）：{known_terms}\n待校订：{payload}"
                    )
                    result = parse_json_payload(app.extensions["providers"].llm.complete(prompt, json_mode=True))
                    applied_count = 0
                    for item in result.get("segments") or []:
                        if not isinstance(item, dict):
                            continue
                        seg = TranscriptSegment.query.filter_by(id=item.get("id"), meeting_id=meeting_id).first()
                        candidate_text = str(item.get("corrected_text") or "").strip()
                        confidence = str(item.get("confidence") or "").lower()
                        if seg is None or not candidate_text or confidence not in {"high", "medium"}:
                            continue
                        local = local_by_id.get(seg.id, self._clean_local_disfluencies(seg.raw_text))
                        if not self._valid_contextual_correction(app, local, candidate_text):
                            continue
                        before = seg.display_text or seg.punctuated_text or seg.raw_text
                        apply_candidate(seg, candidate_text)
                        if (seg.display_text or seg.punctuated_text or seg.raw_text) != before:
                            applied_count += 1
                    log.info(
                        "batched ASR correction complete meeting=%s requested=%s applied=%s",
                        meeting_id,
                        len(payload),
                        applied_count,
                    )
            except Exception:
                log.warning("batched ASR correction failed meeting=%s", meeting_id, exc_info=True)
            finally:
                _llm_lock.release()
                if completed is not None:
                    completed.set()

        threading.Thread(target=_run, name=f"asr-correction-{meeting_id[:8]}", daemon=True).start()
        return True

    def _queue_sensevoice_refinement(
        self,
        app,
        meeting_id: str,
        segment_id: str,
        pcm: bytes,
        *,
        sample_rate: int,
        duration_ms: int,
    ) -> None:
        """Refine a committed VAD turn without delaying live turn handling."""
        provider = getattr(app.extensions["providers"], "sensevoice", None)
        if provider is None or not app.config.get("SENSEVOICE_REFINEMENT_ENABLED", False):
            return
        minimum = max(1, int(app.config.get("SENSEVOICE_MIN_UTTERANCE_MS", 700)))
        maximum = max(minimum, int(app.config.get("SENSEVOICE_MAX_UTTERANCE_MS", 30000)))
        if not pcm or duration_ms < minimum or duration_ms > maximum:
            return

        def normalize(value: str) -> str:
            return _CORRECTION_PUNCT.sub("", value or "")

        def _run() -> None:
            try:
                with app.app_context():
                    candidate_raw = provider.transcribe_pcm(pcm, sample_rate=sample_rate)
                    segment = TranscriptSegment.query.filter_by(id=segment_id, meeting_id=meeting_id).first()
                    if segment is None or not candidate_raw:
                        return
                    baseline = segment.punctuated_text or segment.raw_text
                    source, candidate = normalize(baseline), normalize(candidate_raw)
                    if len(candidate) < 2:
                        return
                    ratio = SequenceMatcher(None, source, candidate).ratio() if source else 1.0
                    if ratio < float(app.config.get("SENSEVOICE_MIN_SIMILARITY", 0.28)):
                        log.info("SenseVoice result rejected meeting=%s segment=%s similarity=%.2f", meeting_id, segment_id, ratio)
                        return
                    # The punctuation model receives lexical content once;
                    # SenseVoice's rich tags have already been stripped by the
                    # provider before this point.
                    candidate = self._normalize_correction_punctuation(
                        app.extensions["providers"].punctuation.finalize(candidate)
                    )
                    if not candidate:
                        return
                    TranscriptService().apply_sensevoice(segment, candidate)
                    broadcast_live(
                        app,
                        "transcript_final",
                        meeting_id,
                        transcript_final_payload(segment, text=segment.display_text or candidate),
                    )
                    log.info("SenseVoice refined meeting=%s segment=%s similarity=%.2f", meeting_id, segment_id, ratio)
            except Exception:
                # This is an enhancement layer. A bad model/device result must
                # never interrupt real-time Paraformer or continuous dialogue.
                log.warning("SenseVoice refinement failed meeting=%s segment=%s", meeting_id, segment_id, exc_info=True)

        threading.Thread(target=_run, name=f"sensevoice-{segment_id[:8]}", daemon=True).start()

    def _on_final(
        self,
        app,
        meeting_id: str,
        text: str,
        pcm: bytes,
        session: dict,
        *,
        start_ms: int = 0,
        end_ms: int = 0,
    ) -> TranscriptSegment | None:
        echo: EchoFilter = session["echo"]
        text = (text or "").strip()
        if not text:
            return None
        wake = self._session_wake(session)
        # Check control semantics against raw ASR *before* echo filtering or
        # delayed SenseVoice refinement. A garbled but recognizable stop must
        # never become a new wake request while the assistant is speaking.
        stop_display = canonical_stop_command(
            text, dialogue_active=bool(session.get("dialogue_active"))
        )
        if echo.is_echo(text) and not stop_display:
            # VAD may have already sent tts_hold before final ASR identifies
            # playback leakage. An echo is a non-interruption, so resume now.
            self._resume_ai_playback(app, meeting_id, session, reason="echo")
            return None
        punctuator = session.setdefault("punctuation", app.extensions["providers"].punctuation)
        punctuated = punctuator.finalize(text)
        display = (punctuated or text).strip()
        stop_display = stop_display or canonical_stop_command(
            display, dialogue_active=bool(session.get("dialogue_active"))
        )
        if stop_display:
            display = stop_display
        context_critical = wake.is_addressed(display) or bool(session.get("dialogue_active"))

        svc = TranscriptService()
        speakers = session["diarization"].current_speakers() or []
        utt = bytes(session.get("utt_pcm") or b"") or (pcm or b"")
        session["utt_pcm"] = bytearray()
        sample_rate = int(session.get("sample_rate") or PCM_SAMPLE_RATE)
        duration_ms = int(end_ms - start_ms) if end_ms > start_ms else int(len(utt) / max(sample_rate * 2, 1) * 1000)
        if duration_ms < 1:
            duration_ms = int(len(utt) / max(sample_rate * 2, 1) * 1000)
        human_speakers = [item for item in speakers if item.get("speaker") != AI_SPEAKER]
        defer_speaker_label = (
            isinstance(app.extensions["providers"].embedding, CampPlusSpeakerEmbeddingProvider)
            and not context_critical
            and len(human_speakers) <= 1
        )
        if defer_speaker_label:
            # Do not hold the user-visible final behind CPU CAM++ inference.
            # A per-meeting FIFO executor applies the real cluster afterward
            # and broadcasts an in-place update for this transcript row.
            provisional = session.get("speaker_label_snapshot") or {}
            label = str(provisional.get("label") or "Speaker 1")
            user_id = provisional.get("user_id")
            cluster = provisional.get("cluster") or "0"
            conf = float(provisional.get("confidence") or 0.0)
        else:
            label, user_id, cluster, conf = self._label_speaker(
                app,
                utt or pcm,
                speakers,
                session,
                duration_ms=duration_ms,
                context_critical=context_critical,
            )
            if (
                isinstance(app.extensions["providers"].embedding, CampPlusSpeakerEmbeddingProvider)
                and cluster != "overlap"
            ):
                session["speaker_label_snapshot"] = {
                    "label": label,
                    "user_id": user_id,
                    "cluster": cluster,
                    "confidence": conf,
                }

        if self._is_nearby_duplicate_final(
            session, text, start_ms=start_ms, end_ms=end_ms, cluster=cluster
        ):
            log.info("skip nearby duplicate final meeting=%s text=%s", meeting_id, text[:80])
            return None

        session["last_final_text"] = text
        self._remember_final(session, self._compact_spoken_text(text), start_ms=start_ms, end_ms=end_ms, cluster=cluster)
        seg = svc.add_segment(
            meeting_id,
            text,
            start_ms=start_ms,
            end_ms=end_ms,
            speaker_label=label,
            speaker_user_id=user_id,
            speaker_cluster_id=cluster,
            speaker_confidence=conf,
            source=TranscriptSource.REALTIME.value,
            punctuated_text=display,
            asr_state="final",
        )
        self._backfill_cluster_identity(
            app,
            meeting_id,
            cluster,
            label=label,
            user_id=user_id,
            confidence=conf,
        )
        session["last_final_seg_id"] = seg.id
        self._remember_caption(session, display)
        session["event_sequence"] = int(session.get("event_sequence") or 0) + 1
        broadcast_live(
            app,
            "transcript_final",
            meeting_id,
            transcript_final_payload(
                seg,
                text=display,
                speaker=label,
                utterance_id=int(session.get("utterance_id") or 0),
                turn_revision=int(session.get("turn_revision") or 0),
                sequence=int(session["event_sequence"]),
            ),
        )
        if defer_speaker_label:
            self._queue_speaker_label_refinement(
                app,
                meeting_id,
                seg.id,
                utt or pcm,
                speakers,
                session,
                duration_ms=duration_ms,
            )
        # Route a final exactly once.  In particular, do not cancel first and
        # then reconsider the same short acknowledgement as an unaddressed
        # follow-up.  This is the turn-state boundary between VAD/ASR and the
        # assistant: only semantic, duration-qualified speech may take over.
        if stop_display:
            # The spoken stop phrase ends the continuous dialogue even if its
            # VAD already interrupted playback before ASR finalized it.
            session["dialogue_active"] = False
            session["dialogue_last_activity"] = 0.0
            session["dialogue_last_assistant_at"] = 0.0
            session["dialogue_phase"] = "stopped"
            self._interrupt_ai_turn(app, meeting_id, session, reason="spoken_stop")
            session["dialogue_phase"] = "stopped"
            broadcast_live(app, "ai_status", meeting_id, {"state": "dialogue_stopped"})
        elif session.get("ai_turn_active"):
            decision = self._dialogue_policy(app).decide_active_turn(display, duration_ms=duration_ms)
            if decision.interrupts:
                self._interrupt_ai_turn(app, meeting_id, session, reason=f"voice_{decision.action}")
                if decision.starts_reply:
                    session["dialogue_last_activity"] = time.monotonic()
                    self._spawn_ai_turn(
                        app, meeting_id, display, session, follow_up=True,
                        transcript_start_ms=max(0, int(end_ms or start_ms or 0)),
                        transcript_segment_id=seg.id,
                    )
            else:
                self._resume_ai_playback(app, meeting_id, session, reason="backchannel")
                log.debug(
                    "ignored non-interrupting speech meeting=%s phase=%s text=%s",
                    meeting_id,
                    session.get("dialogue_phase"),
                    display[:40],
                )
        elif self._dialogue_accepts_follow_up(
            app, session, display, duration_ms=duration_ms
        ) and not wake.is_addressed(display):
            session["dialogue_last_activity"] = time.monotonic()
            self._spawn_ai_turn(
                app, meeting_id, display, session, follow_up=True,
                transcript_start_ms=max(0, int(end_ms or start_ms or 0)),
                transcript_segment_id=seg.id,
            )
        else:
            self._spawn_ai_turn(
                app, meeting_id, display, session,
                transcript_start_ms=max(0, int(end_ms or start_ms or 0)),
                transcript_segment_id=seg.id,
            )
        pending = session.setdefault("correction_pending", [])
        if not pending:
            session["correction_pending_since"] = time.time()
        pending.append({"id": seg.id, "text": text})
        self._queue_sensevoice_refinement(
            app,
            meeting_id,
            seg.id,
            utt or pcm,
            sample_rate=sample_rate,
            duration_ms=duration_ms,
        )
        self._queue_correction(app, meeting_id, session)
        return seg

    def _pcm_frame_spec(self, asr) -> tuple[int, int, int]:
        """(bytes_per_frame, sample_rate, frame_ms), always local 16 kHz PCM."""
        return BYTES_PER_FRAME, PCM_SAMPLE_RATE, FRAME_MS

    def _pcm_time_ms(self, offset: int, sample_rate: int | None = None) -> int:
        rate = int(sample_rate or PCM_SAMPLE_RATE)
        return int(offset / max(rate * 2, 1) * 1000)

    def _soft_endpoint(self, app, meeting_id: str, session: dict, *, end_ms: int) -> None:
        """Hold an ASR endpoint briefly so a natural pause can reopen it.

        FunASR's cache remains intact until the deadline.  If speech resumes,
        the same cache continues under a higher revision instead of creating a
        duplicate final transcript.
        """
        if session.get("pending_endpoint") is not None:
            return
        analyzer = get_smart_turn(app)
        complete = True
        probability: float | None = None
        inference_ms: float | None = None
        if analyzer is not None:
            try:
                result = analyzer.predict_pcm16(bytes(session.get("utt_pcm") or b""))
                complete, probability, inference_ms = result.complete, result.probability, result.inference_ms
            except Exception:
                log.warning("Smart Turn prediction failed meeting=%s", meeting_id, exc_info=True)
        grace_ms = 0 if analyzer is None else int(
            app.config.get("SMART_TURN_COMPLETE_REOPEN_MS", 700)
            if complete
            else app.config.get("SMART_TURN_INCOMPLETE_REOPEN_MS", 1800)
        )
        session["pending_endpoint"] = {
            "deadline": time.monotonic() + max(0, grace_ms) / 1000.0,
            "start_ms": int(session.get("utterance_start_ms") or end_ms),
            "end_ms": int(end_ms),
            "complete": complete,
        }
        session["speech_active"] = False
        session["silence_ms"] = 0
        payload = {
            "turn_id": int(session.get("utterance_id") or 0),
            "revision": int(session.get("turn_revision") or 0),
            "complete": complete,
            "reopen_ms": grace_ms,
        }
        if probability is not None:
            payload["probability"] = round(probability, 3)
        if inference_ms is not None:
            payload["inference_ms"] = round(inference_ms, 1)
        broadcast_live(app, "turn_endpoint", meeting_id, payload)
        log.info("soft endpoint meeting=%s complete=%s grace_ms=%s probability=%s", meeting_id, complete, grace_ms, probability)

    def _finalize_due_endpoint(self, app, meeting_id: str, session: dict) -> bool:
        pending = session.get("pending_endpoint")
        if not isinstance(pending, dict) or time.monotonic() < float(pending.get("deadline") or 0):
            return False
        session["pending_endpoint"] = None
        asr = session["asr"]
        asr.flush()
        self._drain_asr(
            app,
            meeting_id,
            session,
            b"",
            start_ms=int(pending.get("start_ms") or 0),
            end_ms=int(pending.get("end_ms") or 0),
        )
        # If ASR ultimately found no actionable speech (including a filtered
        # playback echo), a two-stage hold must release its cached PCM. Without
        # this terminal transition a browser can remain silently paused halfway
        # through an otherwise healthy answer.
        if session.get("ai_turn_active") and session.get("barge_pending"):
            self._resume_ai_playback(app, meeting_id, session, reason="no_actionable_final")
        session["utterance_ms"] = 0
        session["silence_ms"] = 0
        session.setdefault("punctuation", app.extensions["providers"].punctuation).reset()
        return True

    def _reopen_pending_endpoint(self, app, meeting_id: str, session: dict) -> bool:
        pending = session.get("pending_endpoint")
        if not isinstance(pending, dict):
            return False
        if time.monotonic() >= float(pending.get("deadline") or 0):
            self._finalize_due_endpoint(app, meeting_id, session)
            return False
        session["pending_endpoint"] = None
        session["turn_revision"] = int(session.get("turn_revision") or 0) + 1
        session["speech_active"] = True
        session["silence_ms"] = 0
        # A revision replaces the visible live row; this never creates a second
        # database final because ASR was not flushed while it was reopenable.
        session["last_stable_display"] = ""
        session["event_sequence"] = int(session.get("event_sequence") or 0) + 1
        broadcast_live(
            app,
            "transcript_reopen",
            meeting_id,
            {
                "utterance_id": int(session.get("utterance_id") or 0),
                "turn_revision": int(session["turn_revision"]),
                "sequence": int(session["event_sequence"]),
            },
        )
        log.info("reopened soft endpoint meeting=%s turn=%s revision=%s", meeting_id, session.get("utterance_id"), session.get("turn_revision"))
        return True

    def _flush_ended_sessions(self, app, active_ids: set[str]) -> None:
        """Promote leftover FunASR partials when a meeting leaves RECORDING/STOPPING.

        stop() often jumps STOPPING→PROCESSING in one HTTP request, so the active
        loop never sees STOPPING flush. Drain bag sessions that are no longer active
        so short (~10s) meetings still get finals.
        """
        bag = app.extensions.get("speech_sessions") or {}
        for mid in list(bag.keys()):
            if mid in active_ids:
                continue
            session = bag.get(mid)
            if not session:
                continue
            asr = session.get("asr")
            flush = getattr(asr, "flush", None)
            try:
                if callable(flush):
                    flush()
                # Pull any promoted finals into the DB once.
                get_final = getattr(asr, "get_final", None)
                if callable(get_final):
                    while True:
                        final = get_final()
                        if not final:
                            break
                        self._on_final(app, mid, final, b"", session, start_ms=0, end_ms=0)
                self._queue_correction(app, mid, session, force=True)
                speaker_executor = session.get("speaker_executor")
                if speaker_executor is not None:
                    speaker_executor.shutdown(wait=True)
                    session["speaker_executor"] = None
                log.info("flushed ended speech session meeting=%s", mid)
            except Exception:
                log.warning("flush ended session failed meeting=%s", mid, exc_info=True)
            bag.pop(mid, None)

    def _release_idle_asr(self, app) -> None:
        """Release the lazy local ASR instance only when no meeting is active."""
        bag = app.extensions.get("speech_sessions") or {}
        for session in bag.values():
            close = getattr(session.get("asr"), "close", None)
            if callable(close):
                close()

    def _process_pcm(self, app, meeting_id: str, cursor: dict) -> int:
        processing_started = time.monotonic()
        store = app.extensions["audio_store"]
        path = store.meeting_dir(meeting_id) / PCM_NAME
        session = self._session(app, meeting_id)
        self._consume_ai_control(app, meeting_id, session)
        self._finalize_due_endpoint(app, meeting_id, session)
        asr = session["asr"]
        dia = session["diarization"]
        vad = app.extensions["providers"].vad
        offset = int(cursor.get("pcm_offset") or 0)
        if offset < 0:
            offset = 0
        frame_bytes, sample_rate, frame_ms = self._pcm_frame_spec(asr)
        session["sample_rate"] = sample_rate
        if self._skip_live_fake_asr(app, asr):
            # FakeASR cycles a canned script on every ~300ms of any PCM, including
            # silence/fan. Advance the cursor so we do not backlog, but do not emit.
            if not path.exists():
                return 0
            with path.open("rb") as fh:
                fh.seek(offset)
                new = fh.read()
            n = len(new) - (len(new) % BYTES_PER_FRAME)
            cursor["pcm_offset"] = offset + n
            if n:
                log.info("skipped fake asr on live pcm meeting=%s bytes=%s", meeting_id, n)
            return n
        if not path.exists():
            t_ms = self._pcm_time_ms(offset, sample_rate)
            self._drain_asr(app, meeting_id, session, b"", start_ms=t_ms, end_ms=t_ms + frame_ms)
            self._publish_asr_metrics(
                app, meeting_id, session, cursor, queue_lag_ms=0,
                processing_ms=round((time.monotonic() - processing_started) * 1000),
            )
            return 0
        # Never materialize the entire, ever-growing meeting PCM file.  This
        # was a direct cause of captions slowing down as a meeting progressed.
        max_bytes = int(sample_rate * 2 * 0.5)
        max_bytes -= max_bytes % frame_bytes
        with path.open("rb") as fh:
            fh.seek(offset)
            new = fh.read(max_bytes + frame_bytes)
        if len(new) < frame_bytes:
            t_ms = self._pcm_time_ms(offset, sample_rate)
            self._drain_asr(app, meeting_id, session, b"", start_ms=t_ms, end_ms=t_ms + frame_ms)
            self._publish_asr_metrics(
                app, meeting_id, session, cursor,
                queue_lag_ms=self._pcm_time_ms(max(0, path.stat().st_size - offset), sample_rate),
                processing_ms=round((time.monotonic() - processing_started) * 1000),
            )
            return 0
        consumed = 0
        n = len(new) - (len(new) % frame_bytes)
        # ~500ms per tick: catch up without dumping megabytes into the S2S pool of 1.
        if n > max_bytes:
            n = max_bytes
        last_frame = b""
        for i in range(0, n, frame_bytes):
            frame = new[i : i + frame_bytes]
            last_frame = frame
            consumed += len(frame)
            cursor["pcm_offset"] = offset + consumed
            is_speech = vad.is_speech(frame)
            abs_off = offset + i
            start_ms = self._pcm_time_ms(abs_off, sample_rate)
            if is_speech:
                # First stage: after sustained voice onset, mute playback now.
                # The final ASR route below either resumes it for a short
                # acknowledgement or turns this into a genuine handoff.
                if session.get("ai_turn_active"):
                    session["barge_speech_ms"] = int(session.get("barge_speech_ms") or 0) + frame_ms
                    if int(session["barge_speech_ms"]) >= int(app.config.get("AI_BARGE_IN_HOLD_MS", 300)):
                        self._hold_ai_playback(app, meeting_id, session)
                if not session.get("speech_active"):
                    if not self._reopen_pending_endpoint(app, meeting_id, session):
                        self._refresh_meeting_asr_hotwords(meeting_id, session)
                        session["speech_active"] = True
                        session["utterance_id"] = int(session.get("utterance_id") or 0) + 1
                        session["turn_revision"] = 0
                        session["silence_ms"] = 0
                        session["utterance_ms"] = 0
                        session["utterance_start_ms"] = start_ms
                        session["utt_pcm"] = bytearray()
                        session["last_stable_display"] = ""
                        session.setdefault("punctuation", app.extensions["providers"].punctuation).reset()
                asr.feed_audio(frame)
                dia.feed_audio(frame)
                session.setdefault("utt_pcm", bytearray()).extend(frame)
                session["utterance_ms"] = int(session.get("utterance_ms") or 0) + frame_ms
                session["silence_ms"] = 0
                self._drain_asr(
                    app, meeting_id, session, frame, start_ms=start_ms, end_ms=start_ms + frame_ms
                )
                if int(session["utterance_ms"]) >= int(app.config["VAD_HARD_CUT_MS"]):
                    asr.flush()
                    self._drain_asr(app, meeting_id, session, frame, start_ms=session["utterance_start_ms"], end_ms=start_ms + frame_ms)
                    session["speech_active"] = False
                    session["pending_endpoint"] = None
                    session["utterance_ms"] = 0
                    session["silence_ms"] = 0
                    session.setdefault("punctuation", app.extensions["providers"].punctuation).reset()
            else:
                session["barge_speech_ms"] = 0
            if not is_speech and session.get("speech_active"):
                session["silence_ms"] = int(session.get("silence_ms") or 0) + frame_ms
                if int(session["silence_ms"]) >= int(app.config["VAD_ENDPOINT_SILENCE_MS"]):
                    self._soft_endpoint(app, meeting_id, session, end_ms=start_ms)
        t_ms = self._pcm_time_ms(offset + consumed, sample_rate)
        self._drain_asr(app, meeting_id, session, last_frame, start_ms=t_ms, end_ms=t_ms + frame_ms)
        self._finalize_due_endpoint(app, meeting_id, session)
        cursor["pcm_offset"] = offset + consumed
        self._publish_asr_metrics(
            app,
            meeting_id,
            session,
            cursor,
            queue_lag_ms=self._pcm_time_ms(max(0, path.stat().st_size - (offset + consumed)), sample_rate),
            processing_ms=round((time.monotonic() - processing_started) * 1000),
        )
        if consumed:
            log.info("processed meeting=%s pcm_bytes=%s", meeting_id, consumed)
        return consumed

    def _process_chunks(self, app, meeting_id: str, cursor: dict) -> int:
        last_seq = int(cursor.get("last_seq") if cursor.get("last_seq") is not None else -1)
        rows = (
            AudioChunk.query.filter(
                AudioChunk.meeting_id == meeting_id,
                AudioChunk.seq > last_seq,
            )
            .order_by(AudioChunk.seq.asc())
            .limit(20)
            .all()
        )
        if not rows:
            return 0
        session = self._session(app, meeting_id)
        asr = session["asr"]
        dia = session["diarization"]
        n = 0
        if self._skip_live_fake_asr(app, asr):
            cursor["last_seq"] = int(rows[-1].seq)
            log.info("skipped fake asr on live chunks meeting=%s last_seq=%s", meeting_id, cursor["last_seq"])
            return len(rows)
        for chunk in rows:
            pcm = self._pcm_for_chunk(app, chunk)
            if not pcm:
                continue
            asr.feed_audio(pcm)
            dia.feed_audio(pcm)
            asr.flush()
            final = asr.get_final()
            if final:
                start_ms = int(chunk.start_ms or 0)
                end_ms = start_ms + int(chunk.duration_ms or 5000)
                self._on_final(app, meeting_id, final, pcm, session, start_ms=start_ms, end_ms=end_ms)
            cursor["last_seq"] = int(chunk.seq)
            n += 1
            log.info("processed meeting=%s seq=%s", meeting_id, chunk.seq)
        return n

    def maybe_update_rolling_summary(self, app, meeting_id: str, cursor: dict, *, force: bool = False):
        n = TranscriptSegment.query.filter_by(meeting_id=meeting_id).count()
        if n < 1:
            return None
        # A first sentence is not a useful summary and, with a local 27B model,
        # used to stall the only real-time speech loop for several seconds.
        min_segments = int(app.config.get("ROLLING_SUMMARY_MIN_SEGMENTS", 3))
        if not app.config.get("TESTING") and not force and n < min_segments:
            return None
        # The rolling summary is valuable on the right-hand panel, but it is
        # never allowed to compete with a wake turn or audible reply for the
        # single local Qwen lane. It will be picked up on a later worker tick.
        session = self._session(app, meeting_id)
        if not force and (session.get("ai_turn_active") or session.get("barge_pending")):
            return None
        # Reserve the one local Qwen lane for a participant's spoken question
        # while a meeting is recording.  The final/post-meeting summary still
        # uses this same high-quality path with ``force=True``.
        if (
            not app.config.get("TESTING")
            and not force
            and not app.config.get("REALTIME_BACKGROUND_LLM_ENABLED", False)
        ):
            return None
        now = time.time()
        last = float(cursor.get("last_summary_at") or 0)
        if not force and last and (now - last) < SUMMARY_INTERVAL_S:
            return None
        if _wake_inflight.is_set():
            log.info("rolling summary deferred meeting=%s reason=wake_inflight", meeting_id)
            return None
        if not _llm_lock.acquire(blocking=False):
            log.info("rolling summary deferred meeting=%s reason=llm_busy", meeting_id)
            return None

        # Tests deliberately retain a synchronous result. Production executes
        # the expensive local LLM call out of the PCM/ASR loop so audio frames
        # continue being consumed at the configured 480-ms cadence.
        if not app.config.get("TESTING"):
            cursor["last_summary_at"] = now

            def _run() -> None:
                try:
                    with app.app_context():
                        self._generate_rolling_summary(app, meeting_id, now)
                except Exception:
                    log.warning("rolling summary failed meeting=%s", meeting_id, exc_info=True)
                finally:
                    _llm_lock.release()

            threading.Thread(target=_run, name=f"rolling-summary-{meeting_id[:8]}", daemon=True).start()
            return None
        try:
            state = self._generate_rolling_summary(app, meeting_id, now)
            cursor["last_summary_at"] = now
            return state
        finally:
            _llm_lock.release()

    @staticmethod
    def _generate_rolling_summary(app, meeting_id: str, now: float):
        segs = TranscriptService().list_for_meeting(meeting_id)
        transcript = attributed_transcript(segs)
        llm = app.extensions["providers"].llm
        prompt = (
            "请根据会议逐字稿输出 JSON，字段: summary, topics, decisions, action_items, "
            "risks, open_questions, speaker_views。speaker_views 是按人和议题归纳的数组，每项含 "
            "speaker_key、topic、explicit_stances、key_points、suggestions、concerns、open_questions、"
            "possible_tendencies。每一项内容都必须含 text 和 source_segment_ids，引用只能来自该 "
            "speaker_key 的 segment。明确立场只允许 position 为 支持、反对、保留、建议、未明确；"
            "没有明确说出的判断只能放 possible_tendencies，不能写成确定观点。不得将未验证或重叠 "
            "说话人改写为实名。不要编造原文没有的事实。\n\n" + transcript
        )
        # Ask the local OpenAI-compatible endpoint for actual JSON. A failed
        # parse returns an empty object rather than the test fixture summary,
        # so one bad background response cannot display another meeting's
        # decisions in the live panel.
        payload = parse_json_payload(llm.complete(prompt, json_mode=True), fallback={})
        if not str(payload.get("summary") or "").strip():
            log.warning("rolling summary returned no valid summary meeting=%s", meeting_id)
            return MeetingStateService().current(meeting_id)
        topics = payload.get("topics") or []
        current_topic = ""
        if isinstance(topics, list) and topics:
            first = topics[0]
            current_topic = first if isinstance(first, str) else str(first)
        elif payload.get("current_topic"):
            current_topic = str(payload["current_topic"])
        speaker_views = validate_speaker_views(payload, segs)
        patch = {
            "rolling_summary": payload.get("summary") or "",
            "current_topic": current_topic,
            "decisions": payload.get("decisions") or [],
            "action_items": payload.get("action_items") or [],
            "open_questions": payload.get("open_questions") or [],
            "risks": payload.get("risks") or [],
        }
        # An absent/invalid field must not erase the last evidence-backed view
        # while a local model is under load or returns malformed JSON.
        raw_speaker_views = payload.get("speaker_views")
        if isinstance(raw_speaker_views, list) and (speaker_views or not raw_speaker_views):
            patch["speaker_views"] = speaker_views
            state = MeetingStateService().apply_patch(
                meeting_id, patch, replace_fields={"speaker_views"}
            )
        else:
            state = MeetingStateService().apply_patch(meeting_id, patch)
        broadcast_live(
            app,
            "meeting_state_patch",
            meeting_id,
            {"meeting_id": meeting_id, "state": state.to_dict()},
        )
        return state

    def process_pending(self, app=None) -> dict:
        app = app or self.app
        store = app.extensions["audio_store"]
        meetings = Meeting.query.filter(Meeting.status.in_(ACTIVE_STATUSES)).all()
        stats = {"meetings": 0, "segments": 0, "pcm_bytes": 0, "chunks": 0}
        active_ids = {m.id for m in meetings}
        # Flush sessions whose meetings already left ACTIVE (short-test stop race).
        self._flush_ended_sessions(app, active_ids)
        if not meetings:
            self._release_idle_asr(app)
            return stats
        before = {
            m.id: TranscriptSegment.query.filter_by(meeting_id=m.id).count() for m in meetings
        }
        for meeting in meetings:
            stats["meetings"] += 1
            cursor = load_cursor(store, meeting.id)
            try:
                pcm_path = store.meeting_dir(meeting.id) / PCM_NAME
                pcm_size = pcm_path.stat().st_size if pcm_path.exists() else 0
                pcm_bytes = self._process_pcm(app, meeting.id, cursor)
                stats["pcm_bytes"] += pcm_bytes
                if meeting.status == MeetingStatus.STOPPING.value:
                    session = self._session(app, meeting.id)
                    # A deferred Smart Turn must not survive the explicit
                    # meeting-finalization flush, otherwise it can emit the
                    # same ASR cache a second time on a later worker tick.
                    session["pending_endpoint"] = None
                    flush = getattr(session["asr"], "flush", None)
                    if callable(flush):
                        flush()
                    _fb, _sr, _fm = self._pcm_frame_spec(session["asr"])
                    t_ms = self._pcm_time_ms(int(cursor.get("pcm_offset") or 0), _sr)
                    self._drain_asr(app, meeting.id, session, b"", start_ms=t_ms, end_ms=t_ms + _fm)
                    self._queue_correction(app, meeting.id, session, force=True)
                # Prefer PCM spool. Chunk fallback only when the worklet never wrote PCM.
                if pcm_size == 0 and pcm_bytes == 0:
                    stats["chunks"] += self._process_chunks(app, meeting.id, cursor)
                # Persist PCM offset before any LLM work so a 27B 502 cannot rewind ASR.
                save_cursor(store, meeting.id, cursor)
                try:
                    self.maybe_update_rolling_summary(app, meeting.id, cursor)
                except Exception:
                    log.warning("rolling summary failed meeting=%s", meeting.id, exc_info=True)
                    cursor["last_summary_at"] = time.time()
                save_cursor(store, meeting.id, cursor)
                after = TranscriptSegment.query.filter_by(meeting_id=meeting.id).count()
                stats["segments"] += max(0, after - before.get(meeting.id, 0))
            except Exception:
                log.exception("process meeting failed meeting=%s", meeting.id)
                try:
                    save_cursor(store, meeting.id, cursor)
                except Exception:
                    pass
        return stats


def tick(app) -> dict:
    """One worker iteration. Tests call this instead of the sleep loop."""
    return SpeechPipeline(app).process_pending(app)
