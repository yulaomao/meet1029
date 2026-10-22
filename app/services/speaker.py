"""Speaker identification via cosine similarity against enrolled profiles.

Low-confidence IDs stay as Speaker 1/2/3 — never forced to a named person.

Live captions also go through StickySpeakerAssigner so a single human is not
split into Speaker 1 / Speaker 2 by fake diarization cycling or short fragments.
"""

from __future__ import annotations

import math
import time

from app.models import SpeakerProfile
from app.realtime.echo import AI_SPEAKER


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(x * x for x in a[:n]))
    nb = math.sqrt(sum(x * x for x in b[:n]))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class SpeakerIDService:
    def __init__(self, high: float = 0.75, mid: float = 0.55):
        self.high = high
        self.mid = mid

    def identify(
        self,
        embedding: list[float],
        profiles: list[SpeakerProfile],
        cluster_index: int = 1,
    ) -> dict:
        """Return speaker_label, speaker_user_id, confidence.

        high -> auto name
        mid  -> candidate name but still generic label (not forced)
        low  -> Speaker N
        """
        best: SpeakerProfile | None = None
        best_score = -1.0
        for p in profiles or []:
            score = cosine(embedding, p.embedding or [])
            if score > best_score:
                best_score = score
                best = p
        generic = f"Speaker {cluster_index}"
        if best is None or best_score < self.mid:
            return {
                "speaker_label": generic,
                "speaker_user_id": None,
                "confidence": max(best_score, 0.0),
                "candidate": None,
            }
        if best_score < self.high:
            return {
                "speaker_label": generic,
                "speaker_user_id": None,
                "confidence": best_score,
                "candidate": best.display_name,
            }
        return {
            "speaker_label": best.display_name,
            "speaker_user_id": best.user_id,
            "confidence": best_score,
            "candidate": best.display_name,
        }


class StickySpeakerAssigner:
    """Online human speaker labels for live captions.

    A single-person meeting must stay on Speaker 1 (or the enrolled name).
    Do not invent Speaker 2 from silence gaps, <1s fragments, FakeDiarization
    cycling, or 成就精灵 TTS echo.
    """

    def __init__(
        self,
        sticky_s: float = 45.0,
        min_new_ms: int = 1000,
        match_threshold: float = 0.45,
        max_clusters: int = 8,
    ):
        self.sticky_s = sticky_s
        self.min_new_ms = min_new_ms
        self.match_threshold = match_threshold
        self.max_clusters = max(1, int(max_clusters))
        self.last_label = "Speaker 1"
        self.last_cluster = "0"
        self._history: list[tuple[float, str, str]] = []
        self._centroids: dict[str, list[float]] = {}

    def _prune(self, now_s: float) -> list[tuple[float, str, str]]:
        self._history = [(t, lab, cl) for t, lab, cl in self._history if now_s - t <= self.sticky_s]
        return self._history

    def assign(
        self,
        *,
        duration_ms: int = 0,
        now_s: float | None = None,
        suggested_label: str | None = None,
        suggested_cluster: str | None = None,
        embedding: list[float] | None = None,
    ) -> dict:
        now = time.time() if now_s is None else float(now_s)
        history = self._prune(now)
        suggested = (suggested_label or "").strip()
        if suggested == AI_SPEAKER:
            suggested = ""
        recent = {lab for _, lab, _ in history if lab and lab != AI_SPEAKER}

        if not history:
            label, cluster = "Speaker 1", "0"
        elif int(duration_ms or 0) < self.min_new_ms:
            label, cluster = self.last_label, self.last_cluster
        elif embedding:
            # Real CAM++ vectors, not a diarizer's speculative slot, decide
            # whether to open a new speaker.  This protects a one-person
            # meeting yet lets a genuine long turn form Speaker 2+.
            best_lab, best_score = None, -1.0
            for lab, vec in self._centroids.items():
                score = cosine(embedding, vec)
                if score > best_score:
                    best_score = score
                    best_lab = lab
            if best_lab and best_score >= self.match_threshold:
                label = best_lab
                cluster = next((cl for _, lab, cl in reversed(history) if lab == best_lab), self.last_cluster)
            elif len(self._centroids) < self.max_clusters:
                index = len(self._centroids) + 1
                label, cluster = f"Speaker {index}", str(index - 1)
            else:
                # Capacity reached: retain the nearest established speaker
                # rather than creating an unbounded stream of labels.
                label = best_lab or self.last_label
                cluster = next((cl for _, lab, cl in reversed(history) if lab == label), self.last_cluster)
        elif len(recent) <= 1:
            # Without a real voice vector we do not trust an upstream slot to
            # split a single person merely because of a silence gap.
            label, cluster = self.last_label, self.last_cluster
        else:
            label, cluster = self.last_label, self.last_cluster

        if embedding:
            prev = self._centroids.get(label)
            if prev:
                n = min(len(prev), len(embedding))
                self._centroids[label] = [(prev[i] + embedding[i]) / 2.0 for i in range(n)]
            else:
                self._centroids[label] = list(embedding)

        self.last_label = label
        self.last_cluster = cluster
        self._history.append((now, label, cluster))
        return {
            "speaker_label": label,
            "cluster_id": cluster,
            "confidence": 0.9,
            "suggested": suggested or None,
        }
