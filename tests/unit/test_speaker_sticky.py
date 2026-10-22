"""Sticky speaker: one human must not become Speaker 1 AND Speaker 2."""

from __future__ import annotations

from app.providers.diarization import EmbeddingClusterProvider, FakeDiarizationProvider
from app.realtime.echo import AI_SPEAKER
from app.services.speaker import StickySpeakerAssigner


def test_fake_diarization_does_not_cycle():
    dia = FakeDiarizationProvider(n_speakers=2)
    labels = []
    for _ in range(40):
        dia.feed_audio(b"\x00" * 3200)
        labels.append(dia.current_speakers()[0]["speaker"])
    assert set(labels) == {"Speaker 1"}


def test_embedding_cluster_stays_speaker1():
    dia = EmbeddingClusterProvider()
    for _ in range(50):
        dia.feed_audio(b"\x00" * 1920)
    speakers = dia.current_speakers()
    assert speakers[0]["speaker"] == "Speaker 1"
    assert speakers[0]["cluster_id"] == "0"


def test_sticky_assigner_single_human_never_opens_speaker2():
    a = StickySpeakerAssigner(sticky_s=45.0, min_new_ms=1000)
    labels = []
    now = 1_000_000.0
    for i in range(12):
        suggested = "Speaker 2" if i % 2 else "Speaker 1"
        out = a.assign(
            duration_ms=800 if i % 3 == 0 else 2400,
            now_s=now + i * 2.0,
            suggested_label=suggested,
            suggested_cluster="1" if i % 2 else "0",
        )
        labels.append(out["speaker_label"])
    assert labels == ["Speaker 1"] * 12


def test_sticky_assigner_ignores_ai_speaker_suggestion():
    a = StickySpeakerAssigner()
    out = a.assign(duration_ms=2000, now_s=1.0, suggested_label=AI_SPEAKER)
    assert out["speaker_label"] == "Speaker 1"
    assert out["speaker_label"] != AI_SPEAKER


def test_short_fragment_does_not_create_new_cluster():
    a = StickySpeakerAssigner(min_new_ms=1000)
    first = a.assign(duration_ms=2000, now_s=1.0, suggested_label="Speaker 1")
    second = a.assign(duration_ms=200, now_s=2.0, suggested_label="Speaker 2")
    assert first["speaker_label"] == "Speaker 1"
    assert second["speaker_label"] == "Speaker 1"


def test_real_voice_embeddings_open_a_second_speaker_but_rejoin_the_first():
    a = StickySpeakerAssigner(min_new_ms=1000, match_threshold=0.8)
    first = a.assign(duration_ms=1800, now_s=1.0, embedding=[1.0, 0.0])
    second = a.assign(duration_ms=1800, now_s=3.0, embedding=[0.0, 1.0])
    third = a.assign(duration_ms=1800, now_s=5.0, embedding=[0.98, 0.02])
    assert first["speaker_label"] == "Speaker 1"
    assert second["speaker_label"] == "Speaker 2"
    assert third["speaker_label"] == "Speaker 1"
