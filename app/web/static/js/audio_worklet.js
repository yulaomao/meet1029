/**
 * AudioWorklet: browser Float32 -> 16 kHz Int16 PCM, ~40 ms chunks.
 * This is the canonical local FunASR input format.
 * Classic script for audioWorklet.addModule (not an ES module).
 */

const TARGET_RATE = 16000;
const DEFAULT_CHUNK_MS = 40;

class Pcm16Worklet extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const chunkMs = (options && options.processorOptions && options.processorOptions.chunkMs) || DEFAULT_CHUNK_MS;
    this._ratio = sampleRate / TARGET_RATE;
    this._chunkSamples = Math.round((TARGET_RATE * chunkMs) / 1000);
    this._scratch = new Float32Array(0);
    this._decimated = new Float32Array(this._chunkSamples);
  }

  _ingest(incoming) {
    if (!incoming || incoming.length === 0) return;
    const next = new Float32Array(this._scratch.length + incoming.length);
    next.set(this._scratch, 0);
    next.set(incoming, this._scratch.length);
    this._scratch = next;
    this._maybeEmit();
  }

  _maybeEmit() {
    const r = this._ratio;
    const n = this._chunkSamples;
    const needIn = Math.ceil(n * r);
    const dec = this._decimated;
    while (this._scratch.length >= needIn) {
      if (Math.abs(r - 2) < 1e-6) {
        for (let i = 0; i < n; i++) {
          const idx = i * 2;
          dec[i] = (this._scratch[idx] + this._scratch[idx + 1]) / 2;
        }
      } else {
        for (let i = 0; i < n; i++) {
          const srcPos = i * r;
          const idx = Math.floor(srcPos);
          const frac = srcPos - idx;
          const a = this._scratch[idx];
          const b = this._scratch[idx + 1] !== undefined ? this._scratch[idx + 1] : a;
          dec[i] = a + (b - a) * frac;
        }
      }
      const out = new Int16Array(n);
      for (let i = 0; i < n; i++) {
        const s = dec[i];
        const clamped = s < -1 ? -1 : s > 1 ? 1 : s;
        out[i] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      }
      const consumed = Math.floor(n * r);
      this._scratch = this._scratch.slice(consumed);
      this.port.postMessage(out.buffer, [out.buffer]);
    }
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0]) return true;
    const ch0 = input[0];
    const ch1 = input[1];
    if (ch1 && ch1.length === ch0.length) {
      const mixed = new Float32Array(ch0.length);
      for (let i = 0; i < ch0.length; i++) mixed[i] = (ch0[i] + ch1[i]) * 0.5;
      this._ingest(mixed);
    } else {
      this._ingest(ch0);
    }
    return true;
  }
}

registerProcessor("pcm16-worklet", Pcm16Worklet);
