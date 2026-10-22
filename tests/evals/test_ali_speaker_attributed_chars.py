from scripts.evaluate_ali_speaker_attributed_chars import (
    label_for_span,
    reference_label_at,
    score_characters,
)


def test_label_for_span_uses_overlap_weighted_window_votes():
    windows = [
        {"start_s": 0.0, "end_s": 1.0, "predicted": "A"},
        {"start_s": 0.5, "end_s": 1.5, "predicted": "A"},
        {"start_s": 1.0, "end_s": 2.0, "predicted": "B"},
    ]

    label, coverage, vote_share = label_for_span(0.75, 1.25, windows)

    assert label == "A"
    assert coverage == 1.0
    assert vote_share == 0.75


def test_label_for_span_rejects_low_window_coverage_or_conflicting_votes():
    low_coverage, _, _ = label_for_span(
        0.0, 1.0, [{"start_s": 0.0, "end_s": 0.4, "predicted": "A"}]
    )
    conflict, _, _ = label_for_span(
        0.0, 1.0,
        [
            {"start_s": 0.0, "end_s": 1.0, "predicted": "A"},
            {"start_s": 0.0, "end_s": 1.0, "predicted": "B"},
        ],
    )

    assert low_coverage is None
    assert conflict is None


def test_center_assignment_does_not_extend_a_future_window_label_backwards():
    windows = [
        {"start_s": 0.0, "end_s": 3.0, "predicted": "A"},
        {"start_s": 1.5, "end_s": 4.5, "predicted": None},
        {"start_s": 3.0, "end_s": 6.0, "predicted": "B"},
    ]

    before_unknown = label_for_span(0.9, 1.0, windows, assignment_mode="center")
    at_unknown = label_for_span(2.9, 3.0, windows, assignment_mode="center")

    assert before_unknown[0] == "A"
    assert at_unknown[0] is None


def test_reference_label_is_unknown_during_overlap_and_metrics_split_native_times():
    turns = [
        {"speaker_tier": "A", "start_s": 0.0, "end_s": 2.0},
        {"speaker_tier": "B", "start_s": 1.0, "end_s": 3.0},
    ]
    assert reference_label_at(0.5, turns) == "A"
    assert reference_label_at(1.5, turns) is None

    result = score_characters(
        [
            {"char": "甲", "start_s": 0.2, "end_s": 0.3, "time_estimated": False},
            {"char": "乙", "start_s": 0.4, "end_s": 0.5, "time_estimated": True},
        ],
        [{"speaker_tier": "A", "start_s": 0.0, "end_s": 1.0}],
        [{"start_s": 0.0, "end_s": 1.0, "predicted": "A"}],
        minimum_coverage=0.6,
        minimum_vote_share=0.65,
    )

    assert result["all_timestamp_transfer"]["accuracy_when_labeled"] == 1.0
    assert result["native_timestamps_only"]["speaker_labeled_chars"] == 1
    assert result["estimated_timestamps_only"]["speaker_labeled_chars"] == 1
