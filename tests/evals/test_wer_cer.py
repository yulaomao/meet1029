from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CER_DIR = ROOT / "tests" / "testdata" / "cer"


def _ensure():
    if not (CER_DIR / "ref.txt").exists():
        from scripts.generate_synthetic_meetings import main

        main()


def test_wer_cer():
    _ensure()
    import jiwer

    ref = (CER_DIR / "ref.txt").read_text(encoding="utf-8").strip()
    hyp = (CER_DIR / "hyp.txt").read_text(encoding="utf-8").strip()
    cer = jiwer.cer(ref, hyp)
    wer = jiwer.wer(ref, hyp)
    assert cer == 0.0
    assert wer == 0.0
    assert (CER_DIR / "ref.wav").exists()
