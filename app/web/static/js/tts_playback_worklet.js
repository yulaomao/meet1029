/* Continuous PCM player for local Qwen3-TTS.  AudioBufferSource scheduling
 * creates gaps when socket deliveries vary; an AudioWorklet owns one queue. */
const FADE_FRAMES = 32;
const STATS_FRAMES = 12000;

class MeetingTtsPlayback extends AudioWorkletProcessor {
  constructor() {
    super();
    this.inputRate = 16000;
    // Keep speed in the single long-lived renderer.  The previous UI passed a
    // playback_rate with every TTS packet but the Worklet silently ignored it,
    // so Mandarin always played at the model's deliberately slow native rate.
    this.playbackRate = 1;
    this.step = this.inputRate * this.playbackRate / sampleRate;
    this.queue = [];
    this.index = 0;
    this.frac = 0;
    this.held = false;
    this.generation = 0;
    this.fadeIn = 0;
    this.fadeOut = 0;
    this.last = 0;
    this.frames = 0;
    this.playing = false;
    this.port.onmessage = ({ data }) => {
      if (!data || typeof data !== "object") return;
      if (data.kind === "config" && data.inputRate > 0) {
        this.inputRate = data.inputRate;
        // A measured correction for a complete, unusually slow short reply
        // may reach 1.60. Normal replies continue to use the user's 1.30.
        this.playbackRate = Math.min(1.60, Math.max(0.9, Number(data.playbackRate) || 1));
        this.step = this.inputRate * this.playbackRate / sampleRate;
      } else if (data.kind === "generation") {
        this.generation = Number(data.generation || 0); this.queue = []; this.index = 0; this.frac = 0; this.held = false;
      } else if (data.kind === "audio") {
        if (Number(data.generation || 0) !== this.generation || !(data.samples instanceof Float32Array)) return;
        this.queue.push(data.samples); this.playing = true; this.fadeOut = 0;
        if (this.queue.length === 1 && this.index === 0) this.fadeIn = FADE_FRAMES;
      } else if (data.kind === "hold" && Number(data.generation || 0) === this.generation) {
        this.held = true;
      } else if (data.kind === "resume" && Number(data.generation || 0) === this.generation) {
        this.held = false; this.fadeIn = FADE_FRAMES;
      } else if (data.kind === "clear") {
        this.generation = Number(data.generation || this.generation + 1);
        this.queue = []; this.index = 0; this.frac = 0; this.held = false;
        this.fadeOut = Math.abs(this.last) > 1e-4 ? FADE_FRAMES : 0;
      }
    };
  }
  read() {
    if (!this.queue.length) return null;
    const head = this.queue[0]; const a = head[this.index];
    const b = this.index + 1 < head.length ? head[this.index + 1] : (this.queue[1] ? this.queue[1][0] : a);
    return a + (b - a) * this.frac;
  }
  advance() {
    this.frac += this.step;
    while (this.frac >= 1) { this.frac -= 1; this.index += 1; }
    while (this.queue.length && this.index >= this.queue[0].length) { this.index -= this.queue[0].length; this.queue.shift(); }
  }
  queuedMs() {
    let count = -this.index; for (const item of this.queue) count += item.length;
    return Math.max(0, count) * 1000 / (this.inputRate * this.playbackRate);
  }
  process(_, outputs) {
    const out = outputs[0]?.[0]; if (!out) return true;
    for (let i = 0; i < out.length; i++) {
      let value = 0;
      if (!this.held && this.playing) {
        const read = this.read();
        if (read == null) { this.playing = false; this.last = 0; this.port.postMessage({ kind: "underrun" }); }
        else { value = Number.isFinite(read) ? read : 0; this.last = value; this.advance(); }
      }
      if (this.fadeIn > 0) { value *= 1 - this.fadeIn / FADE_FRAMES; this.fadeIn--; }
      if (this.fadeOut > 0) { value = this.last * this.fadeOut / FADE_FRAMES; this.fadeOut--; if (!this.fadeOut) this.last = 0; }
      out[i] = value;
    }
    this.frames += out.length;
    if (this.frames >= STATS_FRAMES) { this.frames = 0; this.port.postMessage({ kind: "stats", queuedMs: this.queuedMs(), generation: this.generation }); }
    return true;
  }
}
registerProcessor("meeting-tts-playback", MeetingTtsPlayback);
