from app.providers.embedding import CampPlusSpeakerEmbeddingProvider


def test_campplus_warmup_exercises_audio_embedding_without_loading_checkpoint(monkeypatch):
    provider = CampPlusSpeakerEmbeddingProvider(model="not-loaded-in-unit-test")
    calls = []
    monkeypatch.setattr(provider, "embed_audio", lambda pcm: calls.append(pcm) or [])

    provider.warmup()

    assert len(calls) == 1
    assert len(calls[0]) == 16000 * 2 * 2
