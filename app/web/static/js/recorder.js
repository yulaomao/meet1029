/**
 * Reliable recording: MediaRecorder 5s chunks -> IndexedDB -> POST until ACK -> delete.
 * Do NOT hardcode a single mime type. Probe with MediaRecorder.isTypeSupported().
 */

const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/mpeg",
  "audio/ogg;codecs=opus",
  "audio/wav",
];

export function pickSupportedMime(isTypeSupported) {
  const probe = isTypeSupported || (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported
    ? (t) => MediaRecorder.isTypeSupported(t)
    : () => false);
  for (const mime of MIME_CANDIDATES) {
    try {
      if (probe(mime)) return mime;
    } catch (_e) {
      /* continue */
    }
  }
  return "";
}

const DB_NAME = "meeting-ai-chunks";
const STORE = "chunks";

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE, { keyPath: "id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

export function idbChunkKey(meetingId, clientId, seq) {
  return `${meetingId}:${clientId}:${seq}`;
}

export function rowToPendingRecord(row) {
  return {
    seq: row.sequence,
    start_ms: row.start_time,
    duration_ms: row.duration,
    mime: row.mime_type,
    blob: row.blob,
    clientId: row.client_id,
  };
}

/**
 * Load pending (not-yet-acked) IndexedDB rows for a meeting into a list of queue records.
 * Extracted so tests can assert restore-on-load exists independently of ChunkQueue.
 */
export async function loadPendingFromIdb(meetingId, clientId) {
  if (typeof indexedDB === "undefined") return [];
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const req = tx.objectStore(STORE).getAll();
    req.onsuccess = () => {
      const rows = (req.result || []).filter((r) => {
        if (r.meeting_id !== meetingId) return false;
        if (clientId && r.client_id !== clientId) return false;
        if (r.uploaded) return false;
        return true;
      });
      resolve(rows.map(rowToPendingRecord));
    };
    req.onerror = () => reject(req.error);
  });
}

export class ChunkQueue {
  constructor(meetingId, clientId) {
    this.meetingId = meetingId;
    this.clientId = clientId;
    this.pending = new Map();
  }

  async restore() {
    const recs = await loadPendingFromIdb(this.meetingId, this.clientId);
    for (const rec of recs) {
      this.pending.set(rec.seq, rec);
    }
    return this.pending.size;
  }

  async save(record) {
    this.pending.set(record.seq, { ...record, uploaded: false });
    if (typeof indexedDB === "undefined") return;
    const db = await openDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).put({
        id: idbChunkKey(this.meetingId, this.clientId, record.seq),
        meeting_id: this.meetingId,
        client_id: this.clientId,
        sequence: record.seq,
        start_time: record.start_ms,
        duration: record.duration_ms,
        mime_type: record.mime,
        blob: record.blob,
        uploaded: false,
      });
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  }

  async markAckAndDelete(seq) {
    this.pending.delete(seq);
    if (typeof indexedDB === "undefined") return;
    const db = await openDb();
    await new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, "readwrite");
      tx.objectStore(STORE).delete(idbChunkKey(this.meetingId, this.clientId, seq));
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  }

  size() {
    return this.pending.size;
  }
}

export async function uploadChunk(meetingId, record, fetchImpl) {
  const fetchFn = fetchImpl || fetch;
  const body = new FormData();
  body.append("seq", String(record.seq));
  body.append("start_ms", String(record.start_ms || 0));
  body.append("mime", record.mime || "");
  body.append("client_id", record.clientId || "web");
  body.append("file", record.blob, `chunk_${String(record.seq).padStart(6, "0")}`);
  const res = await fetchFn(`/api/meetings/${meetingId}/audio/chunks`, { method: "POST", body });
  if (!res.ok) {
    throw new Error("chunk upload failed");
  }
  const json = await res.json();
  if (!json.ack) {
    throw new Error("missing ack");
  }
  return json;
}

/**
 * Upload loop: retry until ACK, delete local ONLY after ack:true.
 */
export async function drainQueue(queue, meetingId, fetchImpl) {
  const seqs = Array.from(queue.pending.keys()).sort((a, b) => a - b);
  for (const seq of seqs) {
    const rec = queue.pending.get(seq);
    if (!rec) continue;
    let acked = false;
    let attempt = 0;
    while (!acked && attempt < 8) {
      attempt += 1;
      try {
        const json = await uploadChunk(meetingId, rec, fetchImpl);
        if (json && json.ack) {
          await queue.markAckAndDelete(seq);
          acked = true;
        }
      } catch (_e) {
        await new Promise((r) => setTimeout(r, 200 * attempt));
      }
    }
  }
  return queue.size();
}

export function createRecorder(stream, { timeslice = 5000, onChunk } = {}) {
  const mime = pickSupportedMime();
  const options = mime ? { mimeType: mime } : {};
  const recorder = new MediaRecorder(stream, options);
  let seq = 0;
  let chunkChain = Promise.resolve();
  recorder.ondataavailable = (ev) => {
    if (!ev.data || ev.data.size === 0) return;
    const rec = {
      seq: seq++,
      start_ms: seq * timeslice,
      duration_ms: timeslice,
      mime: recorder.mimeType || mime,
      blob: ev.data,
      clientId: "web",
    };
    if (onChunk) {
      chunkChain = chunkChain.then(() => onChunk(rec));
    }
  };
  recorder.flushed = () => chunkChain;
  recorder.start(timeslice);
  return recorder;
}
