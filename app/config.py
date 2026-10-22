"""Application configuration.

Production database is PostgreSQL + pgvector. Tests may override DATABASE_URL
or leave it empty so tests/conftest.py can use a SQLite test double.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[1]
# Tests set FLASK_ENV=testing in conftest; do not let a local .env leak into pytest.
if os.environ.get("FLASK_ENV", "").lower() not in {"test", "testing"}:
    load_dotenv(_ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://meeting:meeting@127.0.0.1:5432/meeting_ai",
    )
    WTF_CSRF_ENABLED = True
    TIMEZONE = os.environ.get("TIMEZONE", "Asia/Shanghai")
    DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()

    # Imported files are processed locally by a separate worker.  The browser
    # streams to disk, so this limit guards disk use rather than RAM use.
    IMPORT_MAX_BYTES = int(os.environ.get("IMPORT_MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
    IMPORT_MAX_DURATION_SECONDS = int(os.environ.get("IMPORT_MAX_DURATION_SECONDS", str(8 * 60 * 60)))
    IMPORT_WORKER_POLL_SECONDS = float(os.environ.get("IMPORT_WORKER_POLL_SECONDS", "1.0"))
    IMPORT_FFMPEG_TIMEOUT_SECONDS = int(os.environ.get("IMPORT_FFMPEG_TIMEOUT_SECONDS", "7200"))
    IMPORT_SPEAKER_CLUSTER_THRESHOLD = float(os.environ.get("IMPORT_SPEAKER_CLUSTER_THRESHOLD", "0.58"))
    IMPORT_SPEAKER_SHORT_GAP_MS = int(os.environ.get("IMPORT_SPEAKER_SHORT_GAP_MS", "3000"))
    IMPORT_MAX_SPEAKERS = int(os.environ.get("IMPORT_MAX_SPEAKERS", "8"))
    MAX_CONTENT_LENGTH = IMPORT_MAX_BYTES

    # Production is local-only. Tests override both backends to fake in TestConfig.
    SPEECH_BACKEND = os.environ.get("SPEECH_BACKEND", "funasr")  # fake|funasr
    LLM_BACKEND = os.environ.get("LLM_BACKEND", "qwen38")  # fake|qwen38
    FUNASR_STREAMING_MODEL = os.environ.get("FUNASR_STREAMING_MODEL", "funasr/paraformer-zh-streaming")
    FUNASR_FINAL_MODEL = os.environ.get("FUNASR_FINAL_MODEL", "paraformer-zh")
    FUNASR_VAD_MODEL = os.environ.get("FUNASR_VAD_MODEL", "fsmn-vad")
    FUNASR_ONLINE_PUNC_MODEL = os.environ.get(
        "FUNASR_ONLINE_PUNC_MODEL",
        "iic/punc_ct-transformer_zh-cn-common-vad_realtime-vocab272727",
    )
    FUNASR_FINAL_PUNC_MODEL = os.environ.get(
        "FUNASR_FINAL_PUNC_MODEL",
        "iic/punc_ct-transformer_cn-en-common-vocab471067-large",
    )
    # 480 ms is Paraformer's documented [0, 8, 4] streaming stride.  It is
    # short enough for live captions while leaving useful acoustic context.
    FUNASR_CHUNK_MS = int(os.environ.get("FUNASR_CHUNK_MS", "480"))
    FUNASR_STABLE_TAIL_CHARS = int(os.environ.get("FUNASR_STABLE_TAIL_CHARS", "1"))
    FUNASR_ENCODER_LOOK_BACK = int(os.environ.get("FUNASR_ENCODER_LOOK_BACK", "4"))
    FUNASR_DEVICE = os.environ.get("FUNASR_DEVICE", "cpu")
    # SenseVoice is a sentence-end, offline refiner. Paraformer remains the
    # only source of low-latency partial captions.
    SENSEVOICE_REFINEMENT_ENABLED = _bool("SENSEVOICE_REFINEMENT_ENABLED", False)
    SENSEVOICE_MODEL = os.environ.get("SENSEVOICE_MODEL", "iic/SenseVoiceSmall")
    SENSEVOICE_VAD_MODEL = os.environ.get("SENSEVOICE_VAD_MODEL", "")
    SENSEVOICE_DEVICE = os.environ.get("SENSEVOICE_DEVICE", "")
    SENSEVOICE_BATCH_SIZE_S = int(os.environ.get("SENSEVOICE_BATCH_SIZE_S", "60"))
    SENSEVOICE_MIN_UTTERANCE_MS = int(os.environ.get("SENSEVOICE_MIN_UTTERANCE_MS", "700"))
    SENSEVOICE_MAX_UTTERANCE_MS = int(os.environ.get("SENSEVOICE_MAX_UTTERANCE_MS", "30000"))
    SENSEVOICE_MIN_SIMILARITY = float(os.environ.get("SENSEVOICE_MIN_SIMILARITY", "0.8"))
    # A VAD end is only a soft endpoint when Smart Turn is enabled.  The
    # classifier/reopen grace decides when it may become a committed turn.
    VAD_ENDPOINT_SILENCE_MS = int(os.environ.get("VAD_ENDPOINT_SILENCE_MS", "250"))
    VAD_HARD_CUT_MS = int(os.environ.get("VAD_HARD_CUT_MS", "20000"))
    VAD_ENERGY_THRESHOLD = float(os.environ.get("VAD_ENERGY_THRESHOLD", "50"))
    SMART_TURN_ENABLED = _bool("SMART_TURN_ENABLED", True)
    SMART_TURN_MODEL_PATH = os.environ.get("SMART_TURN_MODEL_PATH", "")
    SMART_TURN_THRESHOLD = float(os.environ.get("SMART_TURN_THRESHOLD", "0.5"))
    SMART_TURN_COMPLETE_REOPEN_MS = int(os.environ.get("SMART_TURN_COMPLETE_REOPEN_MS", "700"))
    SMART_TURN_INCOMPLETE_REOPEN_MS = int(os.environ.get("SMART_TURN_INCOMPLETE_REOPEN_MS", "700"))
    SMART_TURN_INCOMPLETE_DELAY_MS = int(os.environ.get("SMART_TURN_INCOMPLETE_DELAY_MS", "500"))
    SMART_TURN_CPU_COUNT = int(os.environ.get("SMART_TURN_CPU_COUNT", "1"))

    LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://127.0.0.1:8000/v1")
    LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "mtplx-qwen38-27b-optimized-quality")
    LOCAL_LLM_API_KEY = os.environ.get("LOCAL_LLM_API_KEY", "not-needed")
    LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "90"))
    LLM_MAX_CONCURRENCY = int(os.environ.get("LLM_MAX_CONCURRENCY", "1"))
    # Interactive turns have their own 3k-token ceiling.  Background JSON
    # work must stay bounded because the local gateway is intentionally
    # single-lane and cannot preempt a long summary for a spoken question.
    LLM_BACKGROUND_MAX_TOKENS = int(os.environ.get("LLM_BACKGROUND_MAX_TOKENS", "3072"))
    # Keep the right-hand live summary useful by default. The speech pipeline
    # yields this low-priority work whenever an interactive AI turn is active.
    REALTIME_BACKGROUND_LLM_ENABLED = _bool("REALTIME_BACKGROUND_LLM_ENABLED", True)
    CORRECTION_INTERVAL_SECONDS = int(os.environ.get("CORRECTION_INTERVAL_SECONDS", "30"))
    CORRECTION_BATCH_CHARS = int(os.environ.get("CORRECTION_BATCH_CHARS", "300"))
    CORRECTION_CONTEXT_LINES = int(os.environ.get("CORRECTION_CONTEXT_LINES", "3"))
    # Corrected text is always stored beside immutable raw ASR.  This threshold
    # keeps the more capable contextual editor from turning a transcript into a
    # summary while still allowing several real ASR repairs in one sentence.
    CORRECTION_MIN_SIMILARITY = float(os.environ.get("CORRECTION_MIN_SIMILARITY", "0.55"))
    # Only assistant-directed turns pay for a one-line contextual repair before
    # Qwen answers. General captions continue through the batched path.
    AI_CONTEXT_CORRECTION_ENABLED = _bool("AI_CONTEXT_CORRECTION_ENABLED", True)
    FINAL_CORRECTION_WAIT_SECONDS = int(os.environ.get("FINAL_CORRECTION_WAIT_SECONDS", "45"))
    ROLLING_SUMMARY_MIN_SEGMENTS = int(os.environ.get("ROLLING_SUMMARY_MIN_SEGMENTS", "3"))

    TTS_BACKEND = os.environ.get("TTS_BACKEND", "macos")
    TTS_MLX_MODEL = os.environ.get("TTS_MLX_MODEL", "")
    TTS_MLX_VOICE = os.environ.get("TTS_MLX_VOICE", "aiden")
    # Do not split a Chinese utterance too eagerly: very short independent TTS
    # requests make prosody and perceived accent jump between chunks.
    AI_TTS_EARLY_CHARS = int(os.environ.get("AI_TTS_EARLY_CHARS", "36"))
    AI_TTS_FOLLOWING_CHARS = int(os.environ.get("AI_TTS_FOLLOWING_CHARS", "72"))
    # Mirror speech-to-speech's stream_batch_sentences default: emit complete
    # LLM sentence chunks promptly, then coalesce three adjacent chunks into a
    # stable CustomVoice synthesis request.  It is a queueing policy, never a
    # "wait for the complete answer" switch.
    AI_TTS_STREAM_BATCH_SENTENCES = int(os.environ.get("AI_TTS_STREAM_BATCH_SENTENCES", "3"))
    AI_TTS_UNIT_MAX_CHARS = int(os.environ.get("AI_TTS_UNIT_MAX_CHARS", "360"))
    # These are audible-answer limits, not LLM output limits.  Do not cut a
    # normal, already-generated answer in half: the stop button/barge-in are
    # the controls for an unusually long spoken response.
    AI_TTS_DEFAULT_MAX_CHARS = int(os.environ.get("AI_TTS_DEFAULT_MAX_CHARS", "1000"))
    AI_TTS_DETAILED_MAX_CHARS = int(os.environ.get("AI_TTS_DETAILED_MAX_CHARS", "2000"))
    AI_TTS_HARD_MAX_CHARS = int(os.environ.get("AI_TTS_HARD_MAX_CHARS", "2000"))
    # Qwen emits 512-sample codec blocks.  Group four for local transport so
    # a long answer does not create thousands of HTTP/socket broadcasts.
    AI_TTS_TRANSPORT_BLOCK_SAMPLES = int(os.environ.get("AI_TTS_TRANSPORT_BLOCK_SAMPLES", "2048"))
    # Qwen3-TTS CustomVoice is naturally deliberate in Mandarin. A modest
    # output-only rate keeps it conversational without switching voice/profile.
    AI_TTS_PLAYBACK_RATE = min(1.60, max(0.9, float(os.environ.get("AI_TTS_PLAYBACK_RATE", "1.30"))))
    # A CustomVoice model may choose a deliberately slow prosody for a very
    # short standalone reply. Keep the configured rate as the normal rate,
    # but permit a bounded correction when its measured duration is clearly
    # slower than conversational Mandarin.
    AI_TTS_MAX_PLAYBACK_RATE = min(1.60, max(0.9, float(os.environ.get("AI_TTS_MAX_PLAYBACK_RATE", "1.60"))))
    AI_TTS_SHORT_REPLY_CHARS = max(4, int(os.environ.get("AI_TTS_SHORT_REPLY_CHARS", "28")))
    AI_TTS_SHORT_TARGET_CHARS_PER_SECOND = min(
        8.0, max(2.5, float(os.environ.get("AI_TTS_SHORT_TARGET_CHARS_PER_SECOND", "5.0")))
    )
    # A coalescing deadline, not an end-of-response wait. It prevents a brief
    # answer from waiting for four complete sentences before speaking.
    AI_TTS_STREAM_BATCH_WAIT_MS = min(1000, max(80, int(os.environ.get("AI_TTS_STREAM_BATCH_WAIT_MS", "220"))))
    # A sound alone is not a barge-in.  The worker waits for a final ASR result
    # and checks it for meaningful content before cancelling speech.
    # Semantic barge-in: acknowledgements never interrupt.  A normal handoff
    # requires a little more speech than a terse, recognized control request.
    AI_BARGE_IN_MIN_CHARS = int(os.environ.get("AI_BARGE_IN_MIN_CHARS", "6"))
    AI_BARGE_IN_MIN_SPEECH_MS = int(os.environ.get("AI_BARGE_IN_MIN_SPEECH_MS", "700"))
    AI_BARGE_IN_HOLD_MS = int(os.environ.get("AI_BARGE_IN_HOLD_MS", "650"))
    AI_BARGE_IN_SHORT_COMMAND_CHARS = int(os.environ.get("AI_BARGE_IN_SHORT_COMMAND_CHARS", "3"))
    AI_BARGE_IN_SHORT_COMMAND_MS = int(os.environ.get("AI_BARGE_IN_SHORT_COMMAND_MS", "450"))
    AI_DIALOGUE_MIN_FOLLOW_UP_CHARS = int(os.environ.get("AI_DIALOGUE_MIN_FOLLOW_UP_CHARS", "4"))
    AI_DIALOGUE_MIN_FOLLOW_UP_MS = int(os.environ.get("AI_DIALOGUE_MIN_FOLLOW_UP_MS", "550"))
    AI_DIALOGUE_FREE_FOLLOW_UP_SECONDS = int(os.environ.get("AI_DIALOGUE_FREE_FOLLOW_UP_SECONDS", "0"))
    AI_RESPONSE_MAX_TOKENS = int(os.environ.get("AI_RESPONSE_MAX_TOKENS", "3072"))
    LLM_INTERACTIVE_FIRST_TOKEN_TIMEOUT_SECONDS = float(os.environ.get("LLM_INTERACTIVE_FIRST_TOKEN_TIMEOUT_SECONDS", "12"))
    LLM_INTERACTIVE_STREAM_ATTEMPTS = int(os.environ.get("LLM_INTERACTIVE_STREAM_ATTEMPTS", "2"))
    # 0 disables idle expiry: a user-invoked dialogue ends only through an
    # explicit stop command or the visible Stop button.
    AI_DIALOGUE_IDLE_SECONDS = int(os.environ.get("AI_DIALOGUE_IDLE_SECONDS", "0"))

    SPEAKER_HIGH_CONFIDENCE = float(os.environ.get("SPEAKER_HIGH_CONFIDENCE", "0.68"))
    SPEAKER_MID_CONFIDENCE = float(os.environ.get("SPEAKER_MID_CONFIDENCE", "0.55"))
    SPEAKER_ID_MIN_MS = int(os.environ.get("SPEAKER_ID_MIN_MS", "1500"))
    SPEAKER_CLUSTER_MATCH_THRESHOLD = float(os.environ.get("SPEAKER_CLUSTER_MATCH_THRESHOLD", "0.45"))
    # A wake phrase is context-critical: identify its owner before Qwen sees
    # the message, while keeping ordinary short acknowledgements conservative.
    AI_CONTEXT_SPEAKER_ID_MIN_MS = int(os.environ.get("AI_CONTEXT_SPEAKER_ID_MIN_MS", "700"))

    HOST_TRANSFER_MAX_GAP_MS = int(os.environ.get("HOST_TRANSFER_MAX_GAP_MS", "2000"))
    CHUNK_TIMESLICE_MS = int(os.environ.get("CHUNK_TIMESLICE_MS", "5000"))
    PCM_FRAME_MS = int(os.environ.get("PCM_FRAME_MS", "100"))
    PCM_SAMPLE_RATE = int(os.environ.get("PCM_SAMPLE_RATE", "16000"))

    SOCKETIO_ASYNC_MODE = "threading"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # A phone browser only exposes microphone capture on a secure origin.  A
    # local CA/certificate pair is deliberately opt-in so command-line and
    # test deployments can continue to use ordinary HTTP.
    TLS_CERT_FILE = os.environ.get("TLS_CERT_FILE", "").strip()
    TLS_KEY_FILE = os.environ.get("TLS_KEY_FILE", "").strip()
    TLS_CA_CERT_FILE = os.environ.get("TLS_CA_CERT_FILE", "").strip()
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", bool(TLS_CERT_FILE))
    PREFERRED_URL_SCHEME = "https" if TLS_CERT_FILE else "http"

    # Worker → web live broadcast (loopback only; no Redis).
    INTERNAL_LIVE_HOST = os.environ.get("INTERNAL_LIVE_HOST", "127.0.0.1")
    INTERNAL_LIVE_PORT = int(os.environ.get("INTERNAL_LIVE_PORT") or os.environ.get("PORT") or "5000")
    INTERNAL_LIVE_SCHEME = os.environ.get("INTERNAL_LIVE_SCHEME", "http").strip().lower()
    # Point this to the local CA PEM when the web process uses HTTPS.  httpx
    # then verifies the loopback connection instead of silently disabling TLS
    # validation in the worker.
    INTERNAL_LIVE_CA_FILE = os.environ.get("INTERNAL_LIVE_CA_FILE", "").strip()


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    # Never reuse DATABASE_URL here: it is the production/local-service
    # connection and pytest tears schemas down after each fixture.
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite://")
    LLM_BACKEND = "fake"
    SPEECH_BACKEND = "fake"
    SECRET_KEY = "test-secret"
    SERVER_NAME = "localhost"


class DevelopmentConfig(Config):
    DEBUG = _bool("DEBUG", True)
    WTF_CSRF_ENABLED = True


def get_config(name: str | None = None) -> type[Config]:
    env = (name or os.environ.get("FLASK_ENV") or "development").lower()
    if env in {"test", "testing"}:
        return TestConfig
    return DevelopmentConfig
