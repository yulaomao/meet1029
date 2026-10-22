"""Speech worker process: python -m app.workers.speech"""

from __future__ import annotations

import time

from app.config import get_config
from app.logging import configure_logging, get_logger
from app.services.speech_pipeline import tick
from app.web.factory import create_app


def main() -> None:
    configure_logging()
    log = get_logger("speech-worker")
    app = create_app(get_config())
    with app.app_context():
        registry = app.extensions["providers"]
        log.info("speech worker started backend=%s asr=%s", registry.speech_backend, type(registry.asr).__name__)
        if registry.speech_backend == "funasr":
            # Load local ASR/PNC before a meeting starts. The first model load
            # can take several seconds on CPU; paying it at worker startup keeps
            # the first spoken sentence responsive.
            warmup_started = time.monotonic()
            try:
                registry.asr._load()
                registry.punctuation._load_online()
                # Finals need the offline punctuation model as well. Loading
                # it on the first endpoint blocks the live speech worker and
                # can build tens of seconds of PCM backlog mid-meeting.
                registry.punctuation._load_final()
                # Sentence-end SenseVoice refinement is asynchronous, but
                # warming it here prevents the first completed utterance from
                # paying model-load/JIT latency.  Paraformer remains the
                # immediate live-caption source either way.
                if registry.sensevoice is not None:
                    registry.sensevoice._load()
                log.info("speech models warmed up in %.2fs", time.monotonic() - warmup_started)
            except Exception:
                log.exception("speech model warmup failed; lazy loading remains enabled")
        warm_speaker = getattr(registry.embedding, "warmup", None)
        if callable(warm_speaker):
            try:
                warmup_started = time.monotonic()
                warm_speaker()
                log.info("speaker model warmed up in %.2fs provider=%s", time.monotonic() - warmup_started, type(registry.embedding).__name__)
            except Exception:
                log.exception("speaker model warmup failed; lazy loading remains enabled")
        # Qwen3-TTS is local MLX/Metal or PyTorch/CUDA work. Warming one short
        # utterance prevents the first explicit AI question from paying model-load latency.
        warm_tts = getattr(registry.tts, "warmup", None)
        if callable(warm_tts):
            try:
                warmup_started = time.monotonic()
                warm_tts()
                log.info("tts warmed up in %.2fs provider=%s", time.monotonic() - warmup_started, type(registry.tts).__name__)
            except Exception:
                log.exception("tts warmup failed; lazy loading remains enabled")
    while True:
        try:
            with app.app_context():
                stats = tick(app)
                if stats.get("chunks") or stats.get("pcm_bytes") or stats.get("segments"):
                    log.info(
                        "tick meetings=%s segments=%s pcm_bytes=%s chunks=%s",
                        stats.get("meetings"),
                        stats.get("segments"),
                        stats.get("pcm_bytes"),
                        stats.get("chunks"),
                    )
        except Exception:
            log.exception("speech tick failed")
        time.sleep(0.05)


if __name__ == "__main__":
    main()
