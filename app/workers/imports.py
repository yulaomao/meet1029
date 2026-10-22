"""Local serialized worker for imported meeting recordings."""

from __future__ import annotations

import time

from app.config import get_config
from app.logging import configure_logging, get_logger
from app.services.audio_import import AudioImportService
from app.web.factory import create_app


def main() -> None:
    configure_logging()
    log = get_logger("import-worker")
    app = create_app(get_config())
    with app.app_context():
        service = AudioImportService(app)
        recovered = service.recover_running()
        log.info("import worker started recovered=%s", recovered)
    while True:
        try:
            with app.app_context():
                processed = AudioImportService(app).run_next()
        except Exception:
            log.exception("import worker tick failed")
            processed = False
        if not processed:
            time.sleep(max(0.2, float(app.config.get("IMPORT_WORKER_POLL_SECONDS", 1.0))))


if __name__ == "__main__":
    main()
