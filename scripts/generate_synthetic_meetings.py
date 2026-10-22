#!/usr/bin/env python3
"""Generate small synthetic 16 kHz mono meetings (Chinese, 2-4 speakers)."""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "testdata"


SCRIPTS = [
    {
        "id": "syn-001",
        "title": "膝关节导航技术讨论",
        "speakers": ["张工", "李工"],
        "turns": [
            (0, "张工", "这个针尖球心补偿目前看还是需要重新标定。"),
            (1, "李工", "我觉得可以先固定旧算法作为对照。"),
            (2, "张工", "AI，你怎么看这个方案？"),
            (3, "李工", "那我们明天采集五组数据。"),
        ],
        "terms": ["针尖球心补偿", "FusionTrack"],
    },
    {
        "id": "syn-002",
        "title": "电源与EtherCAT评审",
        "speakers": ["王工", "赵工", "张工"],
        "turns": [
            (0, "王工", "EtherCAT 总线抖动还是偏大。"),
            (1, "赵工", "电源是二百二十伏五安。"),
            (2, "张工", "助手，总结一下现在的结论。"),
        ],
        "terms": ["EtherCAT"],
    },
    {
        "id": "syn-003",
        "title": "股骨远端截骨讨论",
        "speakers": ["张工", "李工", "王工", "赵工"],
        "turns": [
            (0, "张工", "股骨远端截骨角度需要再确认。"),
            (1, "李工", "胫骨平台的球心标定已经完成。"),
            (2, "王工", "SlicerIGT 配准误差在零点一五毫米。"),
            (3, "赵工", "我们现在讨论AI算法优化，先不点名助手。"),
        ],
        "terms": ["股骨远端", "胫骨平台", "SlicerIGT"],
    },
    {
        "id": "golden-med-001",
        "title": "骨科导航内部黄金集甲",
        "speakers": ["张工", "李工"],
        "turns": [
            (0, "张工", "针尖球心补偿验证必须包含五组数据。"),
            (1, "李工", "不采用方案A，继续用旧算法对照。"),
        ],
        "terms": ["针尖球心补偿"],
        "golden": True,
    },
    {
        "id": "golden-med-002",
        "title": "骨科导航内部黄金集乙",
        "speakers": ["王工", "张工"],
        "turns": [
            (0, "王工", "Atracsys FusionTrack 五百的延迟可接受。"),
            (1, "张工", "球心漂移原因还没有确认。"),
        ],
        "terms": ["Atracsys", "FusionTrack"],
        "golden": True,
    },
]


def write_tone_wav(path: Path, duration_s: float = 1.0, freq: float = 220.0) -> None:
    rate = 16000
    n = int(rate * duration_s)
    samples = [int(8000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack("<" + "h" * n, *samples))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    meetings = []
    for spec in SCRIPTS:
        folder = OUT / spec["id"]
        folder.mkdir(parents=True, exist_ok=True)
        wavs = []
        for i, (_t, _spk, _text) in enumerate(spec["turns"]):
            p = folder / f"utt_{i}.wav"
            write_tone_wav(p, duration_s=0.6, freq=180 + i * 40)
            wavs.append(str(p.relative_to(ROOT)))
        master = folder / "meeting.wav"
        write_tone_wav(master, duration_s=max(1.5, 0.5 * len(spec["turns"])), freq=200)
        transcript = {
            "id": spec["id"],
            "title": spec["title"],
            "speakers": spec["speakers"],
            "segments": [
                {"start_ms": i * 600, "end_ms": i * 600 + 500, "speaker": spk, "text": text}
                for i, (_t, spk, text) in enumerate(spec["turns"])
            ],
            "terms": spec["terms"],
        }
        (folder / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
        meetings.append(
            {
                "id": spec["id"],
                "title": spec["title"],
                "wav": str(master.relative_to(ROOT)),
                "transcript": str((folder / "transcript.json").relative_to(ROOT)),
                "golden": bool(spec.get("golden")),
            }
        )
    embeddings = {
        "张工": [1.0, 0.0, 0.0, 0.0],
        "李工": [0.0, 1.0, 0.0, 0.0],
        "王工": [0.0, 0.0, 1.0, 0.0],
        "赵工": [0.0, 0.0, 0.0, 1.0],
    }
    (OUT / "speaker_embeddings.json").write_text(json.dumps(embeddings, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "version": "0.1",
        "note": "Synthetic small fixtures. Do not download full AISHELL-4 / WenetSpeech.",
        "meetings": meetings,
        "speaker_embeddings": str((OUT / "speaker_embeddings.json").relative_to(ROOT)),
        "private_release": "tests/testdata/private_release/expected.json",
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    priv = OUT / "private_release"
    priv.mkdir(exist_ok=True)
    (priv / "expected.json").write_text(
        json.dumps(
            {
                "id": "private-001",
                "note": "Blind set. App code must not special-case this file.",
                "summary_contains": ["针尖"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # tiny wav+text for WER/CER
    cer_dir = OUT / "cer"
    cer_dir.mkdir(exist_ok=True)
    write_tone_wav(cer_dir / "ref.wav", 0.4, 300)
    (cer_dir / "ref.txt").write_text("针尖球心补偿需要重新标定", encoding="utf-8")
    (cer_dir / "hyp.txt").write_text("针尖球心补偿需要重新标定", encoding="utf-8")
    print(f"wrote {len(meetings)} synthetic meetings under {OUT}")


if __name__ == "__main__":
    main()
