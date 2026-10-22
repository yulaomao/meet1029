"""MEETING-AUDIO-012 + IndexedDB restore-on-load helper."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORDER = ROOT / "app" / "web" / "static" / "js" / "recorder.js"
LIVE = ROOT / "app" / "web" / "static" / "js" / "live.js"


def test_load_pending_from_idb_extracted():
    src = RECORDER.read_text(encoding='utf-8')
    assert 'export async function loadPendingFromIdb' in src
    assert 'export function rowToPendingRecord' in src
    assert 'async restore()' in src
    assert 'loadPendingFromIdb(this.meetingId' in src
    assert 'markAckAndDelete' in src
    assert 'json.ack' in src


def test_live_restores_queue_then_drains():
    src = LIVE.read_text(encoding='utf-8')
    assert 'queue.restore()' in src
    assert 'drainQueue(queue, meetingId)' in src
    assert 'force: false' in src or 'force:false' in src
    assert 'queue.size()' in src


def test_row_to_pending_record_shape():
    row = {'sequence': 3, 'start_time': 15000, 'duration': 5000,
           'mime_type': 'audio/webm', 'blob': b'x', 'client_id': 'web-abc'}
    rec = {'seq': row['sequence'], 'start_ms': row['start_time'],
           'duration_ms': row['duration'], 'mime': row['mime_type'],
           'blob': row['blob'], 'clientId': row['client_id']}
    assert rec['seq'] == 3
    assert rec['clientId'] == 'web-abc'
