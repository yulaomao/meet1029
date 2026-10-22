import { ChunkQueue, createRecorder, drainQueue, pickSupportedMime } from "./recorder.js";

const root = document.getElementById("live-root");
if (root) {
  const meetingId = root.dataset.meetingId;
  let clientId = sessionStorage.getItem("meeting-client-id");
  if (!clientId) {
    clientId = "web-" + Math.random().toString(36).slice(2, 10);
    sessionStorage.setItem("meeting-client-id", clientId);
  }
  let isHost = root.dataset.isHost === "1";
  const queue = new ChunkQueue(meetingId, clientId);
  let wakeLock = null;
  let recorder = null;
  let stream = null;
  let audioCtx = null;
  let socket = null;
  let muted = false;
  let joined = false;
  let networkOffline = navigator.onLine === false;
  let chunkDrainPromise = null;
  let drainChunksAgain = false;
  const pendingPcmFrames = [];
  let pendingPcmBytes = 0;
  let droppedPcmBytes = 0;
  const MAX_PENDING_PCM_BYTES = 30 * 16000 * 2;
  let startedAt = Date.now();
  let ttsCtx = null;
  let activeAiTurnId = 0;
  let activeAiGeneration = 0;
  let ttsPlayback = null;
  let ttsToneFilter = null;
  const transcriptRevisions = new Map();
  let aiThinkingStartedAt = 0;
  let aiLatencyTimer = null;
  const ttsSources = new Map();
  const ttsElements = new Set();
  const ttsTurns = new Map();
  const TTS_CACHE_MAX_BYTES = 32 * 1024 * 1024;
  // Follow new transcript/chat content only while the reader has stayed at
  // the bottom.  Scrolling upward is an explicit request to read history and
  // must never be overridden by a new partial, final, or AI token.
  const FOLLOW_BOTTOM_GAP_PX = 96;
  let followLiveMessages = true;

  function transcriptScroller() {
    return document.getElementById("transcript-list");
  }

  function nearLiveBottom() {
    const pane = transcriptScroller();
    if (!pane) return true;
    return pane.scrollTop + pane.clientHeight >= pane.scrollHeight - FOLLOW_BOTTOM_GAP_PX;
  }

  function rememberLiveScrollIntent() {
    followLiveMessages = nearLiveBottom();
  }

  function followLatestMessage(shouldFollow = followLiveMessages) {
    if (!shouldFollow) return;
    requestAnimationFrame(() => {
      if (!shouldFollow) return;
      const pane = transcriptScroller();
      if (pane) pane.scrollTop = pane.scrollHeight;
    });
  }

  transcriptScroller()?.addEventListener("scroll", rememberLiveScrollIntent, { passive: true });

  function renderAsrLag(msg) {
    const lagMs = Math.max(0, Number((msg && msg.queue_lag_ms) || 0));
    const processingMs = Math.max(0, Number((msg && msg.processing_ms) || 0));
    const label = lagMs < 100 ? "<0.1s" : (lagMs / 1000).toFixed(1) + "s";
    const el = document.getElementById("asr-lag");
    if (!el) return;
    el.textContent = label;
    el.title = `待处理音频 ${Math.round(lagMs)}ms；最近 ASR 处理 ${Math.round(processingMs)}ms`;
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function stopAiLatencyClock() {
    if (aiLatencyTimer) window.clearInterval(aiLatencyTimer);
    aiLatencyTimer = null;
  }

  function startAiLatencyClock() {
    stopAiLatencyClock();
    aiThinkingStartedAt = Date.now();
    const update = () => setText("ai-lag", ((Date.now() - aiThinkingStartedAt) / 1000).toFixed(1) + "s…");
    update();
    aiLatencyTimer = window.setInterval(update, 200);
  }

  function persistClientOnChunk(rec) {
    rec.clientId = clientId;
    return rec;
  }

  async function acquireWakeLock() {
    try {
      if (navigator.wakeLock) {
        wakeLock = await navigator.wakeLock.request("screen");
      }
    } catch (_e) {
      // Screen wake locks are optional and must not replace the network state.
    }
  }

  async function recover() {
    await acquireWakeLock();
    if (audioCtx && audioCtx.state === "suspended") {
      await audioCtx.resume();
    }
    if (stream) {
      const live = stream.getAudioTracks().some((t) => t.readyState === "live");
      if (!live) await startMic();
    }
    await drainRecordingQueue();
    if (socket && socket.disconnected) socket.connect();
  }

  function drainRecordingQueue() {
    if (networkOffline || navigator.onLine === false) return Promise.resolve(queue.size());
    if (chunkDrainPromise) {
      drainChunksAgain = true;
      return chunkDrainPromise;
    }
    chunkDrainPromise = drainQueue(queue, meetingId).finally(() => {
      chunkDrainPromise = null;
      setText("upload-queue", String(queue.size()));
      if (drainChunksAgain && !networkOffline && navigator.onLine !== false) {
        drainChunksAgain = false;
        window.setTimeout(() => drainRecordingQueue(), 0);
      }
    });
    return chunkDrainPromise;
  }

  document.addEventListener("click", () => {
    if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
    if (ttsCtx && ttsCtx.state === "suspended") ttsCtx.resume();
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") recover();
    else {
      const warn = document.getElementById("ai-callout");
      if (warn) {
        warn.hidden = false;
        warn.textContent = "会议录音正在进行，请保持会议页面处于前台。";
      }
    }
  });

  window.addEventListener("offline", () => {
    networkOffline = true;
    joined = false;
    setText("conn-state", "网络断开，正在缓冲实时音频");
  });
  window.addEventListener("online", () => {
    networkOffline = false;
    setText("conn-state", "网络已恢复，正在重连");
    // The archive recorder writes each chunk to IndexedDB before attempting
    // upload. Resume that durable queue as soon as connectivity returns; a
    // Socket.IO reconnect alone does not restore the recording archive.
    drainRecordingQueue();
    if (socket && socket.connected) joinMeetingSocket();
    else if (socket && socket.disconnected) socket.connect();
  });

  function attachWorklet(node) {
    node.port.onmessage = (ev) => {
      sendPcmFrame(ev.data);
    };
  }

  function enqueuePcmFrame(frame) {
    const bytes = new Uint8Array(frame);
    if (!bytes.byteLength) return;
    pendingPcmFrames.push(bytes);
    pendingPcmBytes += bytes.byteLength;
    while (pendingPcmBytes > MAX_PENDING_PCM_BYTES && pendingPcmFrames.length) {
      const dropped = pendingPcmFrames.shift();
      pendingPcmBytes -= dropped.byteLength;
      droppedPcmBytes += dropped.byteLength;
    }
  }

  function flushPendingPcmFrames() {
    if (!socket || !socket.connected || !joined || networkOffline) return;
    if (pendingPcmBytes > 0) {
      const payload = new Uint8Array(pendingPcmBytes);
      let offset = 0;
      for (const frame of pendingPcmFrames) {
        payload.set(frame, offset);
        offset += frame.byteLength;
      }
      pendingPcmFrames.length = 0;
      pendingPcmBytes = 0;
      // Send the ordered, bounded outage backlog as one binary event. This
      // avoids flooding the polling transport with hundreds of tiny emits.
      socket.emit("audio_frame", payload);
    }
    setText(
      "conn-state",
      droppedPcmBytes
        ? `已重连，缓冲上限导致约 ${(droppedPcmBytes / (16000 * 2)).toFixed(1)} 秒实时音频未补传`
        : "在线",
    );
  }

  function sendPcmFrame(frame) {
    if (muted || !frame) return;
    const bytes = new Uint8Array(frame);
    if (!bytes.byteLength) return;
    if (socket && socket.connected && joined && !networkOffline && pendingPcmFrames.length === 0) {
      // BINARY PCM16; meeting_id is bound to the Socket.IO session.
      socket.emit("audio_frame", bytes);
      return;
    }
    enqueuePcmFrame(bytes);
    if (socket && socket.connected && joined && !networkOffline) flushPendingPcmFrames();
    else if (droppedPcmBytes) setText("conn-state", "重连中，实时音频缓冲已满");
  }

  async function startMic() {
    const audioConstraints = {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    };
    const savedDevice = localStorage.getItem("meeting-mic-device-id");
    if (savedDevice) {
      audioConstraints.deviceId = { exact: savedDevice };
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
    } catch (_err) {
      delete audioConstraints.deviceId;
      stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
    }
    // Mic capture graph only. Do NOT share this context with TTS — Chrome AEC
    // treats same-context playback as reverse reference and colors the archive mic.
    audioCtx = new AudioContext({ latencyHint: "interactive" });
    if (audioCtx.state === "suspended") await audioCtx.resume().catch(() => {});
    try {
      await audioCtx.audioWorklet.addModule("/static/js/audio_worklet.js");
      const src = audioCtx.createMediaStreamSource(stream);
      const node = new AudioWorkletNode(audioCtx, "pcm16-worklet");
      attachWorklet(node);
      const silent = audioCtx.createGain();
      silent.gain.value = 0;
      src.connect(node);
      node.connect(silent);
      silent.connect(audioCtx.destination);
    } catch (err) {
      setText("conn-state", "音频处理模块加载失败");
    }
    if (isHost) await startRecorder();
    // Initialise the output graph while microphone capture is authorised. This
    // removes Worklet loading from the first assistant reply.
    try {
      await ensurePlaybackCtx();
    } catch (_err) {
      // Capture/transcription remains usable if an older browser cannot load
      // the output Worklet; the first TTS packet retains its existing fallback.
      setText("conn-state", "语音播放模块暂不可用");
    }
  }

  async function startRecorder() {
    if (!stream) return;
    if (recorder && recorder.state !== "inactive") return;
    recorder = createRecorder(stream, {
      timeslice: 5000,
      onChunk: async (rec) => {
        persistClientOnChunk(rec);
        await queue.save(rec);
        setText("upload-queue", String(queue.size()));
        try {
          await drainRecordingQueue();
        } catch (_err) { /* retry later */ }
        setText("upload-queue", String(queue.size()));
      },
    });
    isHost = true;
    setText("host-label", clientId);
  }

  async function stopRecorder() {
    if (recorder && recorder.state !== "inactive") {
      const stopped = new Promise((resolve) => {
        recorder.addEventListener("stop", resolve, { once: true });
      });
      recorder.stop();
      await stopped;
      if (typeof recorder.flushed === "function") {
        await recorder.flushed();
      }
    }
    recorder = null;
    isHost = false;
  }

  function b64ToUint8(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  async function ensurePlaybackCtx() {
    // Dedicated context: mic AEC must never use assistant playback as input.
    if (!ttsCtx || ttsCtx === audioCtx) {
      ttsCtx = new AudioContext({ latencyHint: "interactive" });
      await ttsCtx.audioWorklet.addModule("/static/js/tts_playback_worklet.js");
      ttsPlayback = new AudioWorkletNode(ttsCtx, "meeting-tts-playback", { numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1] });
      // Serena is a native-Chinese voice, but its upper-midrange can sound
      // overly bright on laptop speakers.  A gentle high shelf reduces
      // sibilance without changing speed, pitch, or the PCM stream itself.
      ttsToneFilter = ttsCtx.createBiquadFilter();
      ttsToneFilter.type = "highshelf";
      ttsToneFilter.frequency.value = 3800;
      ttsToneFilter.Q.value = 0.7;
      ttsToneFilter.gain.value = -3;
      ttsPlayback.connect(ttsToneFilter);
      ttsToneFilter.connect(ttsCtx.destination);
      ttsPlayback.port.postMessage({ kind: "config", inputRate: 16000, playbackRate: 1 });
      // ai_status may have already advanced the live turn while this player
      // did not exist. Without this, the first PCM packets (generation 1)
      // are rejected by a newly-created Worklet still at generation 0.
      ttsPlayback.port.postMessage({ kind: "generation", generation: activeAiGeneration });
      ttsPlayback.port.onmessage = ({ data }) => {
        // A natural end also empties the queue.  Treat underruns as telemetry,
        // not a visible error/state transition for the participant.
        if (data?.kind === "stats") window.MeetingTtsStats = data;
      };
    }
    if (ttsCtx.state === "suspended") await ttsCtx.resume().catch(() => {});
    return ttsCtx;
  }

  function ttsTurn(turnId) {
    const id = Number(turnId || activeAiTurnId || 0);
    if (!ttsTurns.has(id)) ttsTurns.set(id, { items: [], bytes: 0, held: false });
    return ttsTurns.get(id);
  }

  function pruneTtsCache(turn) {
    while (turn.items.length && turn.items[0].done) {
      const item = turn.items.shift();
      turn.bytes = Math.max(0, turn.bytes - item.bytes);
    }
  }

  function stopSources(turnId = null, { preserve = false } = {}) {
    for (const [src, meta] of ttsSources) {
      if (turnId != null && meta.turnId !== Number(turnId)) continue;
      meta.stopped = true;
      try { src.stop(); } catch (_e) { /* already ended */ }
      ttsSources.delete(src);
    }
    if (!preserve) ttsNextAt = 0;
  }

  function stopTtsPlayback(turnId = null) {
    const generation = activeAiGeneration + 1;
    activeAiGeneration = generation;
    ttsPlayback?.port.postMessage({ kind: "clear", generation });
    stopSources(turnId);
    for (const audio of ttsElements) {
      try { audio.pause(); audio.src = ""; } catch (_e) { /* ignore */ }
    }
    ttsElements.clear();
    if (turnId == null) ttsTurns.clear();
    else ttsTurns.delete(Number(turnId));
  }

  function pcm16Buffer(ctx, bytes, sampleRate) {
    const n = Math.floor(bytes.byteLength / 2);
    if (n < 8) return null;
    const view = new DataView(bytes.buffer, bytes.byteOffset, n * 2);
    const buf = ctx.createBuffer(1, n, sampleRate || 16000);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < n; i++) {
      const s = view.getInt16(i * 2, true);
      ch[i] = s < 0 ? s / 0x8000 : s / 0x7fff;
    }
    return buf;
  }

  function scheduleTtsTurn(turnId) {
    const turn = ttsTurn(turnId);
    if (turn.held || Number(turnId) !== Number(activeAiTurnId)) return;
    const ctx = ttsCtx;
    if (!ctx) return;
    for (const item of turn.items) {
      if (item.done || item.scheduled) continue;
      const src = ctx.createBufferSource();
      src.buffer = item.buffer;
      src.playbackRate.value = item.rate;
      const startAt = Math.max(ttsNextAt, ctx.currentTime + 0.025);
      item.scheduled = true;
      item.startAt = startAt;
      const meta = { turnId: Number(turnId), item, stopped: false };
      src.onended = () => {
        ttsSources.delete(src);
        if (meta.stopped) return;
        item.done = true;
        item.scheduled = false;
        pruneTtsCache(turn);
      };
      src.connect(ttsToneFilter || ctx.destination);
      ttsSources.set(src, meta);
      src.start(startAt, item.offset || 0);
      ttsNextAt = startAt + Math.max(0, item.buffer.duration - (item.offset || 0)) / item.rate;
    }
  }

  function enqueueTtsBuffer(turnId, buffer, bytes, playbackRate) {
    const turn = ttsTurn(turnId);
    const item = {
      buffer,
      bytes: Number(bytes || 0),
      rate: Math.min(1.60, Math.max(0.9, Number(playbackRate) || 1)),
      offset: 0,
      scheduled: false,
      done: false,
      startAt: 0,
    };
    turn.items.push(item);
    turn.bytes += item.bytes;
    pruneTtsCache(turn);
    // A typical local answer is far below this cap. If an unusually long
    // response exceeds it, keep unplayed PCM rather than silently dropping it.
    scheduleTtsTurn(turnId);
  }

  function holdTtsPlayback(turnId) {
    const id = Number(turnId || activeAiTurnId || 0);
    if (!id) return;
    const turn = ttsTurn(id);
    if (turn.held) return;
    const ctx = ttsCtx;
    for (const [src, meta] of [...ttsSources]) {
      if (meta.turnId !== id) continue;
      const item = meta.item;
      if (ctx && ctx.currentTime > item.startAt) {
        item.offset = Math.min(item.buffer.duration, (item.offset || 0) + (ctx.currentTime - item.startAt) * item.rate);
      }
      item.scheduled = false;
      meta.stopped = true;
      try { src.stop(); } catch (_e) { /* already ended */ }
      ttsSources.delete(src);
    }
    turn.held = true;
    ttsNextAt = 0;
  }

  function resumeTtsPlayback(turnId) {
    const id = Number(turnId || activeAiTurnId || 0);
    const turn = ttsTurns.get(id);
    if (!turn) return;
    turn.held = false;
    ttsNextAt = 0;
    scheduleTtsTurn(id);
  }

  async function playPcm16(ctx, bytes, sampleRate, playbackRate, turnId) {
    const buf = pcm16Buffer(ctx, bytes, sampleRate);
    if (!buf) return;
    enqueueTtsBuffer(turnId, buf, bytes.byteLength, playbackRate);
  }

  async function playTtsAudio(msg) {
    try {
      if (!msg) return;
      if (typeof msg === "object" && msg.turn_id && activeAiTurnId && msg.turn_id !== activeAiTurnId) return;
      const generation = Number((msg && msg.generation) || activeAiGeneration || 0);
      if (generation !== activeAiGeneration) return;
      let u8 = null;
      let mime = "audio/wav";
      let rate = 16000;
      let playbackRate = 1;
      if (typeof msg === "string") {
        if (!msg) return;
        u8 = b64ToUint8(msg);
      } else if (msg.audio_b64) {
        if (!msg.audio_b64) return;
        u8 = b64ToUint8(msg.audio_b64);
        mime = msg.mime || "audio/wav";
        rate = msg.sample_rate || 16000;
        playbackRate = msg.playback_rate || 1;
      } else {
        let buf = null;
        if (msg instanceof ArrayBuffer) buf = msg;
        else if (msg instanceof Uint8Array) buf = msg.buffer;
        else if (msg.audio instanceof ArrayBuffer) buf = msg.audio;
        else if (msg.data instanceof ArrayBuffer) buf = msg.data;
        if (buf) u8 = new Uint8Array(buf);
        if (msg && msg.mime) mime = msg.mime;
        if (msg && msg.sample_rate) rate = msg.sample_rate;
        if (msg && msg.playback_rate) playbackRate = msg.playback_rate;
      }
      if (!u8 || !u8.byteLength) return;
      const turnId = Number((msg && msg.turn_id) || activeAiTurnId || 0);
      const looksWav = (mime || "").indexOf("wav") >= 0 || (u8.length >= 4 && u8[0] === 0x52 && u8[1] === 0x49);
      // Prefer HTMLAudioElement for WAV so Tingting is not the reverse-AEC reference for the mic graph.
      if (looksWav && typeof msg === "object" && msg && msg.audio_b64) {
        // Decode WAV through the same resumable queue when possible.
      }
      const ctx = await ensurePlaybackCtx();
      const copy = u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength);
      if (looksWav) {
        const decoded = await ctx.decodeAudioData(copy);
        ttsPlayback?.port.postMessage({ kind: "config", inputRate: decoded.sampleRate || rate || 16000, playbackRate });
        const samples = decoded.getChannelData(0).slice();
        ttsPlayback?.port.postMessage({ kind: "audio", generation, samples }, [samples.buffer]);
        return;
      }
      const raw = new DataView(copy); const samples = new Float32Array(Math.floor(copy.byteLength / 2));
      for (let i = 0; i < samples.length; i++) { const n = raw.getInt16(i * 2, true); samples[i] = n < 0 ? n / 0x8000 : n / 0x7fff; }
      ttsPlayback?.port.postMessage({ kind: "config", inputRate: rate || 16000, playbackRate });
      ttsPlayback?.port.postMessage({ kind: "audio", generation, samples }, [samples.buffer]);
    } catch (_e) {
      try {
        if (msg && msg.audio_b64) {
          const audio = new Audio("data:" + (msg.mime || "audio/wav") + ";base64," + msg.audio_b64);
          ttsElements.add(audio);
          audio.addEventListener("ended", () => ttsElements.delete(audio), { once: true });
          audio.play().catch(() => {});
        }
      } catch (_e2) { /* ignore autoplay */ }
    }
  }

  function renderState(state) {
    if (!state) return;
    if (state.current_topic != null) setText("live-topic", state.current_topic || "—");
    function fill(id, items, pick) {
      const ul = document.getElementById(id);
      if (!ul || !items) return;
      ul.innerHTML = "";
      if (!items.length) {
        const li = document.createElement("li");
        li.className = "muted";
        li.textContent = "暂无";
        ul.appendChild(li);
        return;
      }
      for (const it of items) {
        const li = document.createElement("li");
        li.textContent = pick(it);
        ul.appendChild(li);
      }
    }
    fill("live-decisions", state.decisions, (d) => (d && d.text) || d);
    fill("live-questions", state.open_questions, (q) => q);
    fill("live-actions", state.action_items, (a) => (a && a.description) || a);
    renderSpeakerViews(state.speaker_views);
    if (state.rolling_summary != null) {
      const sum = document.getElementById("live-summary");
      if (sum) sum.textContent = state.rolling_summary || "—";
    }
  }

  function renderSpeakerViews(views) {
    const container = document.getElementById("live-speaker-views");
    if (!container || !Array.isArray(views)) return;
    container.replaceChildren();
    if (!views.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "尚未形成可验证的按人观点";
      container.appendChild(empty);
      return;
    }
    const addPoints = (card, points, label, className, showPosition = false) => {
      if (!Array.isArray(points)) return;
      for (const point of points) {
        if (!point || !point.text) continue;
        const line = document.createElement("p");
        if (className) line.className = className;
        if (showPosition && point.position) {
          const stance = document.createElement("span");
          stance.className = "badge badge-stance";
          stance.textContent = point.position;
          line.appendChild(stance);
          line.appendChild(document.createTextNode(" "));
        }
        if (label) line.appendChild(document.createTextNode(label));
        line.appendChild(document.createTextNode(point.text));
        const ids = Array.isArray(point.source_segment_ids) ? point.source_segment_ids : [];
        for (const id of ids) {
          const link = document.createElement("a");
          link.className = "view-source";
          link.href = "#seg-" + encodeURIComponent(id);
          link.textContent = "依据";
          line.appendChild(document.createTextNode(" "));
          line.appendChild(link);
        }
        card.appendChild(line);
      }
    };
    for (const view of views) {
      if (!view || !view.speaker_label || !view.topic) continue;
      const card = document.createElement("article");
      card.className = "speaker-view";
      const heading = document.createElement("h4");
      heading.textContent = view.speaker_label;
      const topic = document.createElement("span");
      topic.className = "view-topic";
      topic.textContent = " · " + view.topic;
      heading.appendChild(topic);
      card.appendChild(heading);
      addPoints(card, view.explicit_stances, "", "", true);
      addPoints(card, view.key_points, "", "");
      addPoints(card, view.suggestions, "建议：", "");
      addPoints(card, view.concerns, "顾虑：", "view-concern");
      addPoints(card, view.open_questions, "待确认：", "view-concern");
      addPoints(card, view.possible_tendencies, "可能倾向：", "view-tendency");
      container.appendChild(card);
    }
    if (!container.children.length) {
      const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "尚未形成可验证的按人观点"; container.appendChild(empty);
    }
  }

  function insertTranscriptSegment(msg, { replaceLive = false } = {}) {
    const list = document.getElementById("transcript-list");
    if (!list || !msg) return;
    const shouldFollow = nearLiveBottom();
    const id = msg.id;
    const text = msg.text || msg.display_text || msg.raw_text || "";
    const speaker = msg.speaker || msg.speaker_label || "Speaker";
    const startMs = Math.max(0, Number(msg.start_ms || 0));
    const live = replaceLive && list.querySelector("article.seg.partial-live");
    if (live) {
      live.className = "seg";
      if (id) live.setAttribute("data-id", id);
      if (id) live.id = "seg-" + id;
      live.dataset.startMs = String(startMs);
      live.querySelector("strong").textContent = speaker;
      live.querySelector("p").textContent = text;
      followLatestMessage(shouldFollow);
      return;
    }
    if (id) {
      const existing = list.querySelector('article[data-id="' + id + '"]');
      if (existing) {
        const p = existing.querySelector("p");
        if (p) p.textContent = text;
        const strong = existing.querySelector("strong");
        if (strong) strong.textContent = speaker;
        existing.dataset.startMs = String(startMs);
        followLatestMessage(shouldFollow);
        return;
      }
    }
    const art = document.createElement("article");
    art.className = "seg";
    if (id) art.setAttribute("data-id", id);
    if (id) art.id = "seg-" + id;
    art.dataset.startMs = String(startMs);
    art.innerHTML = `<header><strong></strong></header><p></p>`;
    art.querySelector("strong").textContent = speaker;
    art.querySelector("p").textContent = text;
    // AI replies are committed only after Qwen/TTS finishes. Insert by their
    // captured generation-start position so a slow answer never appears below
    // participant messages that happened later in real meeting time.
    const later = Array.from(list.querySelectorAll("article.seg")).find((row) => {
      const rowStart = Number(row.dataset.startMs || 0);
      return rowStart > startMs;
    });
    if (later) list.insertBefore(art, later);
    else list.appendChild(art);
    followLatestMessage(shouldFollow);
  }

  async function pollTranscript() {
    try {
      const res = await fetch(`/api/meetings/${meetingId}/transcript`);
      if (!res.ok) return;
      const data = await res.json();
      const segs = (data && data.segments) || [];
      for (const seg of segs) {
        insertTranscriptSegment({
          id: seg.id,
          text: seg.display_text || seg.raw_text || "",
          speaker: seg.speaker_label || "Speaker",
          start_ms: seg.start_ms,
        });
      }
    } catch (_e) { /* reliability poll */ }
  }

  async function pollState() {
    try {
      const res = await fetch(`/api/meetings/${meetingId}/state`);
      if (!res.ok) return;
      const data = await res.json();
      renderState((data && data.state) || data);
    } catch (_e) { /* reliability poll */ }
  }

  function connectSocket() {
    let delay = 500;
    const ioFn = window.io;
    if (!ioFn) return;
    socket = ioFn("/live", { reconnection: true, reconnectionAttempts: Infinity });
    socket.on("connect", () => {
      delay = 500;
      joined = false;
      setText("conn-state", networkOffline ? "网络断开" : "正在加入会议");
      joinMeetingSocket();
    });
    socket.on("recording_status", () => {
      if (networkOffline) return;
      joined = true;
      flushPendingPcmFrames();
    });
    socket.on("disconnect", () => {
      joined = false;
      setText("conn-state", "重连中");
    });
    socket.io.on("reconnect_attempt", () => {
      delay = Math.min(delay * 2, 8000);
    });
    socket.on("transcript_partial", (msg) => {
      const text = msg.text || "";
      const utteranceId = String((msg && msg.utterance_id) || "");
      const revision = Number((msg && msg.turn_revision) || 0);
      if (utteranceId) {
        const seen = transcriptRevisions.get(utteranceId);
        // Socket.IO preserves order, but this also protects reconnect/replay
        // delivery from putting an obsolete partial back on screen.
        if (seen != null && revision < seen) return;
        transcriptRevisions.set(utteranceId, revision);
      }
      const el = document.getElementById("partial");
      if (el) el.textContent = text ? text + " ▌" : "▌";
      const list = document.getElementById("transcript-list");
      if (!list) return;
      const shouldFollow = nearLiveBottom();
      let art = list.querySelector("article.seg.partial-live");
      if (art && utteranceId && art.dataset.utteranceId && art.dataset.utteranceId !== utteranceId) {
        art.remove();
        art = null;
      }
      if (!text) {
        if (art) art.remove();
        return;
      }
      if (!art) {
        art = document.createElement("article");
        art.className = "seg partial-live";
        art.innerHTML = "<header><strong></strong></header><p></p>";
        art.querySelector("strong").textContent = "正在说";
        list.appendChild(art);
      }
      if (utteranceId) art.dataset.utteranceId = utteranceId;
      art.querySelector("p").textContent = text + " ▌";
      followLatestMessage(shouldFollow);
    });
    socket.on("transcript_final", (msg) => {
      const utteranceId = String((msg && msg.utterance_id) || "");
      const revision = Number((msg && msg.turn_revision) || 0);
      if (utteranceId) {
        const seen = transcriptRevisions.get(utteranceId);
        if (seen != null && revision < seen) return;
        transcriptRevisions.set(utteranceId, revision);
      }
      const el = document.getElementById("partial");
      if (el) el.textContent = "▌";
      const live = document.querySelector("article.seg.partial-live");
      const canReplaceLive = !live || !msg.utterance_id || !live.dataset.utteranceId || live.dataset.utteranceId === String(msg.utterance_id);
      insertTranscriptSegment(msg, { replaceLive: canReplaceLive });
    });
    socket.on("transcript_reopen", (msg) => {
      const utteranceId = String((msg && msg.utterance_id) || "");
      if (utteranceId) transcriptRevisions.set(utteranceId, Number(msg.turn_revision || 0));
      const live = document.querySelector("article.seg.partial-live");
      if (live && (!utteranceId || live.dataset.utteranceId === utteranceId)) live.remove();
      const el = document.getElementById("partial");
      if (el) el.textContent = "▌";
    });
    socket.on("ai_status", (msg) => {
      const state = (msg && msg.state) || "listening";
      if (msg && msg.generation != null) {
        const incomingGeneration = Number(msg.generation);
        if (incomingGeneration < activeAiGeneration) return;
        if (incomingGeneration > activeAiGeneration) {
          activeAiGeneration = incomingGeneration;
          ttsPlayback?.port.postMessage({ kind: "generation", generation: activeAiGeneration });
        }
      }
      if (msg && msg.turn_id && msg.turn_id !== activeAiTurnId) {
        ttsPlayback?.port.postMessage({ kind: "generation", generation: activeAiGeneration });
        activeAiTurnId = msg.turn_id;
      }
      const labels = {
        thinking: "正在思考", speaking: "正在回答", interrupted: "已被打断",
        barge_pending: "正在识别你的回应…",
        listening: "等待点名", dialogue_listening: "连续对话中（说“成就精灵停止回复”结束）",
        dialogue_stopped: "连续对话已结束",
      };
      setText("ai-state", labels[state] || "等待点名");
      if (state === "thinking" && msg && msg.first_token_latency_ms != null) {
        stopAiLatencyClock();
        setText("ai-lag", (Number(msg.first_token_latency_ms) / 1000).toFixed(1) + "s");
      } else if (state === "thinking") {
        startAiLatencyClock();
      } else if (["speaking", "interrupted", "dialogue_listening", "dialogue_stopped", "listening"].includes(state)) {
        stopAiLatencyClock();
      }
      const banner = document.getElementById("ai-callout");
      if (banner) banner.hidden = !["thinking", "speaking", "barge_pending"].includes(state);
      const stop = document.getElementById("stop-ai");
      if (stop) {
        const inDialogue = state === "dialogue_listening";
        const actionable = ["thinking", "speaking", "barge_pending", "dialogue_listening"].includes(state);
        stop.disabled = !actionable;
        stop.textContent = inDialogue ? "结束 AI 对话" : "停止 AI 回复";
      }
      if (["thinking", "speaking"].includes(state)) {
        const streamEl = document.getElementById("ai-token-stream");
        if (streamEl && state === "thinking") streamEl.textContent = "";
      }
      // dialogue_listening is estimated on the server from generated PCM. Do
      // not clear the Worklet queue there: local TTS generates much faster
      // than it plays, and that estimate used to cut off the final words.
      if (state === "interrupted" || state === "dialogue_stopped") {
        stopTtsPlayback();
      }
    });
    socket.on("ai_token", (msg) => {
      if (msg && msg.turn_id && activeAiTurnId && msg.turn_id !== activeAiTurnId) return;
      const el = document.getElementById("ai-token-stream");
      if (el) {
        el.textContent += (msg.text || msg.token || "");
      }
    });
    socket.on("asr_metrics", renderAsrLag);
    socket.on("tts_audio", (msg) => playTtsAudio(msg));
    socket.on("tts_hold", (msg) => {
      if (!msg || !msg.turn_id || msg.turn_id === activeAiTurnId) {
        ttsPlayback?.port.postMessage({ kind: "hold", generation: Number(msg?.generation ?? activeAiGeneration) });
      }
    });
    socket.on("tts_resume", (msg) => {
      if (!msg || !msg.turn_id || msg.turn_id === activeAiTurnId) {
        ttsPlayback?.port.postMessage({ kind: "resume", generation: Number(msg?.generation ?? activeAiGeneration) });
      }
    });
    socket.on("tts_stop", (msg) => {
      if (!msg || !msg.turn_id || msg.turn_id === activeAiTurnId) {
        activeAiGeneration = Math.max(activeAiGeneration, Number(msg?.generation ?? activeAiGeneration));
        ttsPlayback?.port.postMessage({ kind: "clear", generation: activeAiGeneration });
      }
    });
    socket.on("meeting_state_patch", (msg) => {
      renderState((msg && (msg.state || msg.patch)) || msg);
    });
    function onHostEvent(msg) {
      const newHost = (msg && (msg.host_client_id || msg.to_client_id)) || "";
      const prev = (msg && (msg.previous_host_client_id || msg.from_client_id)) || "";
      if (newHost) setText("host-label", newHost);
      if (newHost === clientId) {
        startRecorder();
      } else if (prev === clientId || (isHost && newHost && newHost !== clientId)) {
        stopRecorder();
      }
    }
    socket.on("host_changed", onHostEvent);
    socket.on("host_transferred", onHostEvent);
    setInterval(() => socket && socket.emit("heartbeat", { meeting_id: meetingId }), 5000);
  }

  function joinMeetingSocket() {
    if (!socket || !socket.connected || networkOffline || joined) return;
    socket.emit("join_meeting", { meeting_id: meetingId, client_id: clientId });
  }

  setInterval(pollTranscript, 2000);
  setInterval(pollState, 5000);
  pollTranscript();
  pollState();
  // Opening a live conversation conventionally starts at its newest message.
  requestAnimationFrame(() => {
    followLiveMessages = true;
    followLatestMessage(true);
  });

  setInterval(() => {
    const s = Math.floor((Date.now() - startedAt) / 1000);
    const hh = String(Math.floor(s / 3600)).padStart(2, "0");
    const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    setText("rec-time", `${hh}:${mm}:${ss}`);
  }, 1000);

  const endBtn = document.getElementById("end-meeting");
  if (endBtn) {
    endBtn.addEventListener("click", async () => {
      endBtn.disabled = true;
      try {
        await stopRecorder();
        const remaining = await drainQueue(queue, meetingId);
        setText("upload-queue", String(queue.size()));
        if (queue.size() > 0 || remaining > 0) {
          setText("conn-state", "上传未完成，未结束会议");
          endBtn.disabled = false;
          return;
        }
        const response = await fetch(`/api/meetings/${meetingId}/stop`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ force: false, client_id: clientId }),
        });
        if (!response.ok) throw new Error("结束会议请求未被接受");
        setText("conn-state", "录音已保存，正在后台整理会议记录…");
        // Final ASR and summary run in the serialized local worker. Do not
        // keep the live page open waiting for a long meeting to finish.
        window.location.assign("/meetings");
      } catch (_e) {
        setText("conn-state", "结束会议失败");
        endBtn.disabled = false;
      }
    });
  }

  const take = document.getElementById("take-host");
  if (take) {
    take.addEventListener("click", async () => {
      const res = await fetch(`/api/meetings/${meetingId}/host-transfer`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id: clientId }),
      });
      if (res.ok) {
        await startRecorder();
      }
    });
  }

  const stopAi = document.getElementById("stop-ai");
  if (stopAi) {
    stopAi.addEventListener("click", () => {
      if (stopAi.disabled) return;
      stopTtsPlayback();
      stopAi.disabled = true;
      if (socket && socket.connected) {
        socket.emit("stop_ai", {
          meeting_id: meetingId,
          turn_id: activeAiTurnId,
          end_dialogue: true,
          requested_at: Date.now(),
        });
      }
    });
  }

  queue.restore().then(() => drainRecordingQueue()).catch(() => {});

  connectSocket();
  if (navigator.mediaDevices) {
    startMic().then(acquireWakeLock).catch(() => {
      setText("conn-state", "麦克风不可用（演示模式）");
    });
  }

  window.MeetingLive = {
    pickSupportedMime,
    queue,
    recover,
    clientId,
    stopTtsPlayback,
    sendPcmFrame,
    get pendingRealtimePcmBytes() { return pendingPcmBytes; },
    get droppedRealtimePcmBytes() { return droppedPcmBytes; },
  };
}
