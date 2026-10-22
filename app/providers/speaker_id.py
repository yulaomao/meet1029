from __future__ import annotations

from typing import Any

from app.providers.base import SpeakerIDProvider
from app.services.speaker import SpeakerIDService


class CosineSpeakerIDProvider(SpeakerIDProvider):
    def __init__(self, high: float = 0.75, mid: float = 0.55):
        self.svc = SpeakerIDService(high=high, mid=mid)

    def identify(self, embedding: list[float], profiles: list[Any]) -> dict:
        return self.svc.identify(embedding, profiles)
