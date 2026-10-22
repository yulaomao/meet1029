from __future__ import annotations

from scripts.diagnose_ali_final_alignment import analyze, read_final_events


def _final(start_ms: int, end_ms: int, text: str = "你好", segment_id: str = "seg-1", wall_s: float = 1.0):
    return {
        "event": "transcript_final",
        "wall_s": wall_s,
        "data": {
            "id": segment_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
        },
    }


def test_final_alignment_scores_only_complete_nonoverlapping_turns():
    rows = [{"speaker_tier": "N_SPK1", "start_s": 1.0, "end_s": 2.0, "text": "你好"}]

    result = analyze([_final(1000, 2000)], rows, start_s=0, end_s=3)

    assert result["strictly_scoreable_final_count"] == 1
    assert result["strict_reference_chars"] == 2
    assert result["strict_final_cer"] == 0.0


def test_final_alignment_rejects_overlap_and_boundary_crossing():
    rows = [
        {"speaker_tier": "N_SPK1", "start_s": 0.0, "end_s": 1.0, "text": "你好"},
        {"speaker_tier": "N_SPK2", "start_s": 0.5, "end_s": 1.5, "text": "再见"},
    ]

    result = analyze([_final(0, 1500, "你好再见")], rows, start_s=0, end_s=2)

    assert result["cross_speaker_overlap_region_count"] == 1
    assert result["strictly_scoreable_final_count"] == 0
    assert result["strict_final_cer"] is None
    assert result["final_rows"][0]["overlapping_reference_turns"] == 2


def test_final_alignment_rejects_reference_turn_crossing_final_boundary():
    rows = [{"speaker_tier": "N_SPK1", "start_s": 0.0, "end_s": 2.0, "text": "你好"}]

    result = analyze([_final(500, 2000)], rows, start_s=0, end_s=3)

    assert result["strictly_scoreable_final_count"] == 0
    assert result["final_rows"][0]["boundary_crossing_reference_turns"] == 1


def test_final_alignment_uses_latest_revision_for_segment(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"event":"transcript_final","wall_s":1,"data":{"id":"seg-1","start_ms":0,"end_ms":1000,"text":"旧稿"}}\n'
        '{"event":"transcript_final","wall_s":2,"data":{"id":"seg-1","start_ms":0,"end_ms":1000,"text":"新稿"}}\n',
        encoding="utf-8",
    )

    result = read_final_events(events, start_s=0, end_s=2)

    assert len(result) == 1
    assert result[0]["data"]["text"] == "新稿"


def test_final_alignment_applies_source_recording_time_offset(tmp_path):
    events = tmp_path / "events.jsonl"
    events.write_text(
        '{"event":"transcript_final","wall_s":1,"data":{"id":"seg-1","start_ms":1000,"end_ms":2000,"text":"你好"}}\n',
        encoding="utf-8",
    )

    result = read_final_events(events, start_s=60, end_s=62, time_offset_s=60)

    assert len(result) == 1
    assert result[0]["data"]["start_ms"] == 61000
    assert result[0]["data"]["end_ms"] == 62000
