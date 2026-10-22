/**
 * Downsample helper used by tests / non-worklet callers.
 * Kept out of audio_worklet.js so addModule can load a classic script.
 */
export function downsampleTo16kMonoPcm16(float32, inRate, frameMs = 100) {
  const target = 16000;
  const ratio = inRate / target;
  const out = [];
  for (let i = 0; i < float32.length; i += ratio) {
    const idx = Math.floor(i);
    const sample = float32[Math.min(idx, float32.length - 1)] || 0;
    const s = Math.max(-1, Math.min(1, sample));
    out.push(s < 0 ? s * 0x8000 : s * 0x7fff);
  }
  const frameSamples = (target * frameMs) / 1000;
  const frames = [];
  for (let i = 0; i < out.length; i += frameSamples) {
    const slice = out.slice(i, i + frameSamples);
    while (slice.length < frameSamples) slice.push(0);
    frames.push(Int16Array.from(slice));
  }
  return frames;
}
