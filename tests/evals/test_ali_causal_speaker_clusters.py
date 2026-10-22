from scripts.evaluate_ali_causal_speaker_clusters import point_windows_to_spans


def test_point_window_predictions_become_non_overlapping_center_spans():
    windows = [
        {"start_s": 0.0, "end_s": 2.0, "raw_cluster": "Speaker 1"},
        {"start_s": 1.0, "end_s": 3.0, "raw_cluster": "Speaker 2"},
        {"start_s": 2.0, "end_s": 4.0, "raw_cluster": None},
    ]

    spans = point_windows_to_spans(windows, start_s=0.0, end_s=4.0)

    assert spans == [
        {"start_s": 0.0, "end_s": 1.5, "speaker": "Speaker 1"},
        {"start_s": 1.5, "end_s": 2.5, "speaker": "Speaker 2"},
    ]
