import numpy as np

from scripts.replay_ali_live_pipeline import (
    build_live_hypothesis,
    coalesce_final_events,
    resolve_replay_audio,
    streaming_asr_settings,
)
from scripts.build_ali_far_nonoverlap_replay import pad_to_processing_frame
from scripts.evaluate_ali_far_nonoverlap import resolve_far_audio, select_nonoverlap_turns


def test_final_correction_updates_replace_text_without_counting_another_segment():
    initial = {
        "event": "transcript_final",
        "wall_s": 2.0,
        "data": {"id": "segment-1", "start_ms": 0, "text": "旧识别"},
    }
    correction = {
        "event": "transcript_final",
        "wall_s": 2.4,
        "data": {"id": "segment-1", "start_ms": 0, "text": "修正后的文本"},
    }
    second = {
        "event": "transcript_final",
        "wall_s": 3.0,
        "data": {"id": "segment-2", "start_ms": 2000, "text": "第二句"},
    }

    first, latest, ordered = coalesce_final_events([initial, correction, second])

    assert len(first) == len(latest) == len(ordered) == 2
    assert first["segment-1"] is initial
    assert latest["segment-1"] is correction
    assert [item["data"]["text"] for item in ordered] == ["修正后的文本", "第二句"]


def test_live_hypothesis_applies_sensevoice_updates_by_transcript_id():
    events = [
        {
            "event": "transcript_final",
            "data": {"id": "segment-1", "start_ms": 0, "text": "原始识别"},
        },
        {
            "event": "transcript_final",
            "data": {"id": "segment-1", "start_ms": 0, "text": "修订文本"},
        },
        {
            "event": "transcript_final",
            "data": {"id": "segment-2", "start_ms": 1000, "text": "第二句"},
        },
    ]

    assert build_live_hypothesis(events) == "修订文本第二句"


def test_live_hypothesis_reopen_replaces_final_with_current_partial():
    events = [
        {
            "event": "transcript_final",
            "data": {"id": "segment-1", "start_ms": 0, "utterance_id": 7, "turn_revision": 0, "text": "旧句"},
        },
        {
            "event": "transcript_reopen",
            "data": {"utterance_id": 7, "turn_revision": 1},
        },
        {
            "event": "transcript_partial",
            "data": {"utterance_id": 7, "turn_revision": 1, "text": "更新中"},
        },
    ]

    assert build_live_hypothesis(events) == "更新中"


def test_streaming_asr_settings_report_effective_decoder_parameters():
    class Provider:
        model_id = "local/paraformer-online"
        device = "cuda:0"
        chunk_ms = 320
        stable_tail_chars = 1
        encoder_chunk_look_back = 2

    assert streaming_asr_settings(Provider()) == {
        "model": "local/paraformer-online",
        "device": "cuda:0",
        "chunk_ms": 320,
        "stable_tail_chars": 1,
        "encoder_chunk_look_back": 2,
    }


def test_far_accuracy_selection_excludes_cross_speaker_overlap():
    tiers = {
        "N_SPK1": [(0.0, 3.0, "说话人一"), (4.0, 7.0, "继续发言")],
        "N_SPK2": [(2.0, 3.5, "插入的重叠话语"), (8.0, 10.0, "另一句话")],
    }

    selected = select_nonoverlap_turns(
        tiers,
        start_s=0,
        end_s=10,
        minimum_duration_s=1.5,
    )

    assert [(row["speaker_tier"], row["reference"]) for row in selected] == [
        ("N_SPK1", "继续发言"),
        ("N_SPK2", "另一句话"),
    ]


def test_far_audio_resolver_keeps_actual_microphone_suffix(tmp_path):
    audio_dir = tmp_path / "audio_dir"
    audio_dir.mkdir()
    (audio_dir / "R8007_M8010_MS803.wav").touch()

    path, meeting = resolve_far_audio(audio_dir, "R8007_M8010_MS803")

    assert path.name == "R8007_M8010_MS803.wav"
    assert meeting == "R8007_M8010"


def test_live_replay_resolves_explicit_far_microphone_suffix(tmp_path):
    audio_dir = tmp_path / "Eval_Ali_far" / "audio_dir"
    audio_dir.mkdir(parents=True)
    (audio_dir / "R8007_M8010_MS803.wav").touch()

    path, meeting = resolve_replay_audio(tmp_path, "far", "R8007_M8010_MS803", "SPK8050")

    assert path.name == "R8007_M8010_MS803.wav"
    assert meeting == "R8007_M8010"


def test_live_replay_refuses_ambiguous_far_microphone(tmp_path):
    audio_dir = tmp_path / "Eval_Ali_far" / "audio_dir"
    audio_dir.mkdir(parents=True)
    (audio_dir / "R8007_M8010_MS801.wav").touch()
    (audio_dir / "R8007_M8010_MS803.wav").touch()

    import pytest

    with pytest.raises(ValueError, match="expected one far-field audio"):
        resolve_replay_audio(tmp_path, "far", "R8007_M8010", "SPK8050")


def test_generated_live_replay_audio_is_padded_to_service_frame_boundary():
    waveform = np.arange(1001, dtype=np.int16)

    padded = pad_to_processing_frame(waveform)

    assert len(padded) % 1600 == 0
    assert np.array_equal(padded[: len(waveform)], waveform)
    assert np.all(padded[len(waveform) :] == 0)
