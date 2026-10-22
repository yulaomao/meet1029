from scripts.score_ali_live_near_timestamped_chars import coalesce_final_versions


def test_coalesce_final_versions_keeps_first_and_latest_and_ignores_speaker_update():
    events = [
        {"event": "transcript_final", "data": {"id": "a", "turn_revision": 1, "start_ms": 1000, "end_ms": 2000, "text": "raw"}},
        {"event": "transcript_final", "data": {"id": "b", "turn_revision": 1, "start_ms": 3000, "end_ms": 4000, "text": "other"}},
        {"event": "transcript_final", "data": {"id": "a", "turn_revision": 2, "start_ms": 1000, "end_ms": 2000, "text": "refined"}},
        {"event": "transcript_final", "data": {"id": "a", "turn_revision": 3, "speaker_update": True, "start_ms": 1000, "end_ms": 2000, "text": "speaker-only"}},
    ]

    groups = coalesce_final_versions(events, start_s=0, end_s=5)

    assert len(groups) == 2
    assert [row["text"] for row in groups[0]] == ["raw", "refined"]
    assert [row["text"] for row in groups[1]] == ["other"]


def test_coalesce_final_versions_filters_out_of_window_rows():
    events = [
        {"event": "transcript_final", "data": {"id": "before", "start_ms": 0, "end_ms": 400}},
        {"event": "transcript_final", "data": {"id": "inside", "start_ms": 900, "end_ms": 1200}},
        {"event": "transcript_final", "data": {"id": "after", "start_ms": 2100, "end_ms": 2400}},
    ]

    groups = coalesce_final_versions(events, start_s=1, end_s=2)

    assert [[row["id"] for row in group] for group in groups] == [["inside"]]
