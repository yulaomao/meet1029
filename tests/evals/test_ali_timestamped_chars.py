from scripts.score_ali_far_timestamped_chars import (
    clean_reference_turns,
    score_turns,
    timestamped_characters,
    transfer_timestamps_to_live_text,
)


def test_timestamped_characters_maps_word_timestamps_to_global_text_times():
    items = timestamped_characters(
        {"words": ["你好", "A"], "timestamp": [[100, 200], [300, 400]]},
        clip_start_s=10.0,
    )

    assert [row["char"] for row in items] == ["你", "好", "A"]
    assert items[0]["start_s"] == 10.1
    assert items[-1]["end_s"] == 10.4


def test_timestamped_characters_rejects_ambiguous_text_timestamp_mapping():
    try:
        timestamped_characters({"text": "你好", "timestamp": [[0, 100]]}, clip_start_s=0)
    except ValueError as error:
        assert "cannot map model text to timestamps" in str(error)
    else:
        raise AssertionError("mismatched text and timestamps must fail closed")


def test_clean_reference_turns_excludes_cross_speaker_overlap():
    rows = [
        {"speaker_tier": "N_SPK1", "start_s": 0, "end_s": 1, "text": "你好"},
        {"speaker_tier": "N_SPK2", "start_s": 0.5, "end_s": 1.5, "text": "再见"},
        {"speaker_tier": "N_SPK1", "start_s": 2, "end_s": 3, "text": "测试"},
    ]

    selected = clean_reference_turns(rows, start_s=0, end_s=3, minimum_duration_s=0.5)

    assert len(selected) == 1
    assert selected[0]["reference"] == "测试"


def test_score_turns_assigns_only_fully_contained_characters():
    result = score_turns(
        [{"speaker_tier": "N_SPK1", "start_s": 1.0, "end_s": 2.0, "reference": "你好"}],
        [
            {"char": "你", "start_s": 1.1, "end_s": 1.2},
            {"char": "好", "start_s": 1.9, "end_s": 2.1},
        ],
    )

    assert result["turns"][0]["hypothesis"] == "你"
    assert result["character_weighted_cer"] == 0.5


def test_live_text_transfers_native_times_and_marks_insertions_as_estimated():
    aligned = [
        {"char": "你", "start_s": 1.0, "end_s": 1.1},
        {"char": "好", "start_s": 1.2, "end_s": 1.3},
    ]

    result = transfer_timestamps_to_live_text("你真好", aligned, clip_start_s=1, clip_end_s=2)

    assert [row["char"] for row in result] == ["你", "真", "好"]
    assert result[0]["time_estimated"] is False
    assert result[1]["time_estimated"] is True
    assert result[2]["time_estimated"] is False


def test_live_replacement_inherits_approximate_timestamp():
    aligned = [
        {"char": "你", "start_s": 1.0, "end_s": 1.1},
        {"char": "好", "start_s": 1.2, "end_s": 1.3},
    ]

    result = transfer_timestamps_to_live_text("你号", aligned, clip_start_s=1, clip_end_s=2)

    assert [row["char"] for row in result] == ["你", "号"]
    assert result[1]["start_s"] == 1.2
    assert result[1]["time_estimated"] is True
