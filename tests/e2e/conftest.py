from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture()
def live_server(app):
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    class Server:
        def __init__(self, port: int, application):
            self.port = port
            self.app = application
            self._url = f"http://127.0.0.1:{port}"

        def url(self):
            return self._url

    try:
        yield Server(port, app)
    finally:
        server.shutdown()
