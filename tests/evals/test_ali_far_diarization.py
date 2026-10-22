from scripts.score_ali_far_diarization import score_der


def _interval(start, end, speaker, speaker_key="speaker_tier"):
    return {"start_s": start, "end_s": end, speaker_key: speaker}


def test_der_maps_arbitrary_system_labels_to_reference_speakers():
    result = score_der(
        [_interval(0, 1, "A"), _interval(1, 2, "B")],
        [_interval(0, 1, "X", "speaker"), _interval(1, 2, "Y", "speaker")],
        start_s=0,
        end_s=2,
    )

    assert result["der"] == 0.0
    assert result["speaker_mapping"] == {"X": "A", "Y": "B"}


def test_der_counts_single_label_across_two_reference_speakers_as_confusion():
    result = score_der(
        [_interval(0, 1, "A"), _interval(1, 2, "B")],
        [_interval(0, 2, "X", "speaker")],
        start_s=0,
        end_s=2,
    )

    assert result["der"] == 0.5
    assert result["confusion_speaker_seconds"] == 1.0


def test_overlap_aware_der_accepts_correct_simultaneous_speakers():
    result = score_der(
        [_interval(0, 2, "A"), _interval(1, 2, "B")],
        [_interval(0, 2, "X", "speaker"), _interval(1, 2, "Y", "speaker")],
        start_s=0,
        end_s=2,
    )

    assert result["der"] == 0.0
    assert result["reference_speaker_seconds"] == 3.0


def test_der_counts_system_speech_during_reference_silence_as_false_alarm():
    result = score_der(
        [_interval(0, 1, "A")],
        [_interval(0, 2, "X", "speaker")],
        start_s=0,
        end_s=2,
    )

    assert result["false_alarm_speaker_seconds"] == 1.0
    assert result["der"] == 1.0
