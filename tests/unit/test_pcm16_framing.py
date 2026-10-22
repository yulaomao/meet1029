from app.realtime.framing import BYTES_PER_FRAME, SAMPLES_PER_FRAME, frames_from_pcm16, downsample_to_16k_mono


def test_pcm16_framing():
    """100 ms @ 16 kHz mono PCM16 == 3200 bytes."""
    samples = [0] * (SAMPLES_PER_FRAME * 3)
    frames = frames_from_pcm16(samples)
    assert len(frames) == 3
    assert all(len(f) == BYTES_PER_FRAME for f in frames)
    down = downsample_to_16k_mono([0.1] * 48000, in_rate=48000)
    assert len(down) == 16000
