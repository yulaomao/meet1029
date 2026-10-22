"""Deterministic final ASR provider used only by tests.

The local production final pass is :class:`FunASRFinalProvider`.
"""

from app.providers.base import FinalASRProvider


class FakeFinalASRProvider(FinalASRProvider):
    def __init__(self, text: str | None = None):
        self.text = text or "张工：这个针尖球心补偿目前看还是需要重新标定。李工：我觉得可以先固定旧算法作为对照。"

    def transcribe(self, wav_path: str, terminology: list[str] | None = None) -> dict:
        body = self.text
        for term in terminology or []:
            if term and term not in body:
                body += f" {term}"
        return {
            "text": body,
            "segments": [
                {"start_ms": 0, "end_ms": 4000, "speaker_label": "Speaker 1", "raw_text": "这个针尖球心补偿目前看还是需要重新标定。"},
                {"start_ms": 4000, "end_ms": 8000, "speaker_label": "Speaker 2", "raw_text": "我觉得可以先固定旧算法作为对照。"},
            ],
        }
