import { pickSupportedMime } from "./recorder.js";

const root = document.getElementById("enroll-root");
if (root) {
  const MIN_S = 30;
  const MAX_S = 60;
  const meter = document.getElementById("enroll-meter");
  const barFill = document.getElementById("enroll-bar-fill");
  const startBtn = document.getElementById("enroll-start");
  const stopBtn = document.getElementById("enroll-stop");
  const submitBtn = document.getElementById("enroll-btn");
  const form = document.getElementById("enroll-form");
  const secondsInput = document.getElementById("sample_seconds");
  const fileInput = document.getElementById("enroll-file");
  const errEl = document.getElementById("enroll-error");
  const enrollUrl = root.dataset.enrollUrl;

  let stream = null;
  let recorder = null;
  let chunks = [];
  let startedAt = 0;
  let timer = null;
  let elapsed = 0;
  let recordedBlob = null;
  let analyser = null;
  let audioCtx = null;
  let raf = 0;

  function showError(msg) {
    if (!errEl) return;
    errEl.hidden = !msg;
    errEl.textContent = msg || "";
  }

  function setMeter(sec) {
    elapsed = sec;
    if (meter) meter.textContent = `${sec.toFixed(0)}秒 / 建议60秒`;
    if (barFill) barFill.style.width = `${Math.min(100, (sec / MAX_S) * 100)}%`;
    if (secondsInput) secondsInput.value = String(Math.round(sec));
    if (submitBtn) submitBtn.disabled = sec < MIN_S;
  }

  function setQuality(rms) {
    const vol = document.getElementById("q-volume");
    const noise = document.getElementById("q-noise");
    const speech = document.getElementById("q-speech");
    if (vol) vol.textContent = rms > 0.02 ? "● 音量正常" : "○ 音量偏低";
    if (noise) noise.textContent = rms < 0.35 ? "● 背景噪声较低" : "○ 背景噪声偏高";
    if (speech) speech.textContent = rms > 0.04 ? "● 有效语音充足" : "○ 有效语音不足";
  }

  function tickAnalyser() {
    if (!analyser) return;
    const buf = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const v = (buf[i] - 128) / 128;
      sum += v * v;
    }
    setQuality(Math.sqrt(sum / buf.length));
    raf = requestAnimationFrame(tickAnalyser);
  }

  function stopTracks() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
    if (audioCtx) {
      try { audioCtx.close(); } catch (_e) { /* ignore */ }
      audioCtx = null;
    }
  }

  async function startRec() {
    showError("");
    chunks = [];
    recordedBlob = null;
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
    const mime = pickSupportedMime();
    const options = mime ? { mimeType: mime } : {};
    recorder = new MediaRecorder(stream, options);
    recorder.ondataavailable = (ev) => {
      if (ev.data && ev.data.size) chunks.push(ev.data);
    };
    recorder.onstop = () => {
      const type = recorder.mimeType || mime || "audio/webm";
      recordedBlob = new Blob(chunks, { type });
      attachBlobToForm(recordedBlob);
      stopTracks();
    };
    try {
      audioCtx = new AudioContext();
      const src = audioCtx.createMediaStreamSource(stream);
      analyser = audioCtx.createAnalyser();
      analyser.fftSize = 2048;
      src.connect(analyser);
      tickAnalyser();
    } catch (_e) { /* analyser optional */ }
    startedAt = Date.now();
    recorder.start(1000);
    startBtn.disabled = true;
    stopBtn.disabled = false;
    submitBtn.disabled = true;
    timer = setInterval(() => {
      const sec = (Date.now() - startedAt) / 1000;
      setMeter(Math.min(sec, MAX_S));
      if (sec >= MAX_S) stopRec();
    }, 200);
  }

  function stopRec() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
    if (recorder && recorder.state !== "inactive") recorder.stop();
    startBtn.disabled = false;
    stopBtn.disabled = true;
  }

  function attachBlobToForm(blob) {
    if (!fileInput || !blob) return;
    const ext = (blob.type || "").includes("mp4") ? "m4a" : (blob.type || "").includes("ogg") ? "ogg" : (blob.type || "").includes("wav") ? "wav" : "webm";
    const file = new File([blob], `enroll.${ext}`, { type: blob.type || "audio/webm" });
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
  }

  startBtn.addEventListener("click", () => {
    startRec().catch((e) => showError("无法打开麦克风：" + (e && e.message ? e.message : e)));
  });
  stopBtn.addEventListener("click", stopRec);

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const file = (fileInput && fileInput.files && fileInput.files[0]) || recordedBlob;
    const sec = Number(secondsInput.value || elapsed || 0);
    if (!file) {
      showError("请先录音或选择音频文件");
      return;
    }
    if (sec < MIN_S) {
      showError("声纹登记需要 30～60 秒清晰语音");
      return;
    }
    const body = new FormData();
    body.append("sample_seconds", String(sec));
    body.append("audio", file, file.name || "enroll.webm");
    submitBtn.disabled = true;
    try {
      const res = await fetch(enrollUrl, { method: "POST", body });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) {
        showError(json.error || "登记失败");
        submitBtn.disabled = false;
        return;
      }
      window.location.href = "/team";
    } catch (e) {
      showError("上传失败");
      submitBtn.disabled = false;
    }
  });

  window.MeetingEnroll = { pickSupportedMime, MIN_S, MAX_S };
}
