from scripts.evaluate_ali_windowed_diarization import classify_similarities, clean_reference_label


def test_clean_reference_window_requires_dominant_single_speaker():
    rows = [
        {"speaker_tier": "N_SPK1", "start_s": 0.0, "end_s": 2.5},
        {"speaker_tier": "N_SPK2", "start_s": 2.0, "end_s": 3.0},
    ]

    label, coverage, overlap = clean_reference_label(rows, 0.0, 3.0)

    assert label is None
    assert coverage == 0.8333333333333334
    assert overlap is True


def test_clean_reference_window_labels_one_speaker_when_coverage_is_sufficient():
    rows = [{"speaker_tier": "N_SPK1", "start_s": 0.5, "end_s": 3.0}]

    label, coverage, overlap = clean_reference_label(rows, 0.0, 3.0)

    assert label == "N_SPK1"
    assert round(coverage, 4) == round(2.5 / 3.0, 4)
    assert overlap is False


def test_classification_rejects_low_confidence_and_accepts_clear_margin():
    rejected = classify_similarities(
        {"N_SPK1": 0.66, "N_SPK2": 0.58}, minimum_score=0.4, minimum_margin=0.12
    )
    accepted = classify_similarities(
        {"N_SPK1": 0.66, "N_SPK2": 0.50}, minimum_score=0.4, minimum_margin=0.12
    )

    assert rejected == (None, 0.66, 0.08000000000000007)
    assert accepted == ("N_SPK1", 0.66, 0.16000000000000003)
